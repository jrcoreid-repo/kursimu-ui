$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)
py -3.12 -m pip install --upgrade pip
py -3.12 -m pip install -r requirements.txt "pyinstaller>=6.10,<7"
$argsList = @("--noconfirm", "--clean", "--onedir", "--windowed", "--name", "Korsimu", "--icon", "assets/kursimu.ico", "--add-data", "assets/kursimu.ico;assets", "--add-data", "logos;logos", "--add-data", "music;music", "--collect-all", "dearpygui", "--collect-all", "pygame", "--collect-submodules", "bleak.backends.winrt", "kursimu_gui_v2.py")
py -3.12 -m PyInstaller @argsList
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }
New-Item -ItemType Directory -Force -Path "dist/Korsimu/music", "dist/Korsimu/logos" | Out-Null
Copy-Item -Path "music/README.md" -Destination "dist/Korsimu/music/README.md" -ErrorAction SilentlyContinue
Copy-Item -Path "logos/README.md" -Destination "dist/Korsimu/logos/README.md" -ErrorAction SilentlyContinue
Write-Output "Built: dist/Korsimu/Korsimu.exe"
