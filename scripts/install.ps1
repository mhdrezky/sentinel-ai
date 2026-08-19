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
#>
[CmdletBinding()]
param(
    [string] $Source
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$CONFIG_DIR = "$env:USERPROFILE\.sentinel-ai"
$CONFIG_FILE = "$CONFIG_DIR\config.toml"
$GITHUB_REPO = "mhdrezky/sentinel-ai"
$RELEASES_API = "https://api.github.com/repos/$GITHUB_REPO/releases/latest"

function Write-Step([string] $Msg) { Write-Host " ==> $Msg" -ForegroundColor Cyan }
function Write-Ok([string] $Msg)   { Write-Host " ok  $Msg" -ForegroundColor Green }
function Write-Warn([string] $Msg){ Write-Host " !   $Msg" -ForegroundColor Yellow }

function Write-Utf8NoBom([string] $Path, [string] $Content) {
    # Set-Content -Encoding utf8 emits a BOM on Windows PowerShell 5.1, and Python's
    # tomllib rejects the leading U+FEFF with "Invalid statement (at line 1, column 1)".
    if (-not $Content.EndsWith("`n")) { $Content += "`r`n" }
    [System.IO.File]::WriteAllText($Path, $Content, (New-Object System.Text.UTF8Encoding($false)))
}

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
            $toml = (Invoke-WebRequest -Uri $tomlUrl -UseBasicParsing).Content
            if (-not $toml -or -not ($toml -match '(?m)^\s*\[policy\]')) {
                throw "response from $tomlUrl is not a Sentinel-AI config"
            }
            Write-Utf8NoBom -Path $CONFIG_FILE -Content $toml
            Write-Ok "Config fetched from $tomlUrl"
        } catch {
            Write-Warn "Could not fetch sentinel.toml: $_"
            Write-Step "Falling back to default bundled config"
            # keep in sync with src/sentinel_ai/sentinel.toml
            $fallbackToml = @"
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
"@
            Write-Utf8NoBom -Path $CONFIG_FILE -Content $fallbackToml
            Write-Ok "Default config created"
        }
    }
    Write-Warn "Edit $CONFIG_FILE with your AI server settings"
} else {
    Write-Ok "Config already exists at $CONFIG_FILE"
}

# --- 5. Install Trivy (optional; warn on failure) ---
$TRIVY_BIN_DIR = "$CONFIG_DIR\bin"
$TRIVY_EXE = "$TRIVY_BIN_DIR\trivy.exe"
$TRIVY_RELEASES_API = "https://api.github.com/repos/aquasecurity/trivy/releases/latest"

function Get-LatestTrivyRelease {
    $headers = @{ "User-Agent" = "sentinel-ai-installer" }
    $release = Invoke-RestMethod -Uri $TRIVY_RELEASES_API -Headers $headers
    if (-not $release.tag_name) {
        throw "GitHub trivy releases/latest returned no tag_name"
    }
    return @{
        Tag     = $release.tag_name
        Version = $release.tag_name.TrimStart("v")
    }
}

function Update-ConfigTrivyBinaryPath([string] $BinaryPath) {
    if (-not (Test-Path $CONFIG_FILE)) { return }

    $content = [System.IO.File]::ReadAllText($CONFIG_FILE)
    if ($content -notmatch '(?m)^\s*binary_path\s*=') { return }

    $current = "trivy"
    if ($content -match '(?m)^\s*binary_path\s*=\s*"([^"]*)"') {
        $current = $matches[1]
    }

    $sentinelBinPrefix = ($TRIVY_BIN_DIR -replace '\\', '/') + "/"
    $normalizedCurrent = $current -replace '\\', '/'
    if ($current -ne "trivy" -and $normalizedCurrent -notlike "$sentinelBinPrefix*") {
        return
    }

    $newContent = [regex]::Replace(
        $content,
        '(?m)^(\s*binary_path\s*=\s*")[^"]*(")',
        "`${1}$BinaryPath`${2}"
    )
    Write-Utf8NoBom -Path $CONFIG_FILE -Content $newContent
}

function Show-TrivyManualInstallHint {
    Write-Warn "Install Trivy manually from https://github.com/aquasecurity/trivy/releases"
    Write-Warn "Then set [trivy].binary_path in $CONFIG_FILE (see README)"
}

$trivyInstalled = $false
New-Item -ItemType Directory -Path $TRIVY_BIN_DIR -Force | Out-Null

if (Test-Path $TRIVY_EXE) {
    try {
        $null = & $TRIVY_EXE --version 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "Trivy already installed at $TRIVY_EXE"
            $trivyInstalled = $true
        }
    } catch {
        $trivyInstalled = $false
    }
}

if (-not $trivyInstalled) {
    Write-Step "Installing latest Trivy to $TRIVY_BIN_DIR"
    $tmpZip = $null
    $tmpDir = $null
    try {
        $trivyRelease = Get-LatestTrivyRelease
        $asset = "trivy_$($trivyRelease.Version)_windows-64bit.zip"
        $url = "https://github.com/aquasecurity/trivy/releases/download/$($trivyRelease.Tag)/$asset"
        $tmpZip = Join-Path $env:TEMP "sentinel-trivy-$($trivyRelease.Version).zip"
        $tmpDir = Join-Path $env:TEMP ("sentinel-trivy-extract-{0}" -f [guid]::NewGuid().ToString())
        Invoke-WebRequest -Uri $url -OutFile $tmpZip -UseBasicParsing
        Expand-Archive -Path $tmpZip -DestinationPath $tmpDir -Force
        $extracted = Get-ChildItem -Path $tmpDir -Recurse -Filter "trivy.exe" | Select-Object -First 1
        if (-not $extracted) { throw "trivy.exe not found in $asset" }
        Copy-Item -Path $extracted.FullName -Destination $TRIVY_EXE -Force
        $null = & $TRIVY_EXE --version 2>&1
        # No backticks in this message: PowerShell reads them as escapes, and a
        # trailing one would escape the closing quote and swallow the next line.
        if ($LASTEXITCODE -ne 0) { throw "installed binary failed: trivy --version" }
        Write-Ok "Trivy $($trivyRelease.Tag) installed at $TRIVY_EXE"
        $trivyInstalled = $true
    } catch {
        Write-Warn "Could not install Trivy automatically: $_"
        Show-TrivyManualInstallHint
    } finally {
        if ($tmpZip -and (Test-Path $tmpZip)) { Remove-Item $tmpZip -Force -ErrorAction SilentlyContinue }
        if ($tmpDir -and (Test-Path $tmpDir)) { Remove-Item $tmpDir -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

if ($trivyInstalled) {
    $trivyConfigPath = $TRIVY_EXE -replace '\\', '/'
    Update-ConfigTrivyBinaryPath -BinaryPath $trivyConfigPath
}

Write-Host ""
Write-Host "Sentinel-AI installed successfully" -ForegroundColor Green
Write-Host ""
Write-Host "  Verify:  sentinel-ai doctor"
Write-Host "  Config:  sentinel-ai config"
Write-Host ""
