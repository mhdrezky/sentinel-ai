#Requires -Version 5.1
<#
.SYNOPSIS
  Install Sentinel-AI for the current user (zero admin required).

.DESCRIPTION
  Installs sentinel-ai from the latest GitHub release via `uv tool install --from git@tag`.
  Designed to be run via:
    irm https://github.com/mhdrezky/sentinel-ai/releases/latest/download/install.ps1 | iex
  Creates default config from the release's bundled sentinel.toml.

.PARAMETER Source
  Override: path to a local directory containing setup.py or pyproject.toml.
  Skips remote install and installs from local source instead.
.PARAMETER RepoPath
  Path to a git repository to install the Husky pre-commit hook into.
#>
[CmdletBinding()]
param(
    [string] $Source,
    [string] $RepoPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Fallback from environment variable (for remote script piping)
if (-not $RepoPath -and $env:SENTINEL_REPO_PATH) {
    $RepoPath = $env:SENTINEL_REPO_PATH
}

$CONFIG_DIR = "$env:USERPROFILE\.sentinel-ai"
$CONFIG_FILE = "$CONFIG_DIR\config.toml"
$GITHUB_REPO = "mhdrezky/sentinel-ai"
$RELEASES_API = "https://api.github.com/repos/$GITHUB_REPO/releases/latest"

function Write-Step([string] $Msg) { Write-Host " ==> $Msg" -ForegroundColor Cyan }
function Write-Ok([string] $Msg)   { Write-Host " ok  $Msg" -ForegroundColor Green }
function Write-Warn([string] $Msg){ Write-Host " !   $Msg" -ForegroundColor Yellow }

function Get-LatestReleaseTag {
    $headers = @{ "User-Agent" = "sentinel-ai-installer" }
    try {
        $release = Invoke-RestMethod -Uri $RELEASES_API -Headers $headers
    } catch {
        throw "Could not resolve latest release from GitHub API: $_"
    }
    if (-not $release.tag_name) {
        throw "GitHub releases/latest returned no tag_name"
    }
    return $release.tag_name
}

Write-Host ""
Write-Host "Sentinel-AI Installer" -ForegroundColor White
Write-Host ""

# --- 1. Ensure uv ---
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    Write-Step "uv not found — installing via Astral installer"
    $uvInstallPath = "$env:USERPROFILE\.cargo\bin"
    iex ((Invoke-WebRequest -Uri "https://astral.sh/uv/install.ps1" -UseBasicParsing).Content)

    # Astral installer writes to PATH but may not update the current process
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uv) {
        Write-Step "Refreshing PATH to include $($uvInstallPath)"
        $currentPath = [Environment]::GetEnvironmentVariable("Path", "User")
        if (-not ($currentPath -split ';' | Where-Object { $_ -eq $uvInstallPath })) {
            [Environment]::SetEnvironmentVariable("Path", "$uvInstallPath;$currentPath", "User")
            $env:Path = "$uvInstallPath;$currentPath"
        }
        $uv = Get-Command uv -ErrorAction SilentlyContinue
    }
    if (-not $uv) { throw "uv install finished but executable is not on PATH. Start a new terminal and re-run." }
    Write-Ok "uv installed at $($uv.Source)"
}

# --- 2. Install sentinel-ai ---
$releaseTag = $null
if ($Source -and (Test-Path $Source)) {
    Write-Step "Installing from local source: $Source"
    $resolved = (Resolve-Path $Source).Path
    uv tool install --force --from $resolved sentinel-ai
} else {
    $releaseTag = Get-LatestReleaseTag
    Write-Step "Installing from git release $releaseTag"
    uv tool install --force "git+https://github.com/$GITHUB_REPO.git@$releaseTag"
}

if ($LASTEXITCODE -ne 0) { throw "uv tool install failed with exit code $LASTEXITCODE" }

# --- 3. Verify sentinel-ai on PATH ---
$cli = Get-Command sentinel-ai -ErrorAction SilentlyContinue
if (-not $cli) {
    $toolBin = "$env:USERPROFILE\.local\bin"
    if (Test-Path $toolBin) {
        $env:Path = "$toolBin;$env:Path"
        $cli = Get-Command sentinel-ai -ErrorAction SilentlyContinue
    }
}
if (-not $cli) { throw "sentinel-ai binary not found on PATH after install. Start a new terminal and re-run." }
Write-Ok "sentinel-ai installed at $($cli.Source)"

# --- 4. Create host config from package ---
if (-not (Test-Path $CONFIG_FILE)) {
    Write-Step "Creating default config at $CONFIG_FILE"
    New-Item -ItemType Directory -Path $CONFIG_DIR -Force | Out-Null

    $bundledToml = $null
    if ($Source -and (Test-Path $Source)) {
        $candidate = Join-Path (Resolve-Path $Source).Path "src\sentinel_ai\sentinel.toml"
        if (Test-Path $candidate) { $bundledToml = $candidate }
    }

    if ($bundledToml) {
        Copy-Item -Path $bundledToml -Destination $CONFIG_FILE
        Write-Ok "Config copied from local source"
    } else {
        $configTag = if ($releaseTag) { $releaseTag } else { Get-LatestReleaseTag }
        $tomlUrl = "https://raw.githubusercontent.com/$GITHUB_REPO/$configTag/src/sentinel_ai/sentinel.toml"
        try {
            Write-Step "Fetching config from release $configTag"
            $toml = irm $tomlUrl
            $toml | Set-Content -Path $CONFIG_FILE -Encoding utf8
            Write-Ok "Config fetched from $tomlUrl"
        } catch {
            Write-Warn "Could not fetch sentinel.toml: $_"
            Write-Step "Falling back to default bundled config"
            # keep in sync with src/sentinel_ai/sentinel.toml
            @"
# Sentinel-AI configuration
# Edit this file to point [ai].base_url and [ai].model to your AI server.

[policy]
block_at_or_above = "high"
block_on_install_scripts = true
block_on_nonregistry_source = true
allowlist = []
denylist = []

[ai]
enabled = true
base_url = "http://localhost:8000/v1"
model = "local-model"
timeout_seconds = 20.0
max_output_tokens = 2048
fail_open = true
enable_thinking = false

[trivy]
enabled = true
binary_path = "trivy"
timeout_seconds = 60.0
skip_db_update = false
offline = false
"@ | Set-Content -Path $CONFIG_FILE -Encoding utf8
            Write-Ok "Default config created"
        }
    }
    Write-Warn "Edit $CONFIG_FILE with your AI server settings"
} else {
    Write-Ok "Config already exists at $CONFIG_FILE"
}

# --- 5. Post-install hook (optional) ---
if ($RepoPath) {
    if (-not (Test-Path $RepoPath)) { throw "Repository path not found: $RepoPath" }
    Write-Step "Installing Husky pre-commit hook in $RepoPath"
    Push-Location $RepoPath
    try {
        sentinel-ai install-hook
        if ($LASTEXITCODE -ne 0) { throw "install-hook failed with exit code $LASTEXITCODE" }
        Write-Ok "Hook installed"
    } catch {
        Write-Warn "Hook install failed: $_"
    } finally {
        Pop-Location
    }
}

Write-Host ""
Write-Host "Sentinel-AI installed successfully" -ForegroundColor Green
Write-Host ""
Write-Host "  Verify:  sentinel-ai doctor"
Write-Host "  Config:  sentinel-ai config"
Write-Host ""
