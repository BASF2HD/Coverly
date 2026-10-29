#ifndef AppVersion
  #error AppVersion must be provided by the native build script
#endif
#ifndef AppSource
  #error AppSource must point to the verified native Coverly directory
#endif
#ifndef AppOutput
  #error AppOutput must point to the release directory
#endif

[Setup]
AppId={{F6EFBAD6-D1E5-44AD-A94A-42E3B6D41B5E}
AppName=Coverly
AppVersion={#AppVersion}
AppPublisher=Coverly
AppPublisherURL=https://github.com/BASF2HD/Coverly
AppSupportURL=https://github.com/BASF2HD/Coverly/issues
DefaultDirName={localappdata}\Programs\Coverly
DefaultGroupName=Coverly
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#AppOutput}
OutputBaseFilename=Coverly-{#AppVersion}-windows-x64-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\Coverly.exe
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: desktopicon; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "{#AppSource}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Coverly"; Filename: "{app}\Coverly.exe"
Name: "{autodesktop}\Coverly"; Filename: "{app}\Coverly.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Coverly.exe"; Description: "Open Coverly"; Flags: nowait postinstall skipifsilent
