param([switch]$Clean = $true)
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "未找到 $python，请先创建虚拟环境并安装 requirements.txt。" }
Set-Location -LiteralPath $projectRoot
& $python -m PyInstaller --clean --noconfirm (Join-Path $projectRoot "NetLab.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败，退出码 $LASTEXITCODE" }
Write-Host "构建完成：$projectRoot\dist\NetLab\NetLab.exe"
