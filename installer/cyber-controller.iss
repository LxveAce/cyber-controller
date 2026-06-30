; Inno Setup script for Cyber Controller.
;
; Produces a real Windows installer so the app appears under Settings > Apps > Installed apps with a
; proper icon and an uninstaller — instead of a loose .exe. It packages a PyInstaller --onedir build
; (instant startup; no ~15 s self-extract) and registers the standard Add/Remove Programs keys (Inno
; writes DisplayName / DisplayVersion / Publisher / DisplayIcon / UninstallString / InstallLocation /
; EstimatedSize automatically from the [Setup] metadata below).
;
; Build it (after `python build.py --onedir` has produced dist\CyberController\):
;   iscc /DMyAppVersion=1.4.0 installer\cyber-controller.iss
; Output: installer\Output\cyber-controller-vX.Y.Z-windows-x64-setup.exe
;
; NOTE: this installer is NOT code-signed here. An unsigned installer still trips SmartScreen until an
; OV/EV certificate is applied (sign installer\Output\*.exe with signtool as a later, cert-gated step).
; See docs/WINDOWS-SECURITY.md.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

#define MyAppName "Cyber Controller"
#define MyAppPublisher "LxveAce"
#define MyAppURL "https://cybercontroller.org"
#define MyAppExeName "CyberController.exe"

[Setup]
; A stable, unique AppId (keep this GUID constant across releases so upgrades replace cleanly).
AppId={{8F3C2A14-7B6E-4D59-9C2A-CC0DEADBEEF1}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL=https://github.com/LxveAce/cyber-controller/releases
; Per-user install — no admin/UAC prompt; lands in %LOCALAPPDATA%\Programs\CyberController.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\CyberController
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=cyber-controller-v{#MyAppVersion}-windows-x64-setup
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The entire --onedir tree (CyberController.exe + _internal\). 'recursesubdirs' pulls in _internal\.
Source: "..\dist\CyberController\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
