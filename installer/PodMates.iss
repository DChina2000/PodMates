; 群聊机器人面板 安装程序（Inno Setup 6）
; 构建：build\tools\inno\ISCC.exe installer\PodMates.iss
; 默认按当前用户安装、不要管理员权限（也可以选「为所有用户安装」，那要管理员权限）；可以选安装位置。
; 「仅为我安装」时数据放在安装目录下的 data 文件夹（程序和数据在一起）；「为所有用户安装」装在 Program Files 里普通用户写不进去，数据仍放 %APPDATA%\PodMates。
; 程序启动时自己找数据（见 mb\paths.py）。卸载时会问要不要删数据，默认保留。

#define AppName "PodMates 群聊机器人面板"
#define AppVersion "1.4.3"
#define AppExe "PodMates.exe"

[Setup]
AppId={{D7A3E1B5-52C8-4E69-8B17-0F4C9A6E2D31}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=本昵称不代表本人
AppPublisherURL=https://space.bilibili.com/10228715
AppSupportURL=https://github.com/DChina2000/PodMates
DefaultDirName={autopf}\PodMates
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=no
UsePreviousAppDir=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=PodMates-Setup-{#AppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=no

[Languages]
Name: "zh"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "在桌面创建图标"; GroupDescription: "附加图标："

[Dirs]
Name: "{app}\data"; Check: PerUserInstall; Flags: uninsneveruninstall

[Files]
Source: "..\build\dist\PodMates\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\docs\使用说明.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\docs\User-Guide.en.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "stop-podmates.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "stop-podmates.ps1"; DestDir: "{tmp}"; Flags: dontcopy
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\THIRD_PARTY.md"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
; 装新版前先清掉旧版的程序文件（新版里去掉的模板、外壳、模块不残留）；数据在 {app}\data 或 %APPDATA%\PodMates，不在这里
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\使用说明"; Filename: "{app}\使用说明.md"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; 开机自启由程序自己按设置写入；卸载时一并删掉
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "PodMates"; ValueType: none; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "现在打开 {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Code]
function PerUserInstall(): Boolean;
begin
  Result := not IsAdminInstallMode;
end;

procedure StopApp(Script: String; KeepNapCat: Boolean);
var
  Code: Integer;
  Roots, PidFiles, Args: String;
begin
  { 装新版（KeepNapCat = True）：只结束 PodMates 自己（主程序和面板窗口）。账号的 NapCat 和它拉起的 QQ 不停，装完 PodMates 重新起来会接回去，
    QQ 不用重新登录；它们用的文件都在数据目录里（或系统装的 QQ），安装程序不往那里写，不会被覆盖或占用。taskkill 不带 /T，
    不然会连 PodMates 启动的 NapCat 一起结束。保险：要保留的进程里如果有从程序目录（数据目录除外）加载了模块的（比如继承了 PodMates 的 DLL 搜索目录，从 _internal
    里加载了运行库），装新版覆盖那些文件会失败，脚本会把它连同同一个号的 NapCat / QQ 一起停掉，并留一条说明给 PodMates 下次启动时记进日志。
    卸载（KeepNapCat = False）：连 PodMates 启动的 NapCat 和 NapCat 拉起的 QQ 一起清掉：一键包里的 QQ.exe 只是启动器，拉起 QQ 本体就退出了，
    QQ 本体不在 PodMates 的进程树里，光 taskkill /T 结束不掉，它们占着文件，卸载会因为文件被占用而删不干净。
    清理脚本按「PodMates 记下的 pid、安装 / 数据目录下的进程、NapCat 启动的孤儿 QQ」认，用户自己开的 QQ 任何时候都不碰（见 stop-podmates.ps1） }
  if KeepNapCat then
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, Code)
  else
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Roots := ExpandConstant('{app}') + '|' + ExpandConstant('{userappdata}\PodMates');
  PidFiles := ExpandConstant('{app}\data\napcat-pids.json') + '|' + ExpandConstant('{userappdata}\PodMates\napcat-pids.json');
  Args := '-NoProfile -ExecutionPolicy Bypass -File "' + Script + '" -Roots "' + Roots + '" -PidFiles "' + PidFiles + '"';
  if KeepNapCat then
    Args := Args + ' -KeepNapCat -ProtectDirs "' + ExpandConstant('{app}\data') + '|' + ExpandConstant('{userappdata}\PodMates') + '"'
      + ' -AppDir "' + ExpandConstant('{app}') + '"'
      + ' -NoteFiles "' + ExpandConstant('{app}\data\install-notes.txt') + '|' + ExpandConstant('{userappdata}\PodMates\install-notes.txt') + '"';
  if FileExists(Script) then
    Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Args, '', SW_HIDE, ewWaitUntilTerminated, Code);
  Sleep(800);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  ExtractTemporaryFile('stop-podmates.ps1');
  StopApp(ExpandConstant('{tmp}\stop-podmates.ps1'), True);
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  StopApp(ExpandConstant('{app}\tools\stop-podmates.ps1'), False);
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{app}\data');  { 数据在安装目录下（「仅为我安装」）或 %APPDATA%\PodMates }
    if not DirExists(DataDir) then
      DataDir := ExpandConstant('{userappdata}\PodMates');
    if DirExists(DataDir) then
      if SuppressibleMsgBox('要不要同时删除你的数据（设置、角色、人设、人物表、记忆、备份）？' + #13#10 + #13#10 +
         '选「否」会保留，以后重装还能接着用。' + #13#10 + '数据位置：' + DataDir,
         mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;
