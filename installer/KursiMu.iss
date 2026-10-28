#define AppName "Korsimu"
#define AppVersion "1.0.0"
#define AppPublisher "JR-Core Lab"
[Setup]
AppId={{E7199D03-86A2-443C-B933-02754F321FAD}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\Korsimu
DefaultGroupName=Korsimu
OutputDir=..\dist\installer
OutputBaseFilename=Korsimu_Setup_1.0.0_Final
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
SetupIconFile=..\assets\kursimu.ico
UninstallDisplayIcon={app}\Korsimu.exe
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
[Files]
Source: "..\dist\Korsimu\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{autoprograms}\Korsimu"; Filename: "{app}\Korsimu.exe"
Name: "{autodesktop}\Korsimu"; Filename: "{app}\Korsimu.exe"; Tasks: desktopicon
[Tasks]
Name: "desktopicon"; Description: "Buat shortcut desktop"; GroupDescription: "Shortcut:"
[Run]
Filename: "{app}\Korsimu.exe"; Description: "Jalankan Korsimu"; Flags: nowait postinstall skipifsilent
[UninstallDelete]
; Existing patient data remains in %LOCALAPPDATA%\KursiMu and is intentionally not deleted.
