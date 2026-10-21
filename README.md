# ZDM — 跨平台多线程下载管理器

本地版的 [NeatDownloadManager](https://www.neatdownloadmanager.com/) 克隆：多线程分块下载、断点续传、暂停/继续、队列、限速，并自带浏览器扩展接管下载。

支持 **Linux / macOS / Windows** 三端，零依赖（仅标准库 + 桌面端 PyQt5），安装后重装系统重装仍可用。

当前版本：**v1.15.6**（桌面端与后端同一套版本号；浏览器扩展独立为 1.5.x）

## 仓库结构

| 路径 | 说明 |
|---|---|
| `zdm/` | 完整源码（引擎、服务、CLI、桌面端、浏览器扩展、安装脚本） |
| `zdm/zdm_launcher.m` | macOS 原生 App 启动器源码（`install.sh` 用 clang 编译成 `ZDM.app` 主程序） |
| `windows-native/` | 旧的 Windows C#/WPF 客户端（仅参考/回退，不再构建；Windows 已改用 PyQt5 那一套） |
| `dist/` | 三端正式安装包 |
| `docs/` | 安装与使用说明文档 |
| `build_dist.py` | 打包脚本：重新生成 `dist/` 下各平台安装包 |

## 安装包（推荐从 [Releases](https://github.com/feibixunchang/ZDM/releases) 下载）

| 平台 | 安装包 | 安装方式 |
|---|---|---|
| macOS | `ZDM-1.15.6.dmg` | 双击挂载，把 `ZDM.app` 拖进 Applications（内含扩展与中文说明） |
| macOS | `zdm-macos.zip` | 解压后 `bash install.sh`，自动生成 `ZDM.app` + 登录自启 |
| Linux | `zdm_1.15.6_amd64.deb` | `sudo apt install ./zdm_1.15.6_amd64.deb`（自带 Python + PyQt5，无需联网装依赖） |
| Linux | `zdm-linux-amd64.tar.gz` | 免 root：解压后 `./install.sh`，装到 `~/.local` |
| Windows | `ZDM-Setup-1.15.6.exe` | 双击安装（用户级、无需管理员、不依赖本机 Python，与 mac/Linux 同一份界面）；**软件内可直接一键更新** |
| 浏览器扩展 | 随上面各包附带 | Windows 装在 `{app}\extension`；macOS 在 `~/Library/Application Support/ZDM/extension`；装好后在 `chrome://extensions` 加载已解压的扩展程序 |

> **完全自动构建**：`.deb` / `zdm-linux.tar.gz` 在 `ubuntu-22.04` 上构建，
> `ZDM-<version>.dmg` 在 `macos-latest` 上构建，`ZDM-Setup-*.exe` 在 `windows-latest` 上构建（由
> `.github/workflows/build-packages.yml` 驱动），push main 即构建，推 tag 时产物自动挂到对应 Release。
>
> macOS 的 `.dmg` 与 Linux 的 `.deb` 都是**自包含**的：Python 运行时、PyQt5/Qt 与资源全部
> 打包在产物内，目标机器无需预装 Python、pip 或 `python3-pyqt5`。
> CI 在发布前会自检：macOS launcher 必须是 Mach-O 且 dyld 能解析内嵌 framework；
> Linux 的 `ZDM` 必须是 ELF、`--selfcheck` 必须报告内嵌 PyQt5/资源齐全、
> 所有 `.so` 无未解析依赖，并在 xvfb 里真实启动 GUI；`.deb` 还会在干净容器里
> `apt install` 一次再跑同样的自检。

每个包内自带 `extension/` 浏览器扩展，装完在 `chrome://extensions` 加载即可接管下载。

> macOS 用户注意：`~/Library` 默认隐藏，在「加载已解压的扩展程序」的选文件夹窗口里按
> `Command + Shift + G`，粘贴 `~/Library/Application Support/ZDM/extension` 即可直接定位。

## 构建

```bash
python3 build_dist.py   # Linux .deb / tar.gz + macOS zip + （在 Windows 上）ZDM-Setup-*.exe

# 只做 Linux 原生包（自带 CPython + PyQt5 的 ZDM onedir + .deb + 免 root tar.gz）
bash zdm/build_linux_app.sh ./linux-build 1.15.6
```

> Windows 只有一个安装包：**`ZDM-Setup-*.exe`**（约 100 MB，自带 CPython 与 PyQt5，
> 用户机器上不需要装 Python）。它和 macOS / Linux 用的是**同一份界面和同一个下载引擎**。

## Windows 客户端

Windows 端从 v1.15.0 起与 macOS / Linux **完全同一份代码**：
`gui.py` + `theme.py` + `zdm/` 引擎，由 `zdm/zdm.spec`（PyInstaller）冻结成 onedir，
再用 Inno Setup 封成安装包。构建脚本 `zdm/build_windows_app.ps1` 一条命令跑完。

> 为什么不再是独立的 C#/WPF 客户端：那套实现自带下载引擎和 HTTP API，
> 等于把同一个功能写了两遍。结果是 mac 上修好的 YouTube 解析、探大小、DASH 音频合流，
> Windows 一行都没生效。`windows-native/` 仅作为参考/回退保留，不再参与构建。

安装包为用户级安装（`%LOCALAPPDATA%\Programs\ZDM`，**不需要管理员权限**），装完自动完成：

- 注册 `zdm://` 协议，浏览器里点链接可直接把下载任务交给 ZDM；
- 注册 Chrome / Edge / Chromium / Brave 的 **Native Messaging** 宿主（`com.zdm.launcher`，
  指向随包的 `ZDM-NativeHost.exe`），**ZDM 没运行时**扩展一点下载就能把它拉起来；
- 把浏览器扩展解压到 `{app}\extension`，扩展的落盘路径由安装包保证；
- 「开机自启」写 `HKCU\...\Run`，值与程序内开关用的是同一条命令（`ZDM.exe serve`）。

**为什么是 Native Messaging 而不是 `zdm://`**：Chrome 只在**有用户手势**的页面里才允许跳转外部协议，
扩展自己开的隐藏标签页没有手势，`zdm://` 导航会被静默丢弃。所以扩展先
`chrome.runtime.connectNative("com.zdm.launcher")`，`zdm://` 只作兜底。

构建（在 Windows 上）：

```powershell
# venv + PyQt5 + PyInstaller + 下载 bin/yt-dlp.exe 与 bin/ffmpeg.exe + 冻结 + 自检 + 打安装包
powershell -ExecutionPolicy Bypass -File zdm\build_windows_app.ps1 -OutDir dist
```

依赖：Python 3.9+、网络可访问 PyPI 与 GitHub、Inno Setup 6（`iscc`）。
版本号唯一来源是 `zdm/zdm/__init__.py` 的 `__version__`，脚本自己读取，不需要手动对齐。

**升级提示**：扩展文件装在 `{app}\extension` 里，跨版本升级时该目录会被重写。
升级前先退出 Chrome，否则 Chrome 可能在目录重建的瞬间把扩展条目丢掉。

## 故障排查

**浏览器扩展怎么装？** 桌面端工具栏和托盘菜单都有「浏览器扩展」入口：它同时显示
①扩展是否已加载（由扩展发给本机服务的 `GET /api/extension/ping` 心跳回答，而非去猜
注册表/浏览器配置）、②一键打开扩展目录、③三步加载说明（`chrome://extensions` →
开发者模式 → 加载已解压的扩展程序 → 选扩展目录）。

**Windows 安装后打不开，报「Failed to load Python DLL '_internal\python312.dll' —
LoadLibrary: 内存资源不足」** —— 这**不是真的内存不足**（32G 也会报）。
v1.15.6 之前的真正根因是**进程无限拉起链**：`desktop.py::ensure_desktop` 拉起
窗口时漏传 `--gui`，冻结版 exe 只好落到 CLI 分支又再次拉起，十几秒堆出几十个
进程把资源耗尽，`LoadLibrary` 才报内存不足（事件查看器里是 ucrtbase.dll
BEX64 / 0xc0000409）。**升级到 v1.15.6 及以上即可根治。**

如果你停留在旧版本，两大环境因素也会触发同样的报错：

1. **杀毒软件在安装过程中隔离/篡改了 DLL**：打开杀毒软件的隔离区还原文件，
   并把 ZDM 安装目录加入信任/排除列表。v1.15.3 起安装器在装完时会自动跑一次
   `--selfcheck` 自检，失败当场弹出带处理指引的对话框，不用等第一次双击才发现。
2. **系统页面文件（虚拟内存）被关闭**：在「系统属性 → 高级 → 性能设置 → 虚拟内存」
   恢复为自动管理后重启。

处理后重新运行安装包覆盖安装一遍即可。

## macOS：真正的原生 App（v1.11.0 起）

`ZDM.app` 的主程序 `Contents/MacOS/ZDM` 是**用 clang 编译的原生 Mach-O**（源码 `zdm/zdm_launcher.m`），
它通过 Python C API 把CPython 解释器内嵌进 App 自身的进程，再运行 Qt 客户端——全程只有一个进程，
而且这个进程就是 `ZDM.app` 本身。

这样做是为了保住 macOS 应用身份：早期版本用「shell 脚本 + exec python」的方式启动，进程镜像会变成
`Python.app`，导致 Dock 显示通用占位图标、点 Dock 图标唤不回窗口（菜单栏托盘不受影响，因为那是
Qt 自身的逻辑）。改成原生主程序后，Dock 图标、点击唤回窗口、`applicationShouldHandleReopen:`
全部按 macOS 原生方式工作。

安装脚本会自动完成：编译 launcher → 把 `Python3.framework` 嵌入 `Contents/Frameworks/` →
生成完整 icns 图标（含全部 @1x/@2x）。若系统没有 clang，会自动回退到脚本启动器（功能可用，仅 Dock 行为受限）。

## 功能

- 多线程分块下载（动态切分，空闲连接抢占最大剩余块）
- 断点续传、暂停/继续、跨进程重启续传
- 队列与同时下载数限制、全局限速
- HTTP/2-only CDN 自动回退 curl 传输
- 桌面端单实例 + 系统托盘（右键含「开机自启」「退出」）
- 桌面端工具栏可一键「打开网页版」
- **每个任务行都有暂停 / 继续**：每行操作区最前面是 `⏸` / `▶` 图标按钮（主题感知绘制，
  深浅色都清晰），针对单个文件；已完成的行保留同位置的按钮但置灰（没有可暂停的东西），
  按钮不会随状态忽隐忽现
- **「全部暂停 / 全部继续」在主界面**：筛选标签右侧的中文按钮（`⏸ 全部暂停`、`▶ 全部继续`），
  没有可操作任务时自动置灰；托盘与右键菜单里也保留同名项
- **菜单栏图标弹出的是毛玻璃菜单**（macOS vibrancy）：背景模糊菜单后的内容、12px 圆角，
  浅色/深色自动匹配（VibrantLight / VibrantDark）；其它平台自动退回不透明菜单
- **「打开」打开的是文件本身**：下载完的 `.dmg` 双击直接进安装界面、`.pdf` 用默认阅读器打开；
  另设「目录」按钮 / 右键「在文件夹中显示」，用于定位文件
- **下载时自动拉起桌面端**：桌面端没开时，浏览器扩展（或 `zdm add`）把任务交给本地服务后
  会自动打开 ZDM 窗口；已在运行则直接激活。可在「设置」里关闭
- **版本号可见**：窗口标题、状态栏右下角、托盘提示、关于对话框都显示当前版本
- **更新提示 + 一键更新**：启动时静默检查 GitHub Releases，有新版本会在主界面顶部弹横幅
  （下载并安装 / 更新说明 / 忽略此版本），自动下载对应平台的安装包并打开安装程序；
  也可在「设置 → 立即检查更新」或托盘「检查更新」手动触发
- **深色 / 浅色模式**：默认**跟随系统外观**自动切换（macOS 读 AppleInterfaceStyle、
  Windows 读 AppsUseLightTheme、Linux 读 gsettings），也可在「设置 → 外观」手动锁定为
  浅色或深色。所有颜色统一取自一套调色板（`zdm/theme.py`），深浅两套完全一致，
  切换时表格、进度条、状态色等自定义绘制部分同步刷新，不留异色碎片。
- 浏览器扩展一键接管下载
- **视频 / 音频嗅探（一键推送下载）**：装好扩展后，网页右下角会出现 ZDM 浮动按钮，
  角标显示当前页面嗅探到的媒体数量。点开面板列出每一个视频 / 音频（图标区分、显示文件名与大小），
  点「推送」即交给 ZDM 下载。嗅探有两条来源并会合并去重：`webRequest` 响应头
  （能看到 Content-Type 与大小，覆盖播放 / 拖进度条 / 切清晰度时的请求）+ 页面 DOM 扫描
  （覆盖首屏就摆在那儿的 `<video>` / `<audio>` 直链媒体）。
  任务列表会标出来源（`[嗅探]` / `[浏览器]` / `[网页版]` / `[协议]`），顶部有「嗅探」筛选。
  *（HLS `.m3u8` 与 DASH `.mpd` 流媒体本期不支持，需要流媒体合并引擎，留待后续版本。）*
- **视频卡片上的「下载此视频」按钮**：B站 / YouTube 的每个视频卡片右上角，鼠标移上去
  会浮出一颗按钮，点开是该视频的**清晰度列表**（1080P / 720P / 音频…），选一个直接推给 ZDM。
  扩展就地解析页面里已加载的媒体数据（YouTube 的 `streamingData`、B站的 `__playinfo__`），
  不额外发请求。DASH 的画面与声音是分开的，ZDM 下载后用随包的 `ffmpeg` 合成有声音的视频
  （三端安装包都内置 `bin/ffmpeg`，不再依赖用户自己装；真缺了会在任务上写明「无声音」而不是静默成功）。
- **未启动也能接管**：ZDM 没开着时，浏览器里点下载会**自动把它拉起来**再接手任务
  （Windows 通过 Native Messaging → `ZDM-NativeHost.exe`，其他平台靠协议唤起）
- 开机自启（设置里可勾选，macOS / Windows / Linux 三端均支持）

详见 [`zdm/README.md`](zdm/README.md)；macOS 踩坑与修复记录见 [`docs/ZDM_安装与使用说明.md`](docs/ZDM_安装与使用说明.md)。
