; Inno Setup script for JobPilot.
; Ported from Prospector's installer\prospector.iss -- same reasoning
; applies unchanged (see the comments kept below). Targets Inno Setup 6.2.x
; on purpose, same version ceiling as Prospector's.
;
; Compiled by installer\build_installer.ps1, which passes MyAppVersion in.
; To compile by hand:
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\jobpilot.iss

#define MyAppName "JobPilot"
#define MyAppPublisher "Akshay Kharvi"
#define MyAppURL "https://github.com/AxeyShane/JobPilot"
#define MyAppExeName "JobPilot.exe"

; Overridden by the build script: /DMyAppVersion=0.4.0
#ifndef MyAppVersion
  #define MyAppVersion "0.4.0"
#endif

; Where PyInstaller left the one-folder build.
#ifndef MySourceDir
  #define MySourceDir "..\dist\JobPilot"
#endif

[Setup]
; NOTE: The value of AppId uniquely identifies this application. Do not use
; the same AppId value in installers for other applications -- this one is
; freshly generated, not copied from Prospector's.
AppId={{3B3A0DFC-9257-4E58-A45C-2EDABFD24C5C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Per-user install: no administrator prompt, no admin account needed. The
; whole point is that a non-technical person can install this on their own.
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputDir=..\dist
OutputBaseFilename=JobPilotSetup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
; Upgrading over a running copy silently fails to replace the files that are
; locked, which produces a half-old half-new install that crashes only on
; upgraded machines. Ask instead.
CloseApplications=yes
CloseApplicationsFilter=*.exe,*.dll,*.pyd
RestartApplications=no
AppMutex=JobPilotSetupMutex

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; PyInstaller renames its private directory between versions, so an upgrade
; used to leave both generations side by side in {app}, and the stale one
; shadows the new one on import.
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\*.pyd"
Type: files; Name: "{app}\python*.dll"
Type: files; Name: "{app}\base_library.zip"

[Files]
Source: "{#MySourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; NOTE: Don't use "Flags: ignoreversion" on any shared system files

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

; No [UninstallDelete] entry for the Playwright browser cache
; ({localappdata}\ms-playwright): unlike Prospector's own per-app "engine"
; download, this path is a single global cache shared by EVERY
; Playwright-based tool on the machine, keyed by browser version, not owned
; exclusively by this app. Verified the hard way -- a silent-uninstall test
; during packaging deleted the dev environment's live Chromium cache out
; from under two already-running pipeline loops. The user's tailored
; resumes, cover letters and application database (%USERPROFILE%\.jobpilot)
; were never touched by uninstall and stay that way.

[Code]
function InitializeSetup(): Boolean;
var
  Version: TWindowsVersion;
begin
  GetWindowsVersionEx(Version);
  { Windows 10 is build 10240. Below that the bundled Python runtime and the
    TLS stack JobPilot needs are not dependable. }
  if Version.Major < 10 then
  begin
    MsgBox('JobPilot needs Windows 10 or later.', mbCriticalError, MB_OK);
    Result := False;
  end
  else
    Result := True;
end;
