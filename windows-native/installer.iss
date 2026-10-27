; ZDM 下载管理器 - 安装脚本
; 生成单文件安装包，含卸载注册、桌面与开始菜单快捷方式、开机自启选项。

#define AppName "ZDM 下载管理器"
#define AppNameEn "ZDM Download Manager"
#define AppVersion "1.18.11"
#define AppPublisher "ZDM"
#define AppExeName "Zdm.exe"
#define AppUrl "https://github.com/"

[Setup]
; AppId 保持与 1.1.0 一致，这样 1.11.0 会覆盖升级而不是并存两个版本
AppId={{7C5B8E11-4A2D-4F6B-9C3E-ZDM110000001}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\ZDM
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=no
OutputDir=..\dist
OutputBaseFilename=ZDM-Setup-{#AppVersion}
SetupIconFile=Assets\app.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Per-user install by default: no admin rights required.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "autostart"; Description: "开机自动启动后台服务"; GroupDescription: "其他选项"; Flags: unchecked

[Files]
Source: "publish\Zdm.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "publish\web\*"; DestDir: "{app}\web"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "publish\Assets\app.ico"; DestDir: "{app}\Assets"; Flags: ignoreversion
; 浏览器扩展随安装包一起落盘。Chrome 里「加载已解压的扩展程序」记的是磁盘路径，
; 目录一旦被移走或删掉，Chrome 会直接丢掉整个扩展条目（不是禁用），工具栏图标也一起消失。
; 放进安装目录，路径才稳定。
Source: "publish\extension\*"; DestDir: "{app}\extension"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\Assets\app.ico"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\Assets\app.ico"; Tasks: desktopicon

[Registry]
Root: HKA; Subkey: "Software\ZDM"; ValueType: string; ValueName: "InstallPath"; ValueData: "{app}"; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\ZDM"; ValueType: string; ValueName: "Version"; ValueData: "{#AppVersion}"; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "ZDM"; ValueData: """{app}\{#AppExeName}"" --autostart"; Tasks: autostart; Flags: uninsdeletevalue
; 程序内的「开机自启」开关也写同一个值；这行不创建任何东西，只保证卸载时把它删掉
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "ZDM"; Flags: uninsdeletevalue

; ---- zdm:// 协议：浏览器里的下载靠它唤醒（或转交给）本程序 ----
Root: HKA; Subkey: "Software\Classes\zdm"; ValueType: string; ValueName: ""; ValueData: "URL:ZDM Download Protocol"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\zdm"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\zdm\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"",0"
Root: HKA; Subkey: "Software\Classes\zdm\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%1"""

; ---- 原生消息（native messaging）：扩展直接拉起本程序，无需任何确认框 ----
; Chrome 只在「有用户手势」的页面跳转外部协议，扩展自己开的隐藏页会被静默
; 拦掉，所以真正靠谱的是这条通道。清单文件由下面的 --register 步骤写出。
Root: HKCU; Subkey: "Software\Google\Chrome\NativeMessagingHosts\com.zdm.launcher"; ValueType: string; ValueName: ""; ValueData: "{userappdata}\ZDM\com.zdm.launcher.json"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Microsoft\Edge\NativeMessagingHosts\com.zdm.launcher"; ValueType: string; ValueName: ""; ValueData: "{userappdata}\ZDM\com.zdm.launcher.json"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Chromium\NativeMessagingHosts\com.zdm.launcher"; ValueType: string; ValueName: ""; ValueData: "{userappdata}\ZDM\com.zdm.launcher.json"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\BraveSoftware\Brave-Browser\NativeMessagingHosts\com.zdm.launcher"; ValueType: string; ValueName: ""; ValueData: "{userappdata}\ZDM\com.zdm.launcher.json"; Flags: uninsdeletekey

[Run]
; 先无界面地把协议、原生消息清单、自启路径都注册一遍，
; 这样即使用户装完没立刻打开程序，浏览器那边也能正常唤醒。
Filename: "{app}\{#AppExeName}"; Parameters: "--register"; Flags: runhidden nowait
Filename: "{app}\{#AppExeName}"; Flags: nowait; Check: not WizardSilent
; 注意：应用内「一键更新」升级完的重启**不在这里做**。
; 放在这里需要 `Check: WizardSilent`，而静默安装（CI 的 Install and verify 也是
; /VERYSILENT）会真的把 ZDM 拉起来，把自检卡死。改由 UpdateInstaller.RunInstaller
; 起一个游离进程等安装器退出后再启动，见 windows-native/Update.cs。

[UninstallDelete]
Type: filesandordirs; Name: "{app}\web"
Type: filesandordirs; Name: "{app}\Assets"
; 扩展目录整棵删掉：卸载后 Chrome 里那条扩展会自己消失，不会留下指向空目录的残留条目。
Type: filesandordirs; Name: "{app}\extension"
Type: files; Name: "{userappdata}\ZDM\com.zdm.launcher.json"
[Code]
// 安装前先把正在运行的 Zdm.exe 结束掉（GUI / 后台服务 / 原生消息宿主
// 都是同一个映像名）。不做这一步，用户双击新安装包就会被「文件被占用」
// 卡住，只能开任务管理器手动结束（Windows 实测）；taskkill 找不到进程
// 时返回非零，属正常，不用处理。
// **不要加 /T**：应用内更新时 Setup 是 ZDM.exe 的子进程，/T 会把它一起
// 杀掉（用户看到的是「安装向导刚出来就自己关了」）。
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  RC: Integer;
begin
  Exec(ExpandConstant('{cmd}'),
    '/C taskkill /F /IM Zdm.exe & taskkill /F /IM ZDM.exe',
    '', SW_HIDE, ewWaitUntilTerminated, RC);
  Sleep(800);
  Result := '';
end;
