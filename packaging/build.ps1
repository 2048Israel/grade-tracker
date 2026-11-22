# Builds dist\GradeTracker.exe: runs the tests, then PyInstaller (one file, no console window).
#
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1
#
# Needs Python 3.9+ with tkinter and PyInstaller (pip install -r packaging\requirements-build.txt,
# ideally in a virtual environment).
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

python -m unittest discover -s tests
if ($LASTEXITCODE -ne 0) { throw "Tests failed: not building." }

if (-not (Test-Path packaging\icon.ico)) { python packaging\make_icon.py }

python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name GradeTracker `
    --icon packaging\icon.ico `
    --add-data "gradetracker\data;gradetracker\data" `
    --version-file packaging\version_info.txt `
    packaging\launch.pyw
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

Get-Item dist\GradeTracker.exe | Select-Object Name, Length, LastWriteTime
