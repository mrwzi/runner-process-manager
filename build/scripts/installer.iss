#define AppName "Runner"
#define AppVersion "4.1.0"
#define AppPublisher "Runner"
#define AppExeName "Runner.exe"

[Setup]
AppId={{9B525114-61D7-4FA4-9E82-AB34A04E5D36}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\Runner
UsePreviousAppDir=no
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=yes
OutputDir=..\..\release
OutputBaseFilename=RunnerSetup-4.1.0
SetupIconFile=..\..\src\assets\runner_icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
; A clustered installation needs a SYSTEM Agent task and a ProgramData
; runtime.  Do not expose the per-user override dialog: it could install the
; GUI while silently leaving cluster mode without an Agent.
CloseApplications=no
RestartApplications=no
ChangesAssociations=no
ChangesEnvironment=no
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
; Main binaries are staged and copied by PrepareToInstall after the old image
; handles are released. This avoids Inno's generic Retry/Ignore/Cancel path.
Source: "..\..\dist\windows\Runner.exe"; DestDir: "{tmp}"; Flags: dontcopy
Source: "..\..\dist\windows\UpdateRunner.exe"; DestDir: "{tmp}"; Flags: dontcopy
Source: "..\..\src\assets\runner_icon.ico"; DestDir: "{app}\assets"; Flags: ignoreversion
Source: "install-runner-agent.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "remove-runner-agent.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "repair-runner.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "verify-runner-upgrade.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "install-server-startup-task.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "remove-server-startup-task.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "prepare-runner-upgrade.ps1"; DestDir: "{tmp}"; Flags: dontcopy
Source: "migrate-runner-apps.ps1"; DestDir: "{tmp}"; Flags: dontcopy

[Icons]
Name: "{group}\Runner"; Filename: "{app}\Runner.exe"; WorkingDir: "{app}"; IconFilename: "{app}\assets\runner_icon.ico"
Name: "{group}\Repair Runner"; Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\tools\repair-runner.ps1"" -InstallDir ""{app}"" -RuntimeRoot ""{commonappdata}\Runner_V4"""; IconFilename: "{app}\assets\runner_icon.ico"
Name: "{group}\Uninstall Runner"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Runner"; Filename: "{app}\Runner.exe"; WorkingDir: "{app}"; IconFilename: "{app}\assets\runner_icon.ico"; Tasks: desktopicon

[Run]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\tools\install-runner-agent.ps1"" -InstallDir ""{app}"" -RuntimeRoot ""{commonappdata}\Runner_V4"""; StatusMsg: "Starting upgraded Runner Agent..."; Flags: runhidden waituntilterminated; Check: VerifyRunnerRegistry
Filename: "{app}\Runner.exe"; Description: "Launch Runner"; Flags: nowait postinstall skipifsilent; Check: VerifyRunnerRegistry

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\tools\remove-runner-agent.ps1"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveRunnerAgent"

[Code]
var
  RemoveData: Boolean;
  PrepareStarted: Boolean;
  InstallCommitted: Boolean;
  BinaryBackupReady: Boolean;
  RunnerWasPresent: Boolean;
  UpdaterWasPresent: Boolean;
  BinaryBackupDir: String;
  UpgradeScriptPath: String;

procedure BackupInstalledBinaries();
var
  RuntimePath: String;
begin
  RuntimePath := ExpandConstant('{commonappdata}\Runner_V4');
  BinaryBackupDir := RuntimePath + '\config-backups\installer-binaries-' + GetDateTimeString('yyyymmdd-hhnnss', '-', '-');
  if not ForceDirectories(BinaryBackupDir) then
    RaiseException('Could not create Runner binary rollback folder: ' + BinaryBackupDir);
  RunnerWasPresent := FileExists(ExpandConstant('{app}\Runner.exe'));
  UpdaterWasPresent := FileExists(ExpandConstant('{app}\UpdateRunner.exe'));
  if RunnerWasPresent and not CopyFile(ExpandConstant('{app}\Runner.exe'), BinaryBackupDir + '\Runner.exe', False) then
    RaiseException('Could not back up the installed Runner.exe. No binaries were replaced.');
  if UpdaterWasPresent and not CopyFile(ExpandConstant('{app}\UpdateRunner.exe'), BinaryBackupDir + '\UpdateRunner.exe', False) then
    RaiseException('Could not back up the installed UpdateRunner.exe. No binaries were replaced.');
  BinaryBackupReady := True;
end;

procedure RestoreInstalledBinaries();
var
  RestoreFailed: Boolean;
begin
  if not BinaryBackupReady then exit;
  RestoreFailed := False;
  if RunnerWasPresent and FileExists(BinaryBackupDir + '\Runner.exe') then
    (* False means overwrite an existing destination. True makes CopyFile fail
       precisely on an in-place upgrade because the destination already exists. *)
    RestoreFailed := not CopyFile(BinaryBackupDir + '\Runner.exe', ExpandConstant('{app}\Runner.exe'), False) or RestoreFailed;
  if UpdaterWasPresent and FileExists(BinaryBackupDir + '\UpdateRunner.exe') then
    RestoreFailed := not CopyFile(BinaryBackupDir + '\UpdateRunner.exe', ExpandConstant('{app}\UpdateRunner.exe'), False) or RestoreFailed;
  if RestoreFailed then
    MsgBox('Runner could not fully restore the previous binaries. Keep this backup for recovery:' + #13#10 + BinaryBackupDir,
      mbCriticalError, MB_OK);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  ScriptPath: String;
  Parameters: String;
begin
  Result := '';
  PrepareStarted := True;
  ExtractTemporaryFile('prepare-runner-upgrade.ps1');
  ScriptPath := ExpandConstant('{tmp}\prepare-runner-upgrade.ps1');
  UpgradeScriptPath := ScriptPath;
  Parameters := '-NoProfile -ExecutionPolicy Bypass -File "' + ScriptPath + '" -InstallDir "' +
    ExpandConstant('{app}') + '" -RuntimeRoot "' + ExpandConstant('{commonappdata}\Runner_V4') + '"';
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Parameters, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then begin
    Result := 'Runner upgrade preparation could not start. No files were replaced.';
  end else if ResultCode <> 0 then begin
    Result := 'Runner could not safely prepare the upgrade. No Runner binaries were replaced and managed applications were left running. Review ProgramData\Runner_V4\logs\installer-upgrade.log, then close Runner and retry.';
  end else begin
    try
      BackupInstalledBinaries();
      ExtractTemporaryFile('Runner.exe');
      ExtractTemporaryFile('UpdateRunner.exe');
      if not CopyFile(ExpandConstant('{tmp}\Runner.exe'), ExpandConstant('{app}\Runner.exe'), False) then
        RaiseException('Windows refused to install Runner.exe after the old process exited.');
      if not CopyFile(ExpandConstant('{tmp}\UpdateRunner.exe'), ExpandConstant('{app}\UpdateRunner.exe'), False) then
        RaiseException('Windows refused to install UpdateRunner.exe.');
    except
      RestoreInstalledBinaries();
      Result := GetExceptionMessage;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssDone then
    InstallCommitted := True;
end;

procedure DeinitializeSetup();
var
  ResultCode: Integer;
  Parameters: String;
begin
  if not InstallCommitted then begin
    RestoreInstalledBinaries();
    if PrepareStarted and (UpgradeScriptPath <> '') and FileExists(UpgradeScriptPath) then begin
      Parameters := '-NoProfile -ExecutionPolicy Bypass -File "' + UpgradeScriptPath + '" -InstallDir "' +
        ExpandConstant('{app}') + '" -RuntimeRoot "' + ExpandConstant('{commonappdata}\Runner_V4') + '" -Rollback';
      if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Parameters, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
        MsgBox('Runner rollback could not restore its previous scheduled-task state automatically. Use the Repair Runner shortcut. Managed applications were not stopped.', mbError, MB_OK);
    end;
  end;
end;

function VerifyRunnerRegistry(): Boolean;
var
  ResultCode: Integer;
  ScriptPath: String;
  Parameters: String;
begin
  ExtractTemporaryFile('migrate-runner-apps.ps1');
  ScriptPath := ExpandConstant('{tmp}\migrate-runner-apps.ps1');
  Parameters := '-NoProfile -ExecutionPolicy Bypass -File "' + ScriptPath + '" -RuntimeRoot "' +
    ExpandConstant('{commonappdata}\Runner_V4') + '"';
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Parameters, '', SW_HIDE, ewWaitUntilTerminated, ResultCode)
    or (ResultCode <> 0) then begin
    MsgBox('Runner could not safely migrate the application registry. The original registry was preserved or restored. Review ProgramData\Runner_V4\config-backups before retrying.', mbCriticalError, MB_OK);
    Result := False;
    exit;
  end;
  ExtractTemporaryFile('prepare-runner-upgrade.ps1');
  { The post-install verifier is installed with Runner, not embedded. }
  ScriptPath := ExpandConstant('{app}\tools\verify-runner-upgrade.ps1');
  Parameters := '-NoProfile -ExecutionPolicy Bypass -File "' + ScriptPath + '" -RuntimeRoot "' +
    ExpandConstant('{commonappdata}\Runner_V4') + '"';
  Result := Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Parameters, '', SW_HIDE, ewWaitUntilTerminated, ResultCode)
    and (ResultCode = 0);
  if not Result then
    MsgBox('Runner did not verify the expected application registry. The previous registry was restored and Runner will not be launched. Review ProgramData\Runner_V4\config-backups.', mbCriticalError, MB_OK);
end;

function InitializeUninstall(): Boolean;
begin
  RemoveData := MsgBox('Remove Runner local configuration, identities, encrypted secrets, logs, and deployment cache?' + #13#10 + #13#10 +
    'Choose No to remove only Runner. Project folders are never removed.', mbConfirmation, MB_YESNO) = IDYES;
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and RemoveData then begin
    DelTree(ExpandConstant('{commonappdata}\Runner_V4'), True, True, True);
    DelTree(ExpandConstant('{localappdata}\Runner_V4'), True, True, True);
  end;
end;
