#Requires -Version 5.1
<#
.SYNOPSIS
  Copy root sentinel.toml into the package tree for wheel builds.

.DESCRIPTION
  uv_build only ships files under src/sentinel_ai/. The canonical config lives
  at the project root; this script syncs it before `uv tool install` or `uv build`.
#>
[CmdletBinding()]
param(
    [string] $RepoRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not $RepoRoot) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}

$source = Join-Path $RepoRoot "sentinel.toml"
$target = Join-Path $RepoRoot "src\sentinel_ai\sentinel.toml"

if (-not (Test-Path $source)) {
    throw "Config not found: $source"
}

$targetDir = Split-Path -Parent $target
if (-not (Test-Path $targetDir)) {
    New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
}

Copy-Item -Path $source -Destination $target -Force
Write-Host "Synced $source -> $target"
