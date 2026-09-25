$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw '请先运行 setup.ps1 创建 Windows 虚拟环境。'
}

& $python -c 'import PyInstaller' 2>$null
if ($LASTEXITCODE -ne 0) {
    & $python -m pip install -r 'requirements-build.txt'
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 安装失败。' }
}

& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name 'Codex Switch' `
    --icon 'assets\codex-switch.ico' `
    --add-data 'assets\codex-switch.ico;assets' `
    --version-file 'assets\version-info.txt' `
    'windows_app.py'

if ($LASTEXITCODE -ne 0) {
    throw 'Codex Switch Windows 软件打包失败。'
}

Write-Host "已生成：$(Join-Path $PSScriptRoot 'dist\Codex Switch.exe')"
