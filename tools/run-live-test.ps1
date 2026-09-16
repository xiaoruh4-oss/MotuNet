$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root "..\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "未找到项目虚拟环境 Python。" }
& $python (Join-Path $root "live_smoke.py") @args
exit $LASTEXITCODE
