#define AppName "Runner V4"
#define AppVersion "4.0.0"
#define AppPublisher "Runner"
#define AppExeName "Runner.exe"

[Setup]
AppId={{9B525114-61D7-4FA4-9E82-AB34A04E5D36}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\RunnerV4
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\installer
OutputBaseFilename=RunnerSetup
SetupIconFile=..\assets\runner_icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
CloseApplications=yes
RestartApplications=no
ChangesAssociations=no
ChangesEnvironment=no
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\Runner.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\UpdateRunner.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\assets\runner_icon.ico"; DestDir: "{app}\assets"; Flags: ignoreversion
Source: "install-server-startup-task.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "remove-server-startup-task.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "install-server-startup-task-admin.cmd"; DestDir: "{app}\tools"; Flags: ignoreversion

[Icons]
Name: "{group}\Runner"; Filename: "{app}\Runner.exe"; WorkingDir: "{app}"; IconFilename: "{app}\assets\runner_icon.ico"
Name: "{group}\Update Runner"; Filename: "{app}\UpdateRunner.exe"; WorkingDir: "{app}"; IconFilename: "{app}\assets\runner_icon.ico"
Name: "{group}\Install Server Startup Task"; Filename: "{app}\tools\install-server-startup-task-admin.cmd"; WorkingDir: "{app}\tools"; IconFilename: "{app}\assets\runner_icon.ico"
Name: "{group}\Uninstall Runner"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Runner"; Filename: "{app}\Runner.exe"; WorkingDir: "{app}"; IconFilename: "{app}\assets\runner_icon.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\Runner.exe"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C taskkill /IM Runner.exe /F >NUL 2>NUL"; Flags: runhidden; RunOnceId: "StopRunner"
Filename: "{cmd}"; Parameters: "/C taskkill /IM UpdateRunner.exe /F >NUL 2>NUL"; Flags: runhidden; RunOnceId: "StopUpdateRunner"

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\Runner_V4"
