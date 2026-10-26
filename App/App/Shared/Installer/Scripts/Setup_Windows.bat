@echo off
setlocal
cd /d "%~dp0"

set "SHOW_HELP="
if "%~1"=="--help" set "SHOW_HELP=1"
if "%~1"=="-h" set "SHOW_HELP=1"
if "%~1"=="/?" set "SHOW_HELP=1"
if defined SHOW_HELP (
    echo Usage: Setup_Windows.bat
    echo.
    echo Sets up ClipEm in this folder, or repairs/updates it: fetches Python if
    echo it's missing ^(checked against its published checksum^), then runs
    echo ClipEm's setup, which fetches OBS and ffmpeg and configures them.
    echo Safe to run again to change settings.
    echo.
    echo   -h, --help, /?  Show this help and exit.
    exit /b 0
)

set "PYTHON_DIR=%~dp0App\Windows\Python"
set "PYTHON_EXE=%PYTHON_DIR%\python.exe"
rem python-build-standalone's Windows Python and its sha256 - must match
rem WINDOWS_PYTHON_VERSION/_PBS_TAG in setup.py and Dev/release/upstream-pins.json
rem (release.sh checks).
set "PYTHON_URL=https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.12.16+20261026-x86_64-pc-windows-msvc-install_only.tar.gz"
set "PYTHON_SHA256=590dfcbe3a34c07301e6332e13e2188b98655cd072b08afa45a7998dc4c22bc5"

if not exist "%PYTHON_EXE%" (
    echo Python not found - fetching it ^(one-time, ~45MB, stays inside App\Windows\Python^)...
    if not exist "%PYTHON_DIR%" mkdir "%PYTHON_DIR%"
    powershell -NoProfile -Command ^
        "$ProgressPreference = 'SilentlyContinue';" ^
        "Invoke-WebRequest -Uri '%PYTHON_URL%' -OutFile '%TEMP%\clipem-python.tar.gz';" ^
        "if ((Get-FileHash '%TEMP%\clipem-python.tar.gz' -Algorithm SHA256).Hash -ne '%PYTHON_SHA256%') { Remove-Item '%TEMP%\clipem-python.tar.gz'; Write-Host 'Checksum mismatch for the Python download - aborting.'; exit 1 };" ^
        "& (Join-Path $env:SystemRoot 'System32\tar.exe') -xzf '%TEMP%\clipem-python.tar.gz' -C '%PYTHON_DIR%' --strip-components=1 --exclude '*.pdb';" ^
        "Remove-Item '%TEMP%\clipem-python.tar.gz'"
    if not exist "%PYTHON_EXE%" (
        echo Failed to fetch Python. Check your internet connection and try again.
        pause
        exit /b 1
    )
)

"%PYTHON_EXE%" "%~dp0App\Shared\Installer\setup.py"
pause
