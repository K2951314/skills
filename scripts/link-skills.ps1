# 把本仓库挂载到 WorkBuddy AI 的技能扫描目录（整仓一个 junction）
# 用法：powershell -ExecutionPolicy Bypass -File scripts\link-skills.ps1
# 可选参数：-TargetRoot 指定其他工具的 skills 目录

param(
    [string]$TargetRoot = (Join-Path $env:USERPROFILE ".workbuddy-ai\skills")
)

$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
$name = Split-Path -Leaf $repo
$target = Join-Path $TargetRoot $name

if (-not (Test-Path $TargetRoot)) {
    New-Item -ItemType Directory -Force -Path $TargetRoot | Out-Null
}

if (Test-Path $target) {
    Write-Host "[跳过] 已存在：$target" -ForegroundColor Yellow
} else {
    New-Item -ItemType Junction -Path $target -Target $repo | Out-Null
    Write-Host "[完成] 已挂载" -ForegroundColor Green
    Write-Host "       链接：$target"
    Write-Host "       指向：$repo"
}

Write-Host ""
Write-Host "本仓库已注册的技能：" -ForegroundColor Cyan
$found = $false
Get-ChildItem -Path $repo -Directory | ForEach-Object {
    if (Test-Path (Join-Path $_.FullName 'SKILL.md')) {
        Write-Host "  - $($_.Name)"
        $script:found = $true
    }
}
if (-not $found) { Write-Host "  （暂无，按 README 新增技能后无需重新挂载）" }

Write-Host ""
Write-Host "提示：新增技能后无需重新挂载；改过 description 需新开会话才生效。" -ForegroundColor DarkGray
