#Requires -Version 5.1
<#
.SYNOPSIS
  Copy sentinel.toml.example into the package tree for wheel builds.

.DESCRIPTION
  uv_build only ships files under src/sentinel_ai/. The bundled defaults come
  from sentinel.toml.example; host overrides live in ~/.sentinel-ai/config.toml.
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

$source = Join-Path $RepoRoot "sentinel.toml.example"
$target = Join-Path $RepoRoot "src\sentinel_ai\sentinel.toml"

if (-not (Test-Path $source)) {
    throw "Template not found: $source"
}

$targetDir = Split-Path -Parent $target
if (-not (Test-Path $targetDir)) {
    New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
}

Copy-Item -Path $source -Destination $target -Force
Write-Host "Synced $source -> $target"
