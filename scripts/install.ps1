#Requires -Version 5.1
<#
.SYNOPSIS
  Install Sentinel-AI on a Windows developer machine (idempotent, zero config).

.DESCRIPTION
  1. Detects `uv` on PATH — installs it via Astral when missing
  2. Ensures Python 3.13 through `uv`
  3. Installs the `sentinel-ai` CLI from this repository
  4. Optionally writes a Husky pre-commit hook into a project repository

  Organisation config (AI server, policy, Trivy) ships inside the package at
  `src/sentinel_ai/sentinel.toml`. Developers do not set env vars manually.

.PARAMETER Source
  Local path to the sentinel-ai repository, or a git URL. Defaults to the repo
  that contains this script.

.PARAMETER Repo
  Project repository to run `sentinel-ai install-hook` after installation.

.PARAMETER SkipPython
  Skip `uv python install` when Python 3.13 is already managed by uv.

.EXAMPLE
  # Clone sentinel-ai, then one command — ready to use
  .\scripts\install.ps1

.EXAMPLE
  # Install CLI and wire the hook into a project
  .\scripts\install.ps1 -Repo "D:\Repositories\website"
#>
[CmdletBinding()]
param(
    [string] $Source,
    [string] $Repo,
    [switch] $SkipPython
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step([string] $Message) {
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Ok([string] $Message) {
    Write-Host " ok  $Message" -ForegroundColor Green
}

function Write-Warn([string] $Message) {
    Write-Host " !   $Message" -ForegroundColor Yellow
}

function Get-UvCandidatePaths {
    @(
        (Join-Path $env:USERPROFILE ".local\bin\uv.exe")
        (Join-Path $env:LOCALAPPDATA "uv\uv.exe")
        (Join-Path $env:ProgramFiles "uv\uv.exe")
    )
}

function Import-UvPath {
    $dirs = @(
        (Join-Path $env:USERPROFILE ".local\bin")
        (Join-Path $env:LOCALAPPDATA "uv")
        (Join-Path $env:ProgramFiles "uv")
    ) | Where-Object { Test-Path $_ }

    foreach ($dir in $dirs) {
        if ($env:PATH -notlike "*$dir*") {
            $env:PATH = "$dir;$env:PATH"
        }
    }
}

function Find-UvExecutable {
    Import-UvPath
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }
    foreach ($candidate in Get-UvCandidatePaths) {
        if (Test-Path $candidate) {
            return (Resolve-Path $candidate).Path
        }
    }
    return $null
}

function Install-Uv {
    Write-Step "uv not found — installing via Astral installer"
    irm https://astral.sh/uv/install.ps1 | iex
    Import-UvPath
    $uv = Find-UvExecutable
    if (-not $uv) {
        throw "uv install finished but the executable is still not on PATH. Open a new terminal and re-run this script."
    }
    Write-Ok "uv installed at $uv"
    return $uv
}

function Ensure-Uv {
    $uv = Find-UvExecutable
    if ($uv) {
        Write-Ok "uv found at $uv"
        return $uv
    }
    return Install-Uv
}

function Ensure-Python([string] $UvExe) {
    if ($SkipPython) {
        Write-Warn "Skipping Python install (-SkipPython)"
        return
    }
    Write-Step "Ensuring Python 3.13 via uv"
    & $UvExe python install 3.13
    if ($LASTEXITCODE -ne 0) {
        throw "uv python install 3.13 failed with exit code $LASTEXITCODE"
    }
    Write-Ok "Python 3.13 ready"
}

function Install-SentinelAiTool([string] $UvExe, [string] $InstallSource) {
    Write-Step "Installing sentinel-ai CLI from $InstallSource"
    if (Test-Path $InstallSource) {
        $resolved = (Resolve-Path $InstallSource).Path
        & $UvExe tool install --force --from $resolved sentinel-ai
    }
    else {
        & $UvExe tool install --force sentinel-ai --from $InstallSource
    }
    if ($LASTEXITCODE -ne 0) {
        throw "uv tool install sentinel-ai failed with exit code $LASTEXITCODE"
    }
    Import-UvPath
    $tool = Get-Command sentinel-ai -ErrorAction SilentlyContinue
    if (-not $tool) {
        $toolDir = Join-Path $env:USERPROFILE ".local\bin"
        throw "sentinel-ai was installed but is not on PATH yet. Add $toolDir to PATH or open a new terminal."
    }
    Write-Ok "sentinel-ai installed at $($tool.Source)"
}

function Install-RepoHook([string] $RepoPath) {
    if (-not (Test-Path $RepoPath)) {
        throw "Repository path not found: $RepoPath"
    }
    Write-Step "Installing Husky pre-commit hook in $RepoPath"
    Push-Location $RepoPath
    try {
        & sentinel-ai install-hook
        if ($LASTEXITCODE -ne 0) {
            throw "sentinel-ai install-hook failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
    Write-Ok "Hook installed in $RepoPath"
}

# --- main ---

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptRoot
if (-not $Source) {
    $Source = $repoRoot
}

$exampleConfig = Join-Path $repoRoot "sentinel.toml.example"
$localConfig = Join-Path $repoRoot "sentinel.toml"

Write-Host ""
Write-Host "Sentinel-AI installer" -ForegroundColor White
Write-Host ""

if (-not (Test-Path $exampleConfig)) {
    throw "Template not found: $exampleConfig"
}
if (-not (Test-Path $localConfig)) {
    Copy-Item -Path $exampleConfig -Destination $localConfig
    Write-Ok "Created sentinel.toml from sentinel.toml.example"
    Write-Warn "Edit sentinel.toml with your AI server settings, then re-run this script"
}
Write-Ok "Config: $localConfig"

Write-Step "Syncing config into package for install"
& (Join-Path $scriptRoot "sync-config.ps1") -RepoRoot $repoRoot

$uvExe = Ensure-Uv
Ensure-Python -UvExe $uvExe
Install-SentinelAiTool -UvExe $uvExe -InstallSource $Source

if ($Repo) {
    Install-RepoHook -RepoPath $Repo
}

Write-Host ""
Write-Ok "Done. Verify with: sentinel-ai doctor"
if (-not $Repo) {
    Write-Warn "Next: .\scripts\install.ps1 -Repo D:\path\to\your-project"
}
Write-Host ""
