$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$builtExe = Join-Path $PSScriptRoot 'dist\Codex Switch.exe'
if (-not (Test-Path -LiteralPath $builtExe)) {
    throw '没有找到 dist\Codex Switch.exe，请先运行 build-windows.ps1。'
}

$installDir = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Programs\Codex Switch'
New-Item -ItemType Directory -Force -Path $installDir | Out-Null
$target = Join-Path $installDir 'Codex Switch.exe'
Copy-Item -LiteralPath $builtExe -Destination $target -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'LICENSE') -Destination (Join-Path $installDir 'LICENSE.txt') -Force

$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop 'Codex Switch.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $target
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.IconLocation = "$target,0"
$shortcut.Description = '在 Windows 与 WSL2 中切换 Codex 连接服务'
$shortcut.Save()

Write-Host "软件已安装：$target"
Write-Host "桌面快捷方式已创建：$shortcutPath"
