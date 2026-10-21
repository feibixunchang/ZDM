; ZDM 下载管理器 - Windows 安装脚本（PyQt5 三端统一版）
;
; 由 zdm/build_windows_app.ps1 调用：
;   iscc /DAppVersion=1.15.5 /DSourceDir=dist\ZDM /DOutDir=dist installer.iss
;
; SourceDir 是 PyInstaller 产出的 onedir（ZDM.exe + _internal\ 整个运行时，
; 内含 CPython 与 PyQt5）。用户机器上不需要装 Python、不需要 pip、不需要 VC 运行库。
;
; AppId 刻意与旧的 C#/WPF 版保持一致：1.14.7 及更早的安装能被原地覆盖升级，
; 不会出现「新旧两个 ZDM 同时存在」。旧版留下的 Zdm.exe / *.dll 由
; [InstallDelete] 清掉。

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\ZDM"
#endif
#ifndef OutDir
  #define OutDir "..\dist"
#endif
; 扩展 ID 由构建脚本从 extension/manifest.json 的 "key" 现算后传入
; （sha256 前 32 位映射到 a-p）。这里的默认值是同算法的结果，仅作兜底。
#ifndef ExtId
  #define ExtId "mpodbnemeeghjpllgphpemhoogddfhjg"
#endif

#define AppName "ZDM 下载管理器"
#define AppExeName "ZDM.exe"
#define AppPublisher "ZDM"

[Setup]
AppId={{7C5B8E11-4A2D-4F6B-9C3E-ZDM110000001}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\ZDM
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=no
OutputDir={#OutDir}
OutputBaseFilename=ZDM-Setup-{#AppVersion}
SetupIconFile={#SourceDir}\_internal\extension\icons\icon.ico
UninstallDisplayIcon={app}\_internal\extension\icons\icon.ico
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 用户级安装，不需要管理员权限（与旧版一致，因此能在同一个目录上覆盖升级）
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; 升级时由 Restart Manager 关掉正在运行的旧进程，否则 ZDM.exe 被占用写不进去。
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "autostart"; Description: "开机自动启动后台服务"; GroupDescription: "其他选项"; Flags: unchecked

[InstallDelete]
; ---- 清理旧的 C#/WPF 客户端 ----
; 两个安装包用的是同一个目录，旧文件不删就会留下一个能启动的 Zdm.exe 和一堆
; WPF 的 dll，用户从桌面快捷方式点进去看到的还是老界面。
; 文件名在 Windows 上不区分大小写，{app}\ZDM.exe 与旧的 Zdm.exe 是同一个文件；
; InstallDelete 在复制新文件之前执行，所以随后写入的 ZDM.exe 不会被误删。
Type: files; Name: "{app}\ZDM.exe"
Type: files; Name: "{app}\*.dll"
Type: files; Name: "{app}\*.json"
Type: files; Name: "{app}\Zdm.exe"
Type: filesandordirs; Name: "{app}\runtimes"
Type: filesandordirs; Name: "{app}\Assets"
Type: filesandordirs; Name: "{app}\web"

[Files]
; 整个 onedir 原样落盘：ZDM.exe、ZDM-NativeHost.exe、_internal\（CPython、PyQt5、
; web、extension、bin\yt-dlp.exe、bin\ffmpeg.exe）。
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; 浏览器扩展单独放一份到 {app}\extension：Chrome 里「加载已解压的扩展程序」记的是
; 磁盘路径，目录一旦被移走，Chrome 会把整条扩展（含工具栏图标）直接丢掉。
; _internal\extension 是程序自己取图标用的，这里的副本才是给人加载的。
Source: "{#SourceDir}\_internal\extension\*"; DestDir: "{app}\extension"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; **必须带 gui 参数**。裸 ZDM.exe 在 CLI 里等于 `start`：前台跑服务 +
; 用浏览器打开网页版 —— 1.15.0 的快捷方式就是这样，用户更新后看到的是
; 「桌面界面没了，只剩网页版」。gui 子命令会先确保本机服务在跑，再开窗口。
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Parameters: "gui"; IconFilename: "{app}\_internal\extension\icons\icon.ico"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Parameters: "gui"; IconFilename: "{app}\_internal\extension\icons\icon.ico"; Tasks: desktopicon

[Registry]
Root: HKA; Subkey: "Software\ZDM"; ValueType: string; ValueName: "InstallPath"; ValueData: "{app}"; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\ZDM"; ValueType: string; ValueName: "Version"; ValueData: "{#AppVersion}"; Flags: uninsdeletevalue uninsdeletekeyifempty
; 与程序内「开机自启」开关写的是同一个值（autostart.py::_win_cmd_list 用
; `ZDM.exe serve`），两边必须一致，否则开关状态和实际行为对不上。
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "ZDM"; ValueData: """{app}\{#AppExeName}"" serve"; Tasks: autostart; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "ZDM"; Flags: uninsdeletevalue

; ---- zdm:// 协议：扩展在拿不到 HTTP 端口时用它唤醒本程序 ----
Root: HKA; Subkey: "Software\Classes\zdm"; ValueType: string; ValueName: ""; ValueData: "URL:ZDM Download Protocol"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\zdm"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\zdm\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"",0"
Root: HKA; Subkey: "Software\Classes\zdm\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%1"""

; ---- 原生消息：扩展直接拉起本程序，不弹任何「是否允许打开」确认框 ----
; 清单文件内容由 [Code] 段写出（需要展开 {app} 的真实路径）。
Root: HKCU; Subkey: "Software\Google\Chrome\NativeMessagingHosts\com.zdm.launcher"; ValueType: string; ValueName: ""; ValueData: "{app}\com.zdm.launcher.json"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Microsoft\Edge\NativeMessagingHosts\com.zdm.launcher"; ValueType: string; ValueName: ""; ValueData: "{app}\com.zdm.launcher.json"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Chromium\NativeMessagingHosts\com.zdm.launcher"; ValueType: string; ValueName: ""; ValueData: "{app}\com.zdm.launcher.json"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\BraveSoftware\Brave-Browser\NativeMessagingHosts\com.zdm.launcher"; ValueType: string; ValueName: ""; ValueData: "{app}\com.zdm.launcher.json"; Flags: uninsdeletekey

[Run]
; 只有交互安装才拉起界面。**不要**再加一条 `Check: WizardSilent` 的自动重启：
; 静默安装（应用内一键更新就是 /VERYSILENT，CI 也是）时那条会真的执行，
; Setup 会一直等这个 GUI 进程退出，装完永远不返回。
; 升级后的重启由应用自己在退出前安排（见 gui.py::_relaunch_after_update）。
Filename: "{app}\{#AppExeName}"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\extension"
Type: files; Name: "{app}\com.zdm.launcher.json"

[Code]
type
  TSystemInfo = record
    dwOemId: DWORD;
    dwPageSize: DWORD;
    lpMinimumApplicationAddress: Cardinal;
    lpMaximumApplicationAddress: Cardinal;
    dwActiveProcessorMask: Cardinal;
    dwNumberOfProcessors: DWORD;
    dwProcessorType: DWORD;
    dwAllocationGranularity: DWORD;
    wProcessorLevel: Word;
    wProcessorRevision: Word;
  end;

procedure GetNativeSystemInfo(var lpSystemInfo: TSystemInfo);
external 'GetNativeSystemInfo@kernel32.dll stdcall';

function HasNonAscii(const S: string): Boolean;
var
  i: Integer;
begin
  Result := False;
  for i := 1 to Length(S) do
    if Ord(S[i]) > 127 then
    begin
      Result := True;
      Exit;
    end;
end;

// 装完做一次「启动可行性」预检：把「Failed to load Python DLL / 内存资源不足」
// 这种没头没脑的报错，转成用户能直接照做的处理指引。覆盖三类已知真实原因：
// ① 本机是 ARM64 但安装包是 x64（骁龙等，x64 模拟层加载 python312.dll 会失败）；
// ② 系统关掉了「虚拟内存/页面文件」（Windows 靠它映射 DLL，关掉就报内存不足）；
// ③ 安装路径含中文/特殊字符。
// 这一检不调用 PyInstaller 引导器（那一步失败只会弹 PyInstaller 自己的英文框），
// 所以即便静默安装（应用内一键更新）也能落地一份中文诊断，而不是什么提示都没有。
procedure PreFlightCheck();
var
  si: TSystemInfo;
  nativeArch: Integer;
  paging: string;
  installDir: string;
  reasons: string;
begin
  reasons := '';
  try
    GetNativeSystemInfo(si);
    nativeArch := si.dwOemId and $FFFF;   // 低字即 wProcessorArchitecture
    if (nativeArch = 12) or (nativeArch = 5) then   // 12=ARM64, 5=ARM
      reasons := reasons + #13#10 +
        '• 本机是 ARM64 芯片（如高通骁龙 Snapdragon），而当前安装包是 x64。' +
        'x64 程序在 ARM 上靠模拟层运行，CPython 的 python312.dll 往往无法加载。' +
        '需要单独的 ARM64 安装包，请反馈需求或等待后续版本。' + #13#10;
  except
  end;

  if RegQueryStringValue(HKLM, 'SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management', 'PagingFiles', paging) then
  begin
    if (paging = '') or (Trim(paging) = '') then
      reasons := reasons + #13#10 +
        '• 系统「虚拟内存/页面文件」被关闭了。Windows 需要用它来映射 DLL，' +
        '关掉后即使内存很大也会报「内存资源不足」。' +
        '请开启：设置 → 系统 → 关于 → 高级系统设置 → 性能[设置] → 高级 →' +
        '虚拟内存[更改] → 勾选「自动管理所有驱动器的分页文件大小」。' + #13#10;
  end;

  installDir := ExpandConstant('{app}');
  if HasNonAscii(installDir) then
    reasons := reasons + #13#10 +
      '• 安装路径含中文/特殊字符（' + installDir + '），请安装到默认路径 C:\Program Files\ZDM。' + #13#10;

  if reasons <> '' then
  begin
    reasons := 'ZDM 可能无法启动，常见原因与解决办法：' + reasons + #13#10 + #13#10 +
      '若按上述处理后仍打不开，多半是杀毒软件隔离了 _internal\python312.dll，' +
      '请把它加入信任/排除列表后重装。';
    try
      SaveStringToFile(ExpandConstant('{app}\ZDM-启动诊断.txt'), reasons, False);
    except
    end;
    if not WizardSilent() then
      MsgBox(reasons, mbInformation, MB_OK);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Line: string;
  Rc: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    PreFlightCheck();
    Line := ExpandConstant('{app}') + '\ZDM-NativeHost.exe';
    StringChangeEx(Line, '\', '\\', True);
    // Chrome 只认 registry 里那个值指向的清单文件，清单里的 path 必须是 exe 的
    // 绝对路径（且不能带命令行参数），所以指向 ZDM-NativeHost.exe，而不是
    // 窗口版 ZDM.exe（窗口版没有 stdio）。JSON 里的反斜杠要转义成 \\。
    SaveStringToFile(
      ExpandConstant('{app}\com.zdm.launcher.json'),
      '{' + #13#10 +
      '  "name": "com.zdm.launcher",' + #13#10 +
      '  "description": "ZDM download manager launcher",' + #13#10 +
      '  "path": "' + Line + '",' + #13#10 +
      '  "type": "stdio",' + #13#10 +
      '  "allowed_origins": ["chrome-extension://' + '{#ExtId}' + '/"]' + #13#10 +
      '}' + #13#10,
      False);

    // ---- 装完立刻自检一遍 ----
    // PyInstaller 引导器若加载不了 _internal\python312.dll（典型原因：杀毒
    // 软件在安装过程中隔离/篡改了 DLL；个别机器是页面文件被关掉），用户要
    // 等第一次双击才会看到「Failed to load Python DLL / 内存资源不足」这种
    // 没头没脑的报错。这里当场验一遍，失败就直接给出可操作的指引。
    // 静默安装（应用内一键更新 / CI）**绝不**执行：那两个场景各有自己的
    // 验证，而且绝不能在静默路径上等待子进程 —— [Run]+WizardSilent 卡死
    // CI 的教训就在眼前。
    if not WizardSilent() then
    begin
      // 窗口版 exe 是 GUI 子系统，没有可写 stdout；--selfcheck 的 JSON 报告
      // 既会打印也会被 CI 忽略，这里只看退出码（0=通过）。
      if not Exec(
           ExpandConstant('{app}\ZDM.exe'), '--selfcheck',
           ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Rc)
         or (Rc <> 0) then
      begin
        MsgBox(
          '安装后的自检未通过（退出码 ' + IntToStr(Rc) + '），ZDM 可能无法启动。' + #13#10 +
          '最常见的原因是杀毒软件（Windows Defender / 360 / 腾讯管家等）在' + #13#10 +
          '安装过程中隔离或改动了 ZDM 的运行组件（如 _internal\python312.dll），' + #13#10 +
          '表现为启动时报「Failed to load Python DLL / 内存资源不足」——这并' + #13#10 +
          '不是真的内存不足。' + #13#10 + #13#10 +
          '建议：' + #13#10 +
          '1. 打开杀毒软件的「隔离区/信任区」，还原相关文件，并把 ZDM 安装目录' + #13#10 +
          '   （' + ExpandConstant('{app}') + '）加入信任或排除列表；' + #13#10 +
          '2. 若系统虚拟内存（页面文件）被关闭，请在「系统属性 → 高级 → 性能' + #13#10 +
          '   设置 → 虚拟内存」里恢复为自动管理；' + #13#10 +
          '3. 处理完成后重新运行本安装包覆盖安装一遍。',
          mbCriticalError, MB_OK);
      end;
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    // 协议注册整棵删掉，否则重装前浏览器还会把 zdm:// 交给一个不存在的程序。
    // [Registry] 里用的是 HKA（随权限自动选 HKCU/HKLM），这里按最低权限装的情况
    // 清 HKCU 那一份即可。
    RegDeleteKeyIncludingSubkeys(HKCU, 'Software\Classes\zdm');
  end;
end;
