@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_DIR=%~dp0App\Windows\Python"
set "PYTHON_EXE=%PYTHON_DIR%\python.exe"
rem sha256 of python-3.12.10-embed-amd64.zip - must match Dev/release/upstream-pins.json
rem (and WINDOWS_PYTHON_VERSION in setup.py); CI checks the two agree.
set "PYTHON_SHA256=4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"

if not exist "%PYTHON_EXE%" (
    echo Python not found - fetching the embeddable distribution ^(one-time, ~10MB, stays inside App\Windows\Python^)...
    powershell -NoProfile -Command ^
        "$ProgressPreference = 'SilentlyContinue';" ^
        "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip' -OutFile '%TEMP%\python-embed.zip';" ^
        "if ((Get-FileHash '%TEMP%\python-embed.zip' -Algorithm SHA256).Hash -ne '%PYTHON_SHA256%') { Remove-Item '%TEMP%\python-embed.zip'; Write-Host 'Checksum mismatch for the Python download - aborting.'; exit 1 };" ^
        "Expand-Archive -Path '%TEMP%\python-embed.zip' -DestinationPath '%PYTHON_DIR%' -Force;" ^
        "Remove-Item '%TEMP%\python-embed.zip'"
    if not exist "%PYTHON_EXE%" (
        echo Failed to fetch Python. Check your internet connection and try again.
        pause
        exit /b 1
    )
)

"%PYTHON_EXE%" "%~dp0App\Shared\Installer\setup.py"
pause
