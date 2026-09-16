# 一键启动文献下载器（网页版）
# 用法：pwsh -File .\scripts\start.ps1 [-Port 8765]

param(
    [int]$Port = 8765,
    [string]$Config = ""
)

$ErrorActionPreference = "Stop"
$workspace = Split-Path -Parent $PSScriptRoot
Set-Location $workspace

Write-Host "工作区：$workspace"

$python = "python"
try {
    & $python --version | Out-Null
} catch {
    Write-Host "找不到 python，请先安装 Python 3.10+ 并加入 PATH。" -ForegroundColor Red
    exit 1
}

Write-Host "检查依赖（fastapi / uvicorn / requests）…"
& $python -c "import fastapi, uvicorn, requests" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "缺少依赖，正在安装 requirements.txt …" -ForegroundColor Yellow
    & $python -m pip install -r requirements.txt
}

Write-Host "期刊白名单文件：$(if ($Config) { $Config } else { Join-Path $workspace 'config.json' })"
Write-Host "提示：修改白名单后无需重启，网页里点『重载配置』或直接重新检索即可生效。" -ForegroundColor Cyan

$args = @("-m", "app.server", "--port", "$Port")
if ($Config) { $args += @("--config", $Config) }
& $python @args
