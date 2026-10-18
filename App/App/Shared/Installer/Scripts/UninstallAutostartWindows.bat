@echo off
setlocal
cd /d "%~dp0"

set "SHOW_HELP="
if "%~1"=="--help" set "SHOW_HELP=1"
if "%~1"=="-h" set "SHOW_HELP=1"
if "%~1"=="/?" set "SHOW_HELP=1"
if defined SHOW_HELP (
    echo Usage: UninstallAutostartWindows.bat
    echo.
    echo Removes this ClipEm folder's boot autostart entry and its Start Menu
    echo entry, without running Setup. Entries that start another ClipEm folder
    echo are left alone. Safe to run any time.
    echo.
    echo   -h, --help, /?  Show this help and exit.
    exit /b 0
)

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
