@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=%~dp0App\Windows\Python\python.exe"

if not exist "%PYTHON_EXE%" (
    set "PYTHON_EXE="
    for /r "%~dp0App\Windows\Python" %%i in (python.exe) do if not defined PYTHON_EXE set "PYTHON_EXE=%%i"
)

if not defined PYTHON_EXE (
    where python >nul 2>nul && set "PYTHON_EXE=python"
)

if not defined PYTHON_EXE (
    echo No Python interpreter found - run Setup_Windows.bat ^(or App\Setup-Uninstall_Scripts\Setup_Windows.bat^) at least once first.
    pause
    exit /b 1
)

"%PYTHON_EXE%" "%~dp0App\Shared\Installer\uninstall_autostart.py"
pause
