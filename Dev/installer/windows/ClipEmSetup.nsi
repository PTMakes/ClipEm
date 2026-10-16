; ClipEmSetup.nsi - thin installer stub.
;
; Does not reimplement anything App/Shared/Installer/setup.py already does (OBS
; config, tray widget, autostart, hotkeys, the GUI wizard itself - see
; that file). Its only job is: pick an install folder, run bootstrap.ps1
; to fetch+verify+extract OBS/Python/Shared, then hand off to the portable
; Python it just downloaded to run setup.py - the same hand-off
; Setup_Windows.bat already does for a from-source checkout.
;
; Build with build.sh (not makensis directly): it stages the verification
; files bundled below into build\ and passes -DVERSION=<release version>.
; Requires no third-party plugins - only stock NSIS (MUI2, LogicLib, FileFunc,
; System).

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"

!ifndef VERSION
  !error "Build with build.sh, which passes -DVERSION=<X.Y.Z>"
!endif
!ifndef VI_VERSION
  !error "Build with build.sh, which passes -DVI_VERSION=<X.Y.Z.0>"
!endif

!define PRODUCT_NAME "ClipEm"
!define PUBLISHER "PT Makes"
!define UNINSTALL_REG_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\ClipEm"

Name "${PRODUCT_NAME}"
OutFile "dist\ClipEmSetupWindows.exe"
InstallDir "$LOCALAPPDATA\ClipEm"
RequestExecutionLevel user
Unicode true

VIProductVersion "${VI_VERSION}"
VIAddVersionKey "ProductName" "${PRODUCT_NAME}"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "CompanyName" "${PUBLISHER}"
VIAddVersionKey "FileDescription" "${PRODUCT_NAME} installer"
VIAddVersionKey "LegalCopyright" "${PUBLISHER}"

!define MUI_ABORTWARNING
!define MUI_ICON "ClipEm.ico"
!define MUI_UNICON "ClipEm.ico"

; --- Installer pages ---
!insertmacro MUI_PAGE_WELCOME
!define MUI_PAGE_CUSTOMFUNCTION_LEAVE EnsureClipEmSubfolder
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES

!define MUI_FINISHPAGE_RUN "$INSTDIR\App\Windows\Python\python.exe"
!define MUI_FINISHPAGE_RUN_FUNCTION RunSetupWizard
!define MUI_FINISHPAGE_RUN_TEXT "Continue to the ClipEm setup wizard (OBS configuration, clip folder, hotkeys...)"
!define MUI_PAGE_CUSTOMFUNCTION_SHOW FinishPageShow
!insertmacro MUI_PAGE_FINISH

; --- Uninstaller pages ---
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

; MUI sizes the Run checkbox for a single line (10u), but its text wraps
; to two - double its height so neither line is clipped.
Function FinishPageShow
    System::Call '*(i, i, i, i) p .r0'
    System::Call 'user32::GetWindowRect(p $mui.FinishPage.Run, p r0)'
    System::Call '*$0(i .r1, i .r2, i .r3, i .r4)'
    System::Free $0
    IntOp $3 $3 - $1
    IntOp $4 $4 - $2
    IntOp $4 $4 * 2
    ; SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE
    System::Call 'user32::SetWindowPos(p $mui.FinishPage.Run, p 0, i 0, i 0, i r3, i r4, i 0x16)'
FunctionEnd

; Through cmd so that if setup.py fails before its window comes up, the
; error stays on screen (`|| pause`) instead of flashing past.
Function RunSetupWizard
    SetOutPath "$INSTDIR"
    Exec '"$SYSDIR\cmd.exe" /c ""$INSTDIR\App\Windows\Python\python.exe" "$INSTDIR\App\Shared\Installer\setup.py" || pause"'
FunctionEnd

; Always install into a folder of our own. If a user picks an existing
; folder (Documents, Desktop...), ClipEm goes into a ClipEm subfolder of it
; instead of mixing its files - and the uninstaller - into theirs.
Function EnsureClipEmSubfolder
    ${GetFileName} "$INSTDIR" $0
    ${If} $0 != "ClipEm"
        StrCpy $INSTDIR "$INSTDIR\ClipEm"
    ${EndIf}
FunctionEnd

Var ProgressBar
Var StatusText
Var StatusOffset

; All of the install runs in DoInstall rather than the section itself:
; NSIS advances the progress bar per *section* instruction (never inside
; functions it calls), which made it jump to ~1/8 and then sit there for the
; whole download. Inside a function the bar is ours to drive from
; bootstrap.ps1's reported progress.
Section "Install"
    ; Everything is downloaded, so NSIS can't size it itself (it showed
    ; "0.0 KB required"). Unpacked sizes as of OBS 32.2.2 / ffmpeg 9.0.2:
    ; OBS ~390 MB (its ~76 MB of .pdb debug symbols aren't extracted), ffmpeg ~10 MB, embeddable Python ~17 MB, Shared <1 MB,
    ; Tcl/Tk (bundled) ~10 MB, plus headroom for the pip packages setup.py
    ; adds later.
    ; Revisit when bumping OBS.
    AddSize 471040 ; KB - shown as 460 MB (NSIS counts 1 MB = 1024 KB)
    Call DoInstall
SectionEnd

Function DoInstall
    SetOutPath "$INSTDIR"

    ; Verification inputs live in the installer's own temp folder, not in
    ; $INSTDIR, so nothing is left behind if the download step fails.
    InitPluginsDir
    File "/oname=$PLUGINSDIR\bootstrap.ps1" "bootstrap.ps1"
    File "/oname=$PLUGINSDIR\ed25519_verify.py" "build\ed25519_verify.py"
    File "/oname=$PLUGINSDIR\signing_public_key.txt" "build\signing_public_key.txt"
    File "/oname=$PLUGINSDIR\bootstrap-pins.json" "build\bootstrap-pins.json"
    File "/oname=$PLUGINSDIR\tcltk.zip" "build\tcltk.zip"
    File "/oname=$PLUGINSDIR\vcruntime.zip" "build\vcruntime.zip"

    FindWindow $0 "#32770" "" $HWNDPARENT
    GetDlgItem $ProgressBar $0 1004
    GetDlgItem $StatusText $0 1006
    SendMessage $ProgressBar ${PBM_SETPOS} 0 0
    StrCpy $StatusOffset 0

    DetailPrint "Downloading and verifying OBS, a portable Python, and ClipEm's files..."
    ; Exit code 3: files in $INSTDIR are in use (OBS or ClipEm is running from
    ; it - see bootstrap.ps1's Assert-InstallNotInUse). Offer to retry once
    ; they've been closed.
    ${Do}
        Call RunBootstrap
        ${If} $0 != 3
            ${Break}
        ${EndIf}
        StrCpy $R9 "cancel"
        MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "OBS or ClipEm is still running from $INSTDIR, so its files can't be replaced.$\r$\n$\r$\nClose OBS, and quit ClipEm from its tray icon (Quit > Quit ClipEm + OBS), then click Retry." IDCANCEL +2
        StrCpy $R9 "retry"
        ${If} $R9 != "retry"
            ${Break}
        ${EndIf}
        SendMessage $ProgressBar ${PBM_SETPOS} 0 0
    ${Loop}
    ${If} $0 = 3
        RMDir "$INSTDIR" ; only removed if still empty
        Abort
    ${ElseIf} $0 != 0
        MessageBox MB_OK|MB_ICONSTOP "ClipEm setup couldn't finish installing (exit code $0). Click Show details for the reason - usually no internet connection. Then run the installer again."
        RMDir "$INSTDIR" ; only removed if still empty
        Abort
    ${EndIf}

    WriteUninstaller "$INSTDIR\Uninstall.exe"
    ${GetSize} "$INSTDIR" "/S=0K" $1 $2 $3
    WriteRegStr HKCU "${UNINSTALL_REG_KEY}" "DisplayName" "${PRODUCT_NAME}"
    WriteRegStr HKCU "${UNINSTALL_REG_KEY}" "DisplayVersion" "${VERSION}"
    WriteRegStr HKCU "${UNINSTALL_REG_KEY}" "Publisher" "${PUBLISHER}"
    WriteRegStr HKCU "${UNINSTALL_REG_KEY}" "DisplayIcon" "$INSTDIR\App\Shared\tray-widget\ClipEm.ico"
    WriteRegStr HKCU "${UNINSTALL_REG_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
    WriteRegStr HKCU "${UNINSTALL_REG_KEY}" "InstallLocation" "$INSTDIR"
    WriteRegDWORD HKCU "${UNINSTALL_REG_KEY}" "EstimatedSize" $1
    WriteRegDWORD HKCU "${UNINSTALL_REG_KEY}" "NoModify" 1
    WriteRegDWORD HKCU "${UNINSTALL_REG_KEY}" "NoRepair" 1
FunctionEnd

; Runs bootstrap.ps1 without a console window and, until it exits, relays
; its status file to the progress bar and details list (ShowBootstrapStatus).
; nsExec would block until the end, so this is CreateProcess + a wait loop
; via the stock System plugin. Returns the exit code in $0.
Function RunBootstrap
    StrCpy $3 '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\bootstrap.ps1" -InstallDir "$INSTDIR" -BundleDir "$PLUGINSDIR" -StatusFile "$PLUGINSDIR\bootstrap-status.txt"'
    System::Call '*(&l4, p, p, p, i, i, i, i, i, i, i, i, &i2, &i2, p, p, p, p) p .r1' ; STARTUPINFOW
    System::Call '*(p, p, i, i) p .r2' ; PROCESS_INFORMATION
    ; 0x08000000 = CREATE_NO_WINDOW
    System::Call 'kernel32::CreateProcessW(p 0, w r3, p 0, p 0, i 0, i 0x08000000, p 0, p 0, p r1, p r2) i .r0'
    System::Call '*$2(p .r4, p .r5)'
    System::Free $1
    System::Free $2
    ${If} $0 = 0
        DetailPrint "Couldn't start PowerShell."
        StrCpy $0 -1
        Return
    ${EndIf}
    System::Call 'kernel32::CloseHandle(p r5)'

    ${Do}
        System::Call 'kernel32::WaitForSingleObject(p r4, i 200) i .r0'
        Call ShowBootstrapStatus
    ${LoopWhile} $0 = 258 ; WAIT_TIMEOUT

    System::Call 'kernel32::GetExitCodeProcess(p r4, *i .r0)'
    System::Call 'kernel32::CloseHandle(p r4)'
FunctionEnd

; Reads whatever bootstrap.ps1 has appended since last time (see the
; status line format in its header). Uses only $R0-$R2 - RunBootstrap's
; $0/$4 must survive it.
Function ShowBootstrapStatus
    ClearErrors
    FileOpen $R0 "$PLUGINSDIR\bootstrap-status.txt" r
    ${If} ${Errors}
        Return ; not written yet
    ${EndIf}
    FileSeek $R0 $StatusOffset SET
    ${Do}
        ClearErrors
        FileReadUTF16LE $R0 $R1
        ${If} ${Errors}
            ${Break}
        ${EndIf}
        StrCpy $R2 $R1 1 -1
        ${If} $R2 != "$\n"
            ${Break} ; a line still being written - re-read it next time
        ${EndIf}
        FileSeek $R0 0 CUR $StatusOffset
        StrCpy $R1 $R1 -2 ; drop the CRLF
        StrCpy $R2 $R1 1
        StrCpy $R1 $R1 "" 1
        ${If} $R2 == "@"
            IntOp $R1 $R1 * 30 ; 0-1000 -> the bar's 0-30000 range
            SendMessage $ProgressBar ${PBM_SETPOS} $R1 0
        ${ElseIf} $R2 == "~"
            SendMessage $StatusText ${WM_SETTEXT} 0 "STR:$R1"
        ${Else}
            DetailPrint "$R2$R1"
        ${EndIf}
    ${Loop}
    FileClose $R0
FunctionEnd

Section "Uninstall"
    ; ClipEm is otherwise fully portable (see setup.py) - this removes the
    ; boot/login autostart entry setup.py may have created outside $INSTDIR,
    ; via the same uninstall_autostart.py UninstallAutostartWindows.bat
    ; wraps (not the .bat itself - it ends in `pause`, which would hang an
    ; unattended ExecWait here).
    ${If} ${FileExists} "$INSTDIR\App\Windows\Python\python.exe"
        ExecWait '"$INSTDIR\App\Windows\Python\python.exe" "$INSTDIR\App\Shared\Installer\uninstall_autostart.py"'
    ${EndIf}

    ; Only what ClipEm itself put here - the Clips folder (the user's saved
    ; clips) is deliberately kept.
    RMDir /r "$INSTDIR\App"
    Delete "$INSTDIR\ClipEm.lnk"
    Delete "$INSTDIR\UninstallAutostartWindows.bat"
    Delete "$INSTDIR\Uninstall.exe"
    RMDir "$INSTDIR" ; only removed if now empty
    DeleteRegKey HKCU "${UNINSTALL_REG_KEY}"
SectionEnd
