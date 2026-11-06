# 初始化本地 Git 仓库（安装 Git 后运行）
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts/git-init.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/git-init.ps1 -Commit
param(
    [switch]$Commit,
    [string]$Message = 'feat: 电商售后智能处理系统 v2.0.0'
)

$ErrorActionPreference = 'Stop'

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw '未找到 git，请先安装 Git for Windows（https://git-scm.com/download/win）后重开终端'
}

$root = Split-Path $PSScriptRoot -Parent
Push-Location $root
try {
    Write-Host '== 1/4 上传前自检 =='
    python tools/preflight_secrets.py
    if ($LASTEXITCODE -ne 0) { throw '自检未通过，已中止' }

    Write-Host '== 2/4 初始化仓库 =='
    if (-not (Test-Path '.git')) {
        git init | Out-Host
        git branch -M main
    }

    Write-Host '== 3/4 暂存并检查 =='
    git add -A
    $staged = @(git diff --cached --name-only)
    if ($staged.Count -eq 0) {
        Write-Host '没有需要提交的变更'
        return
    }

    $blocked = $staged | Where-Object {
        $_ -match '(^|/)\.env$' -or
        $_ -match '\.(db|sqlite|sqlite3|ifpkg|pem|key|p12|jks)$' -or
        $_ -eq 'dify_app_dsl.v1.backup.yml'
    }
    if ($blocked) {
        $joined = $blocked -join "`n"
        throw "检测到敏感文件被暂存，请检查 .gitignore：`n$joined"
    }

    git status --short | Out-Host

    Write-Host '== 4/4 提交 =='
    if ($Commit) {
        git commit -m $Message | Out-Host
        Write-Host '本地提交完成。下一步：'
        Write-Host '  git remote add origin git@github.com:aibiyaqi/ecom-aftersale-agent.git'
        Write-Host '  git push -u origin main'
        Write-Host '  git tag v2.0.0; git push origin v2.0.0'
    }
    else {
        Write-Host '已暂存但未提交。确认无误后执行：'
        Write-Host ('  git commit -m "' + $Message + '"')
    }
}
finally {
    Pop-Location
}
