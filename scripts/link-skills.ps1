# Mount this repo into the WorkBuddy AI skills scan directory.
# One junction covers the whole repo, so new skills take effect automatically.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\link-skills.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\link-skills.ps1 -TargetRoot "$env:USERPROFILE\.claude\skills"
#
# NOTE: this file is intentionally ASCII-only. Windows PowerShell 5.1 reads
# BOM-less script files as ANSI/GBK, which corrupts non-ASCII characters and
# breaks parsing. Keep it ASCII to stay portable.

param(
    [string]$TargetRoot = (Join-Path $env:USERPROFILE ".workbuddy-ai\skills")
)

$ErrorActionPreference = 'Stop'

$repo   = Split-Path -Parent $PSScriptRoot
$name   = Split-Path -Leaf $repo
$target = Join-Path $TargetRoot $name

if (-not (Test-Path $TargetRoot)) {
    New-Item -ItemType Directory -Force -Path $TargetRoot | Out-Null
}

if (Test-Path $target) {
    Write-Host "[skip] already mounted: $target" -ForegroundColor Yellow
} else {
    New-Item -ItemType Junction -Path $target -Target $repo | Out-Null
    Write-Host "[ok] mounted" -ForegroundColor Green
    Write-Host "     link   : $target"
    Write-Host "     target : $repo"
}

Write-Host ""
Write-Host "Skills registered from this repo:" -ForegroundColor Cyan
$skills = Get-ChildItem -Path $repo -Directory |
    Where-Object { Test-Path (Join-Path $_.FullName 'SKILL.md') }
if ($skills) {
    $skills | ForEach-Object { Write-Host "  - $($_.Name)" }
} else {
    Write-Host "  (none yet - add a skill folder, no re-mount needed)"
}

Write-Host ""
Write-Host "Note: adding a skill needs no re-mount. Changing a description needs a new session." -ForegroundColor DarkGray
