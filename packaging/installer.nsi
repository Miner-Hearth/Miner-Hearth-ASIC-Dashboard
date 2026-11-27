; Miner Hearth per-user Windows installer (NSIS 3, Unicode). Build on Linux or Windows:
;   makensis -DVERSION=1.17.1 -DSTAGE=<folder with MinerHearth.exe etc> -DOUTFILE=MinerHearth-Setup-v1.17.1.exe packaging/installer.nsi
; Installs to %LOCALAPPDATA%\Programs\Miner Hearth without admin rights. userdata\, logs\ and config.json
; are never overwritten on upgrade and are kept on uninstall unless the user ticks the delete option.
; v1.17.0 rename: the "Move from the old name" section (ticked by default when an old install exists) removes
; the old program's shortcuts, sign-in entry, uninstall entry and program files, but never its userdata\,
; logs\ or config.json. It records that folder (HKCU\Software\Miner Hearth, LegacyDataDir); Miner Hearth
; copies the data from there on its first start (nk_legacy.py) and leaves the old folder as it was.
Unicode true
SetCompressor /SOLID lzma
RequestExecutionLevel user
ManifestDPIAware true

!ifndef VERSION
  !error "pass -DVERSION=x.y.z"
!endif
!ifndef STAGE
  !error "pass -DSTAGE=<payload folder>"
!endif
!ifndef OUTFILE
  !define OUTFILE "MinerHearth-Setup-v${VERSION}.exe"
!endif
!ifndef VERSION4
  !define VERSION4 "${VERSION}.0"
!endif

!define APPNAME "Miner Hearth"
!define PUBLISHER "Miner Hearth"
!define EXE "MinerHearth.exe"
!define UNINSTKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\MinerHearth"
!define RUNKEY "Software\Microsoft\Windows\CurrentVersion\Run"
!define HEARTHKEY "Software\Miner Hearth"
; the program's name before v1.17.0 (only used to find and tidy up an old install)
!define OLD_APPNAME "Nonce Keeper"
!define OLD_EXE "NonceKeeper.exe"
!define OLD_RUNVALUE "NonceKeeper"
!define OLD_UNINSTKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\NonceKeeper"

Var OldDir

Name "${APPNAME} ${VERSION}"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\Programs\${APPNAME}"
InstallDirRegKey HKCU "${UNINSTKEY}" "InstallLocation"
BrandingText "${APPNAME} ${VERSION}"

VIProductVersion "${VERSION4}"
VIAddVersionKey "ProductName" "${APPNAME}"
VIAddVersionKey "CompanyName" "${PUBLISHER}"
VIAddVersionKey "FileDescription" "${APPNAME} ${VERSION} Setup"
VIAddVersionKey "FileVersion" "${VERSION4}"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "LegalCopyright" "Free to share"

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!include "Sections.nsh"

!define MUI_ICON "${STAGE}\miner_dashboard.ico"
!define MUI_UNICON "${STAGE}\miner_dashboard.ico"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TEXT "This installs ${APPNAME} ${VERSION} for your Windows account only (no administrator rights needed).$\r$\n$\r$\nIf you are upgrading, your machines, settings and logs are kept.$\r$\n$\r$\nClick Next to continue."
!define MUI_FINISHPAGE_RUN "$INSTDIR\${EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "Launch ${APPNAME}"
!define MUI_FINISHPAGE_SHOWREADME "$INSTDIR\INSTALL.txt"
!define MUI_FINISHPAGE_SHOWREADME_TEXT "Show the quick-start guide"
!define MUI_FINISHPAGE_SHOWREADME_NOTCHECKED

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_COMPONENTS
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

Function CloseRunning
  ; Ask before closing a running copy so files can be replaced.
  nsExec::ExecToStack 'cmd /c tasklist /FI "IMAGENAME eq ${EXE}" /NH | find /I "${EXE}"'
  Pop $0
  Pop $1
  ${If} $0 == 0
    MessageBox MB_OKCANCEL|MB_ICONEXCLAMATION "${APPNAME} is running. Click OK to close it and continue, or Cancel to stop." IDOK close
    Abort
    close:
    nsExec::Exec 'taskkill /IM ${EXE}'
    Sleep 4000
    nsExec::Exec 'taskkill /F /IM ${EXE}'
    Sleep 1000
  ${EndIf}
FunctionEnd

Section "${APPNAME} (required)" SecMain
  SectionIn RO
  Call CloseRunning
  SetOutPath "$INSTDIR"
  SetOverwrite on
  File "${STAGE}\${EXE}"
  File "${STAGE}\VERSION"
  File "${STAGE}\README.md"
  File "${STAGE}\INSTALL.txt"
  File "${STAGE}\enable-autostart.bat"
  File "${STAGE}\disable-autostart.bat"
  File "${STAGE}\miner_dashboard.ico"
  ; shipped defaults: only on first install, never over the user's copy
  SetOverwrite off
  File "${STAGE}\config.json"
  SetOverwrite on
  CreateDirectory "$INSTDIR\userdata"
  CreateDirectory "$INSTDIR\logs"

  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayName" "${APPNAME}"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINSTKEY}" "Publisher" "${PUBLISHER}"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayIcon" "$INSTDIR\${EXE},0"
  WriteRegStr HKCU "${UNINSTKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTKEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINSTKEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegStr HKCU "${UNINSTKEY}" "Comments" "Miner dashboard and autotuner for home miners"
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K /G=0" $0 $1 $2
  WriteRegDWORD HKCU "${UNINSTKEY}" "EstimatedSize" $0
SectionEnd

Function FindOld
  StrCpy $OldDir ""
  ReadRegStr $0 HKCU "${OLD_UNINSTKEY}" "InstallLocation"
  ${If} $0 != ""
  ${AndIf} ${FileExists} "$0\*.*"
    StrCpy $OldDir $0
  ${ElseIf} ${FileExists} "$LOCALAPPDATA\Programs\${OLD_APPNAME}\${OLD_EXE}"
    StrCpy $OldDir "$LOCALAPPDATA\Programs\${OLD_APPNAME}"
  ${EndIf}
FunctionEnd

Section "Move from the old name (${OLD_APPNAME})" SecLegacy
  ; close the old program, if it is running, so its files can be removed
  nsExec::ExecToStack 'cmd /c tasklist /FI "IMAGENAME eq ${OLD_EXE}" /NH | find /I "${OLD_EXE}"'
  Pop $0
  Pop $1
  ${If} $0 == 0
    MessageBox MB_OKCANCEL|MB_ICONEXCLAMATION "${OLD_APPNAME} (the old name of ${APPNAME}) is running. Click OK to close it, or Cancel to leave the old program as it is." IDOK closeold
    Goto legacydone
    closeold:
    nsExec::Exec 'taskkill /IM ${OLD_EXE}'
    Sleep 4000
    nsExec::Exec 'taskkill /F /IM ${OLD_EXE}'
    Sleep 1000
  ${EndIf}
  ${If} $OldDir != ""
    ; Miner Hearth copies the data from here on its first start (the folder itself is left alone)
    WriteRegStr HKCU "${HEARTHKEY}" "LegacyDataDir" "$OldDir"
    ; old program files only: userdata\, logs\ and config.json are kept
    Delete "$OldDir\${OLD_EXE}"
    Delete "$OldDir\Uninstall.exe"
    Delete "$OldDir\VERSION"
    Delete "$OldDir\README.md"
    Delete "$OldDir\INSTALL.txt"
    Delete "$OldDir\enable-autostart.bat"
    Delete "$OldDir\disable-autostart.bat"
    Delete "$OldDir\miner_dashboard.ico"
  ${EndIf}
  ReadRegStr $0 HKCU "${RUNKEY}" "${OLD_RUNVALUE}"
  ${If} $0 != ""
    DeleteRegValue HKCU "${RUNKEY}" "${OLD_RUNVALUE}"
    WriteRegStr HKCU "${RUNKEY}" "MinerHearth" '"$INSTDIR\${EXE}"'
  ${EndIf}
  Delete "$SMPROGRAMS\${OLD_APPNAME}\${OLD_APPNAME}.lnk"
  Delete "$SMPROGRAMS\${OLD_APPNAME}\Quick start.lnk"
  Delete "$SMPROGRAMS\${OLD_APPNAME}\Uninstall ${OLD_APPNAME}.lnk"
  RMDir "$SMPROGRAMS\${OLD_APPNAME}"
  Delete "$DESKTOP\${OLD_APPNAME}.lnk"
  DeleteRegKey HKCU "${OLD_UNINSTKEY}"
  legacydone:
SectionEnd

Section "Start Menu shortcut" SecStart
  CreateDirectory "$SMPROGRAMS\${APPNAME}"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk" "$INSTDIR\${EXE}" "" "$INSTDIR\${EXE}" 0
  CreateShortcut "$SMPROGRAMS\${APPNAME}\Quick start.lnk" "$INSTDIR\INSTALL.txt"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\Uninstall ${APPNAME}.lnk" "$INSTDIR\Uninstall.exe"
SectionEnd

Section /o "Desktop shortcut" SecDesktop
  CreateShortcut "$DESKTOP\${APPNAME}.lnk" "$INSTDIR\${EXE}" "" "$INSTDIR\${EXE}" 0
SectionEnd

Section /o "Start ${APPNAME} when I sign in to Windows" SecAutostart
  WriteRegStr HKCU "${RUNKEY}" "MinerHearth" '"$INSTDIR\${EXE}"'
  DeleteRegValue HKCU "${RUNKEY}" "MinerAutotune"
  DeleteRegValue HKCU "${RUNKEY}" "${OLD_RUNVALUE}"
SectionEnd

Function .onInit
  Call FindOld
  ${If} $OldDir == ""
    ; nothing to move: untick the section (it can still be ticked; it then only cleans up leftovers)
    SectionGetFlags ${SecLegacy} $0
    IntOp $0 $0 & ${SECTION_OFF}
    SectionSetFlags ${SecLegacy} $0
  ${EndIf}
FunctionEnd

LangString DESC_Main ${LANG_ENGLISH} "The program. Your machines, settings and logs are kept when upgrading."
LangString DESC_Start ${LANG_ENGLISH} "Add ${APPNAME} to the Start Menu."
LangString DESC_Desktop ${LANG_ENGLISH} "Put a ${APPNAME} shortcut on the desktop."
LangString DESC_Auto ${LANG_ENGLISH} "Start automatically when you sign in (replaces the sign-in entry of an older version)."
LangString DESC_Legacy ${LANG_ENGLISH} "Found an install of ${OLD_APPNAME} (this program's old name)? Removes its shortcuts, sign-in entry, uninstall entry and program files. Its machines, settings and logs are NOT deleted: ${APPNAME} copies them on its first start."
!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecMain} $(DESC_Main)
  !insertmacro MUI_DESCRIPTION_TEXT ${SecLegacy} $(DESC_Legacy)
  !insertmacro MUI_DESCRIPTION_TEXT ${SecStart} $(DESC_Start)
  !insertmacro MUI_DESCRIPTION_TEXT ${SecDesktop} $(DESC_Desktop)
  !insertmacro MUI_DESCRIPTION_TEXT ${SecAutostart} $(DESC_Auto)
!insertmacro MUI_FUNCTION_DESCRIPTION_END

; ---------------- Uninstaller ----------------
Function un.CloseRunning
  nsExec::ExecToStack 'cmd /c tasklist /FI "IMAGENAME eq ${EXE}" /NH | find /I "${EXE}"'
  Pop $0
  Pop $1
  ${If} $0 == 0
    MessageBox MB_OKCANCEL|MB_ICONEXCLAMATION "${APPNAME} is running. Click OK to close it and uninstall, or Cancel to stop." IDOK close
    Abort
    close:
    nsExec::Exec 'taskkill /IM ${EXE}'
    Sleep 4000
    nsExec::Exec 'taskkill /F /IM ${EXE}'
    Sleep 1000
  ${EndIf}
FunctionEnd

Section "un.${APPNAME} program files" UnSecMain
  SectionIn RO
  Call un.CloseRunning
  ; remove the sign-in entry only if it points at this copy
  ReadRegStr $0 HKCU "${RUNKEY}" "MinerHearth"
  ${If} $0 == '"$INSTDIR\${EXE}"'
    DeleteRegValue HKCU "${RUNKEY}" "MinerHearth"
  ${EndIf}
  Delete "$INSTDIR\${EXE}"
  Delete "$INSTDIR\VERSION"
  Delete "$INSTDIR\README.md"
  Delete "$INSTDIR\INSTALL.txt"
  Delete "$INSTDIR\enable-autostart.bat"
  Delete "$INSTDIR\disable-autostart.bat"
  Delete "$INSTDIR\miner_dashboard.ico"
  Delete "$INSTDIR\Uninstall.exe"
  Delete "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk"
  Delete "$SMPROGRAMS\${APPNAME}\Quick start.lnk"
  Delete "$SMPROGRAMS\${APPNAME}\Uninstall ${APPNAME}.lnk"
  RMDir "$SMPROGRAMS\${APPNAME}"
  Delete "$DESKTOP\${APPNAME}.lnk"
  DeleteRegKey HKCU "${UNINSTKEY}"
  DeleteRegValue HKCU "${HEARTHKEY}" "LegacyDataDir"
  DeleteRegKey /ifempty HKCU "${HEARTHKEY}"
  RMDir "$INSTDIR"  ; only removed when empty (user data kept)
SectionEnd

Section /o "un.Also delete my settings, machines and logs" UnSecData
  RMDir /r "$INSTDIR\userdata"
  RMDir /r "$INSTDIR\logs"
  Delete "$INSTDIR\config.json"
  Delete "$INSTDIR\autotune-state.json"
  RMDir "$INSTDIR"
SectionEnd

LangString DESC_UnMain ${LANG_ENGLISH} "Remove the program, shortcuts and the Apps & Features entry."
LangString DESC_UnData ${LANG_ENGLISH} "Also delete userdata\ (machines, saved passwords, settings, history, event log), logs\ and config.json. Leave unticked to keep them for a later reinstall."
!insertmacro MUI_UNFUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${UnSecMain} $(DESC_UnMain)
  !insertmacro MUI_DESCRIPTION_TEXT ${UnSecData} $(DESC_UnData)
!insertmacro MUI_UNFUNCTION_DESCRIPTION_END
