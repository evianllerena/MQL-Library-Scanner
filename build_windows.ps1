$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

Write-Host '=== MQL Indicator Library Beta 0.2 Windows Build ===' -ForegroundColor Cyan

if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw 'Python 3.11+ is required on the BUILD machine.' }
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw 'Node.js 20+ is required on the BUILD machine.' }
if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) { throw 'Rust 1.77.2+ is required on the BUILD machine.' }

python -m pip install --upgrade pip
python -m pip install -r "$Root\engine\requirements-build.txt"

$PyDist = "$Root\build-sidecar"
if (Test-Path $PyDist) { Remove-Item $PyDist -Recurse -Force }
python -m PyInstaller --noconfirm --clean --onefile --name mql-engine --distpath $PyDist --workpath "$Root\build-pyinstaller" --specpath "$Root\build-pyinstaller" "$Root\engine\engine.py"

$BinDir = "$Root\src-tauri\binaries"
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
Copy-Item "$PyDist\mql-engine.exe" "$BinDir\mql-engine-x86_64-pc-windows-msvc.exe" -Force

npm install
npm run tauri build

Write-Host ''
Write-Host 'Build complete.' -ForegroundColor Green
Write-Host "Installer: $Root\src-tauri\target\release\bundle\nsis\" -ForegroundColor Green
