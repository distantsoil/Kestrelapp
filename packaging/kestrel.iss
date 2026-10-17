; Inno Setup script for Kestrel (SPEC-INTERFACES 1.4). Produces Kestrel-Setup-x.y.z.exe that installs
; WITHOUT admin rights to %LOCALAPPDATA%\Programs\Kestrel, with Start-menu and optional desktop
; shortcuts, an uninstaller, and an optional launch-at-login (minimized to tray).
;
; Build:  uv run python packaging/gen_version_iss.py   (writes version.iss from __version__)
;         uv run pyinstaller packaging/kestrel.spec --noconfirm   (writes dist/Kestrel/)
;         iscc packaging/kestrel.iss                   (writes dist/Kestrel-Setup-<ver>.exe)
;
; The installer never touches %LOCALAPPDATA%\Kestrel (the DATA directory): install and data are
; separate. The uninstaller asks whether to delete it too, defaulting to No, and a silent
; uninstall always keeps it (D-PKG-UNINSTALL).

#include "version.iss"

[Setup]
AppId={{7F3A2C10-4B8E-4E2A-9C1D-9A1B2C3D4E5F}
AppName=Kestrel
AppVersion={#AppVersion}
AppPublisher=Kestrel
DefaultDirName={localappdata}\Programs\Kestrel
DefaultGroupName=Kestrel
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=Kestrel-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Generated from docs/images/kestrel-mark.svg by scripts/make_icons.py.
SetupIconFile=kestrel.ico
UninstallDisplayIcon={app}\Kestrel.exe
UninstallDisplayName=Kestrel {#AppVersion}
; Must match MUTEX_NAME in kestrel/desktop/shell.py byte for byte. Without it the installer will
; happily overwrite files while Kestrel is running; with it the operator is told to close it first.
AppMutex=Local\Kestrel

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
Name: "startupicon"; Description: "Launch Kestrel at login (minimized to the tray)"; Flags: unchecked

[Files]
; The whole PyInstaller one-folder output.
Source: "..\dist\Kestrel\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\Kestrel"; Filename: "{app}\Kestrel.exe"
Name: "{group}\Uninstall Kestrel"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Kestrel"; Filename: "{app}\Kestrel.exe"; Tasks: desktopicon
Name: "{userstartup}\Kestrel"; Filename: "{app}\Kestrel.exe"; Tasks: startupicon

[Run]
Filename: "{app}\Kestrel.exe"; Description: "Launch Kestrel now"; Flags: nowait postinstall skipifsilent

[Code]
// Optional data cleanup (D-PKG-UNINSTALL). After the program files are gone, offer to delete the
// data directory as well. No is the default button; a silent uninstall never deletes data. The
// path is the default data home (paths.data_home on Windows); a KESTREL_DATA_HOME override is
// outside it and is left alone.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
  NL: String;
begin
  if CurUninstallStep <> usPostUninstall then
    exit;
  DataDir := ExpandConstant('{localappdata}\Kestrel');
  if UninstallSilent or not DirExists(DataDir) then
    exit;
  // No line may start with '#': the preprocessor (ISPP) reads that as a directive, so the line
  // breaks live in NL rather than as #13#10 at the start of a continuation line.
  NL := #13#10#13#10;
  if MsgBox('Also delete your Kestrel data?' + NL +
            'This removes ' + DataDir + ': the database, saved API keys, settings, generated ' +
            'documents, logs, and any backups saved in that folder. It cannot be undone.' + NL +
            'Close any kestrel-cli window first (for example kestrel-cli mcp); ' +
            'files it holds open cannot be deleted.' + NL +
            'Choose No to keep it for a later reinstall.',
            mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
  begin
    if not DelTree(DataDir, True, True, True) then
      MsgBox('Some files in ' + DataDir + ' could not be deleted. Close anything using them ' +
             'and remove the folder by hand.', mbError, MB_OK);
  end;
end;
