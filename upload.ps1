#Requires -Version 5.1
<#
.SYNOPSIS
    一键上传当前项目到 GitHub。

.DESCRIPTION
    自动完成：检测改动 -> 敏感文件拦截 -> 暂存 -> 提交 -> 推送。
    不保存任何账号密码 / Token，推送凭据由 Git Credential Manager 负责。

.PARAMETER Message
    提交说明。不传则交互询问，直接回车使用时间戳默认值。

.PARAMETER Yes
    跳过“确认推送”这一步，用于自动化调用。

.EXAMPLE
    .\upload.ps1
.EXAMPLE
    .\upload.ps1 "修复 Cookie 上传重复的问题"
.EXAMPLE
    .\upload.ps1 -Message "同步扩展改动" -Yes
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]] $Message,

    [switch] $Yes
)

# 不用 'Stop'：git 会把正常进度信息写到 stderr，
# 在 PowerShell 5.1 下会被当成终止错误。这里统一靠 $LASTEXITCODE 判断成败。
$ErrorActionPreference = 'Continue'

# ============================================================
# 常量
# ============================================================

$RemoteName = 'origin'
$RemoteUrl  = 'https://github.com/baideji521/a.git'

# 一旦这些路径被暂存，立刻中止推送。
# .gitignore 是第一道防线，这里是第二道。
$BlockPatterns = @(
    'config.json',
    'cookies/',
    'browser_ua.txt',
    '_all_cookies.txt',
    '.cookie',
    '.cookies',
    'logs/',
    '.env',
    'id_rsa'
)

# ============================================================
# 输出辅助
# ============================================================

function Write-Step  { param($t) Write-Host "==> $t"   -ForegroundColor Cyan }
function Write-Ok    { param($t) Write-Host "    $t"   -ForegroundColor Green }
function Write-Warn2 { param($t) Write-Host "    $t"   -ForegroundColor Yellow }
function Write-Err2  { param($t) Write-Host "    $t"   -ForegroundColor Red }

function Stop-WithError {
    param([string] $Text)
    Write-Host ''
    Write-Err2 $Text
    Write-Host ''
    if (-not $Yes) { Read-Host '按回车退出' | Out-Null }
    exit 1
}

# 只调用 git，不做字符串拼接，避免命令注入
function Invoke-Git {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]] $GitArgs)
    & git -c core.quotepath=false @GitArgs
}

# ============================================================
# 环境准备
# ============================================================

# 中文路径 + 中文提交说明都依赖 UTF-8，这里强制统一
try {
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [Console]::OutputEncoding = $utf8
    $OutputEncoding = $utf8
} catch {
    # 某些宿主（如 ISE）不允许改，忽略即可
}

$scriptRoot = $PSScriptRoot

if ([string]::IsNullOrWhiteSpace($scriptRoot)) {
    $scriptRoot = (Get-Location).Path
}

Set-Location -LiteralPath $scriptRoot

Write-Host ''
Write-Host '============================================' -ForegroundColor DarkGray
Write-Host ' 一键上传到 GitHub' -ForegroundColor White
Write-Host " 目录: $PSScriptRoot"
Write-Host " 远端: $RemoteUrl"
Write-Host '============================================' -ForegroundColor DarkGray
Write-Host ''

# ---------- 1. 检查 git ----------

Write-Step '检查 Git'

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Stop-WithError '找不到 git 命令，请先安装 Git for Windows: https://git-scm.com/download/win'
}

Write-Ok (& git --version)

# ---------- 2. 检查仓库 ----------

Write-Step '检查仓库'

& git rev-parse --is-inside-work-tree *> $null

if ($LASTEXITCODE -ne 0) {
    Write-Warn2 '当前目录还不是 Git 仓库，正在初始化…'
    Invoke-Git init | Out-Null
    Invoke-Git checkout -B main | Out-Null
    Write-Ok '已初始化并切到 main 分支'
}

$branch = (Invoke-Git rev-parse --abbrev-ref HEAD).Trim()

if ($branch -eq 'HEAD' -or [string]::IsNullOrWhiteSpace($branch)) {
    Stop-WithError '当前处于游离 HEAD 状态，请先 git checkout 到一个分支再运行。'
}

Write-Ok "当前分支: $branch"

# ---------- 3. 检查远端 ----------

Write-Step "检查远端 $RemoteName"

$existingUrl = (& git remote get-url $RemoteName 2>$null)

if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($existingUrl)) {
    Invoke-Git remote add $RemoteName $RemoteUrl | Out-Null
    Write-Ok "已添加 $RemoteName -> $RemoteUrl"
} else {
    $existingUrl = $existingUrl.Trim()
    if ($existingUrl -ne $RemoteUrl) {
        Write-Warn2 "远端地址与脚本预期不一致，保留现有配置不做修改："
        Write-Warn2 "  现有: $existingUrl"
        Write-Warn2 "  预期: $RemoteUrl"
    } else {
        Write-Ok $existingUrl
    }
}

# ---------- 4. 检测改动 ----------

Write-Step '检测改动'

$changes = Invoke-Git status --porcelain

if (-not $changes) {
    Write-Host ''
    Write-Ok '工作区干净，没有需要上传的改动。'
    Write-Host ''
    if (-not $Yes) { Read-Host '按回车退出' | Out-Null }
    exit 0
}

Invoke-Git status --short
Write-Host ''

# ---------- 5. 暂存 ----------

Write-Step '暂存改动'

Invoke-Git add -A

$staged = @(Invoke-Git diff --cached --name-only)

if ($staged.Count -eq 0) {
    Write-Host ''
    Write-Ok '没有内容进入暂存区（改动可能全部被 .gitignore 忽略）。'
    Write-Host ''
    if (-not $Yes) { Read-Host '按回车退出' | Out-Null }
    exit 0
}

Write-Ok "共 $($staged.Count) 个文件"

# ---------- 6. 敏感文件拦截 ----------

Write-Step '敏感文件检查'

$hits = @()

foreach ($file in $staged) {
    foreach ($pattern in $BlockPatterns) {
        if ($file.ToLowerInvariant().Contains($pattern.ToLowerInvariant())) {
            $hits += $file
            break
        }
    }
}

if ($hits.Count -gt 0) {
    Write-Host ''
    Write-Err2 '检测到疑似敏感文件进入了暂存区，已中止上传：'
    $hits | Sort-Object -Unique | ForEach-Object { Write-Err2 "  $_" }
    Write-Host ''
    Write-Warn2 '这些文件可能含有 Cookie / Token / 本地凭据。'
    Write-Warn2 '请先补充 .gitignore，然后执行：'
    Write-Warn2 '  git rm --cached <文件>'
    Invoke-Git reset | Out-Null
    Stop-WithError '暂存区已回滚，未做任何提交。'
}

Write-Ok '未发现敏感文件'

# ---------- 7. 提交说明 ----------

$commitMessage = ''

if ($Message -and $Message.Count -gt 0) {
    $commitMessage = ($Message -join ' ').Trim()
}

if ([string]::IsNullOrWhiteSpace($commitMessage)) {
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm'
    $default = "更新: $stamp"

    if ($Yes) {
        $commitMessage = $default
    } else {
        Write-Host ''
        $typed = Read-Host "提交说明（直接回车 = $default）"
        if ([string]::IsNullOrWhiteSpace($typed)) {
            $commitMessage = $default
        } else {
            $commitMessage = $typed.Trim()
        }
    }
}

# ---------- 8. 确认 ----------

if (-not $Yes) {
    Write-Host ''
    Write-Host "即将提交并推送到 $RemoteName/$branch" -ForegroundColor White
    Write-Host "提交说明: $commitMessage"
    $answer = Read-Host '确认？(Y/n)'
    if ($answer -and $answer.Trim().ToLowerInvariant() -notin @('y', 'yes')) {
        Invoke-Git reset | Out-Null
        Write-Host ''
        Write-Warn2 '已取消，暂存区已回滚。'
        Write-Host ''
        exit 0
    }
}

# ---------- 9. 提交 ----------

Write-Host ''
Write-Step '提交'

Invoke-Git commit -m $commitMessage

if ($LASTEXITCODE -ne 0) {
    Stop-WithError 'git commit 失败，请查看上方输出。'
}

# ---------- 10. 推送 ----------

Write-Step "推送到 $RemoteName/$branch"

Invoke-Git push -u $RemoteName $branch

if ($LASTEXITCODE -ne 0) {
    Write-Host ''
    Write-Err2 '推送失败。改动已经提交到本地，仓库没有损坏。'
    Write-Host ''
    Write-Warn2 '常见原因与处理：'
    Write-Warn2 "  1) 远端有新提交       -> git pull --rebase $RemoteName $branch"
    Write-Warn2 '  2) 凭据过期或未登录   -> 重新运行本脚本，在弹窗中登录 GitHub'
    Write-Warn2 '  3) 没有该仓库写权限   -> 确认账号对 baideji521/a 有写权限'
    Stop-WithError '推送未完成。'
}

Write-Host ''
Write-Host '============================================' -ForegroundColor DarkGray
Write-Ok '上传完成'
Write-Host "  分支: $branch"
Write-Host "  说明: $commitMessage"
Write-Host "  地址: https://github.com/baideji521/a"
Write-Host '============================================' -ForegroundColor DarkGray
Write-Host ''

if (-not $Yes) { Read-Host '按回车退出' | Out-Null }
