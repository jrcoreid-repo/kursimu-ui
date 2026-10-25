$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)
py -3.12 -m pip install --upgrade pip
py -3.12 -m pip install -r requirements.txt "pyinstaller>=6.10,<7"
$argsList = @("--noconfirm", "--clean", "--onedir", "--windowed", "--name", "KursiMu", "--icon", "assets/kursimu.ico", "--add-data", "assets/kursimu.ico;assets", "--collect-all", "dearpygui", "--collect-all", "pygame", "--collect-submodules", "bleak.backends.winrt", "kursimu_gui_v2.py")
py -3.12 -m PyInstaller @argsList
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }
New-Item -ItemType Directory -Force -Path "dist/KursiMu/music", "dist/KursiMu/logos" | Out-Null
Copy-Item -Path "music/README.md" -Destination "dist/KursiMu/music/README.md" -ErrorAction SilentlyContinue
Copy-Item -Path "logos/README.md" -Destination "dist/KursiMu/logos/README.md" -ErrorAction SilentlyContinue
Write-Output "Built: dist/KursiMu/KursiMu.exe"
