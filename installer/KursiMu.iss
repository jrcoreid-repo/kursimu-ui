#define AppName "KursiMu"
#define AppVersion "2.11.5"
#define AppPublisher "JR-Core Lab"
[Setup]
AppId={{E7199D03-86A2-443C-B933-02754F321FAD}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\KursiMu
DefaultGroupName=KursiMu
OutputDir=..\dist\installer
OutputBaseFilename=KursiMu_Setup_2.11.5
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
SetupIconFile=..\assets\kursimu.ico
UninstallDisplayIcon={app}\KursiMu.exe
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
[Files]
Source: "..\dist\KursiMu\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{autoprograms}\KursiMu"; Filename: "{app}\KursiMu.exe"
Name: "{autodesktop}\KursiMu"; Filename: "{app}\KursiMu.exe"; Tasks: desktopicon
[Tasks]
Name: "desktopicon"; Description: "Buat shortcut desktop"; GroupDescription: "Shortcut:"
[Run]
Filename: "{app}\KursiMu.exe"; Description: "Jalankan KursiMu"; Flags: nowait postinstall skipifsilent
[UninstallDelete]
; Patient data is in %LOCALAPPDATA%\KursiMu and is intentionally not deleted.
