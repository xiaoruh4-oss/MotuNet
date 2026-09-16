; Motu Net installer (x64, Windows 10+)
#define AppName "Motu Net"
#define AppPublisher "Motu"
#ifndef AppVersion
  #error Build with tools/build_release.ps1 to supply AppVersion from netlab.__version__.
#endif
#define AppExeName "MotuNet.exe"
#ifndef PayloadDir
  #define PayloadDir "..\staging\MotuNet"
#endif
#ifndef ReleaseOutputDir
  #define ReleaseOutputDir "..\deliverables"
#endif
#define DriverRelativePath "_internal\vendor\windivert\WinDivert64.sys"
#define BundledDriverSHA256 GetSHA256OfFile(PayloadDir + "\" + DriverRelativePath)

[Setup]
AppId={{8B7A7D4C-7D6D-4BE0-9F34-4B7E2D1A9A20}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={code:GetInstallDir}
; Always let the user review or change the actual installation directory.
; UsePreviousAppDir keeps the last installed path as the default during upgrades.
DisableDirPage=no
UsePreviousAppDir=yes
DefaultGroupName={#AppName}
; The Start Menu folder is fixed to the app name; skip its wizard page so it
; cannot be mistaken for the installation directory. The shortcut is still
; created under {group}.
DisableProgramGroupPage=yes
PrivilegesRequired=admin
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#ReleaseOutputDir}
OutputBaseFilename=MotuNet-{#AppVersion}-Setup-x64
SetupIconFile=..\assets\motu-net.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
LanguageDetectionMethod=uilanguage
AppMutex=MotuNet.Desktop
CloseApplications=yes
RestartApplications=no
Uninstallable=yes
PrivilegesRequiredOverridesAllowed=commandline
ChangesAssociations=no
; Keep user settings outside the install directory. Uninstall only removes installed files.

[Languages]
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#PayloadDir}\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "vendor\windivert\WinDivert64.sys"
; An unchanged driver may still be loaded by another application. Do not
; queue identical bytes for replacement at reboot just because it is locked.
Source: "{#PayloadDir}\{#DriverRelativePath}"; DestDir: "{app}\_internal\vendor\windivert"; Flags: ignoreversion restartreplace; Check: ShouldInstallDriver

[InstallDelete]
; Remove payload files that existed in earlier onedir builds while preserving user data.
Type: filesandordirs; Name: "{app}\NetLab.exe"
Type: filesandordirs; Name: "{app}\MutuNet.exe"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
; Do not auto-run after installation. Users can launch from the shortcut.

[Code]
const
  { The AppId was retained when NetLab/Mutu Net became Motu Net. Both old
    per-user and newer machine-wide installations use this uninstall key. }
  ProductUninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{8B7A7D4C-7D6D-4BE0-9F34-4B7E2D1A9A20}_is1';

var
  KnownInstallDirs: TStringList;
  ProcessQueryError: String;

function ShouldInstallDriver: Boolean;
var
  InstalledPath: String;
begin
  Result := True;
  InstalledPath := ExpandConstant('{app}\{#DriverRelativePath}');
  if not FileExists(InstalledPath) then
    exit;
  try
    if CompareText(GetSHA256OfFile(InstalledPath), '{#BundledDriverSHA256}') = 0 then begin
      Log('WinDivert64.sys matches bundled SHA256; skipping driver replacement.');
      Result := False;
    end else
      Log('WinDivert64.sys differs from bundled SHA256; driver update required.');
  except
    { A read failure is not evidence that the driver is current. Keep the
      normal install/retry/restart behavior instead of silently keeping it. }
    Log('Cannot compare existing WinDivert64.sys: ' + GetExceptionMessage);
  end;
end;

function NormalizeInstallDir(const Dir: String): String;
begin
  Result := Lowercase(RemoveBackslashUnlessRoot(ExpandFileName(Dir)));
end;

function ReadInstallDir(Root: Integer; var Dir: String): Boolean;
begin
  Result := RegQueryStringValue(Root, ProductUninstallKey, 'InstallLocation', Dir);
  if Result then
    Result := (Trim(Dir) <> '') and DirExists(Dir);
  if not Result then begin
    { Inno also stores its original directory in this value. }
    Result := RegQueryStringValue(Root, ProductUninstallKey, 'Inno Setup: App Path', Dir);
    if Result then
      Result := (Trim(Dir) <> '') and DirExists(Dir);
  end;
end;

function GetInstallDir(Param: String): String;
var
  Dir: String;
begin
  { UsePreviousAppDir still prefers a matching installation in the current
    install mode. This fallback also finds the previous per-user install
    when moving to the administrator installer, including older 32-bit keys. }
  if ReadInstallDir(HKCU64, Dir) or ReadInstallDir(HKCU32, Dir) or
     ReadInstallDir(HKLM64, Dir) or ReadInstallDir(HKLM32, Dir) then
    Result := Dir
  else
    Result := ExpandConstant('{localappdata}\Programs\Motu Net');
end;

procedure AddKnownInstallDir(const Dir: String);
var
  Normalized: String;
begin
  if Trim(Dir) = '' then
    exit;
  Normalized := NormalizeInstallDir(Dir);
  if KnownInstallDirs.IndexOf(Normalized) < 0 then
    KnownInstallDirs.Add(Normalized);
end;

procedure AddRegisteredInstallDir(Root: Integer);
var
  Dir: String;
begin
  if ReadInstallDir(Root, Dir) then
    AddKnownInstallDir(Dir);
end;

procedure RefreshKnownInstallDirs;
begin
  if KnownInstallDirs = nil then
    KnownInstallDirs := TStringList.Create;
  KnownInstallDirs.Clear;
  AddKnownInstallDir(WizardDirValue);
  AddRegisteredInstallDir(HKCU64);
  AddRegisteredInstallDir(HKCU32);
  AddRegisteredInstallDir(HKLM64);
  AddRegisteredInstallDir(HKLM32);
end;

function IsMotuProcess(const ProcessName, ExePath: String): Boolean;
var
  Name: String;
begin
  Name := Lowercase(ProcessName);
  Result := ((Name = 'motunet.exe') or (Name = 'mutunet.exe') or
    (Name = 'netlab.exe')) and (Trim(ExePath) <> '') and
    (KnownInstallDirs.IndexOf(NormalizeInstallDir(ExtractFileDir(ExePath))) >= 0);
end;

function GetMotuProcessIds(Ids: TStringList): Boolean;
var
  Locator, Service, Processes, Proc: Variant;
  I: Integer;
  Name, ExePath: String;
begin
  Result := False;
  ProcessQueryError := '';
  Ids.Clear;
  try
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Service := Locator.ConnectServer('.', 'root\cimv2');
    Processes := Service.ExecQuery(
      'SELECT Name, ProcessId, ExecutablePath FROM Win32_Process ' +
      'WHERE Name=''MotuNet.exe'' OR Name=''MutuNet.exe'' OR Name=''NetLab.exe''');
    for I := 0 to Processes.Count - 1 do begin
      Proc := Processes.ItemIndex(I);
      Name := Proc.Name;
      { A null/inaccessible executable path raises here. Do not interpret a
        failed query as proof that the program is no longer running. }
      ExePath := Proc.ExecutablePath;
      if Trim(ExePath) = '' then begin
        ProcessQueryError := '无法读取正在运行的 ' + Name + ' 的路径，不能安全确认是否属于本次安装。';
        exit;
      end;
      if IsMotuProcess(Name, ExePath) then
        Ids.Add(IntToStr(Proc.ProcessId));
    end;
    Result := True;
  except
    ProcessQueryError := '无法查询正在运行的程序：' + GetExceptionMessage;
    Log(ProcessQueryError);
  end;
end;

function ProcessQueryMessage: String;
begin
  Result := ProcessQueryError + #13#10 + '尚未替换安装文件。请关闭 Motu Net 后点击“重试”；若仍失败，请确认 Windows Management Instrumentation 服务可用。';
end;

function CloseMotuProcess(const Pid: String; Force: Boolean): Boolean;
var
  Params: String;
  ExitCode: Integer;
begin
  { Match the executable path before reaching this point. Never use /IM or
    /T: other programs with the same name and child processes are not ours. }
  Params := '/PID ' + Pid;
  if Force then
    Params := Params + ' /F';
  Result := Exec(ExpandConstant('{sys}\taskkill.exe'), Params, '',
    SW_HIDE, ewWaitUntilTerminated, ExitCode) and (ExitCode = 0);
  Log('Close Motu PID ' + Pid + ': ' + IntToStr(ExitCode));
end;

function WaitForMotuProcesses(MaxSeconds: Integer; Ids: TStringList): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 0 to MaxSeconds * 4 do begin
    if not GetMotuProcessIds(Ids) then
      exit;
    if Ids.Count = 0 then begin
      Result := True;
      exit;
    end;
    if I < MaxSeconds * 4 then
      Sleep(250);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Ids: TStringList;
  I: Integer;
begin
  Result := '';
  RefreshKnownInstallDirs;
  Ids := TStringList.Create;
  try
    if not GetMotuProcessIds(Ids) then begin
      Result := ProcessQueryMessage;
      exit;
    end;
    if Ids.Count > 0 then begin
      for I := 0 to Ids.Count - 1 do
        CloseMotuProcess(Ids[I], False);
      if not WaitForMotuProcesses(3, Ids) then begin
        if ProcessQueryError <> '' then begin
          Result := ProcessQueryMessage;
          exit;
        end;
        { WaitForMotuProcesses refreshed these IDs and checked their paths. }
        for I := 0 to Ids.Count - 1 do
          CloseMotuProcess(Ids[I], True);
        if not WaitForMotuProcesses(7, Ids) then begin
          if ProcessQueryError <> '' then
            Result := ProcessQueryMessage
          else
            Result := 'Motu Net 仍在运行，尚未替换安装文件。请先点击“停止并恢复”并退出程序，然后点击“重试”。';
          exit;
        end;
      end;
    end;
    { Never stop a shared WinDivert service. An identical driver is skipped
      by ShouldInstallDriver; a genuinely different locked driver retains
      restartreplace so Setup explicitly reports the required restart. }
    Sleep(500);
  finally
    Ids.Free;
  end;
end;

procedure DeinitializeSetup;
begin
  if KnownInstallDirs <> nil then
    KnownInstallDirs.Free;
end;
