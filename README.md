# ZDM — 跨平台多线程下载管理器

本地版的 [NeatDownloadManager](https://www.neatdownloadmanager.com/) 克隆：多线程分块下载、断点续传、暂停/继续、队列、限速，并自带浏览器扩展接管下载。

支持 **Linux / macOS / Windows** 三端，零依赖（仅标准库 + 桌面端 PyQt5），安装后重装系统重装仍可用。

当前版本：Python 三端 **v1.2.0** ｜ Windows 原生客户端 **v1.9.1**

## 仓库结构

| 路径 | 说明 |
|---|---|
| `zdm/` | 完整源码（引擎、服务、CLI、桌面端、浏览器扩展、安装脚本） |
| `zdm/zdm_launcher.m` | macOS 原生 App 启动器源码（`install.sh` 用 clang 编译成 `ZDM.app` 主程序） |
| `windows-native/` | **Windows 原生客户端**（C# / WPF / .NET），独立于 PyQt5 桌面端的 Windows 实现 |
| `dist/` | 三端正式安装包 |
| `docs/` | 安装与使用说明文档 |
| `build_dist.py` | 打包脚本：重新生成 `dist/` 下各平台安装包 |

## 安装包（推荐从 [Releases](https://github.com/feibixunchang/ZDM/releases) 下载）

| 平台 | 安装包 | 安装方式 |
|---|---|---|
| macOS | `ZDM-1.2.0.dmg` | 双击挂载，把 `ZDM.app` 拖进 Applications（内含扩展与中文说明） |
| macOS | `zdm-macos.zip` | 解压后 `bash install.sh`，自动生成 `ZDM.app` + 登录自启 |
| Linux | `zdm_1.2.0_all.deb` | `sudo dpkg -i zdm_1.2.0_all.deb`（自动装 PyQt5） |
| Linux | `zdm-linux.tar.gz` | 免 root：解压后 `./install.sh` |
| Windows | `ZDM-Setup-1.2.0.exe` | 双击运行，用户级安装、无需管理员 |
| Windows（原生） | `ZDM-Setup-1.9.1.exe` | C#/WPF 客户端，双击安装，见下方专节 |
| 浏览器扩展 | `ZDM-Connector-1.2.1.zip` | 解压后在 `chrome://extensions` 加载已解压的扩展程序 |

> **完全自动构建**：`.deb` / `zdm-linux.tar.gz` / `ZDM-Setup-*.exe` 在 `ubuntu-latest`
> 上构建，`ZDM-<version>.dmg` 在 `macos-latest` 上构建（由
> `.github/workflows/build-packages.yml` 驱动），push main 即构建，推 tag 时产物自动挂到对应 Release。
>
> macOS 的 `.dmg` 是**自包含**的：Python 运行时与含 PyQt5/PyObjC 的 venv 都嵌在
> `ZDM.app` 内，目标机器无需预装 Python，拖进 Applications 即可运行。
> CI 在发布前会自检：launcher 必须是 Mach-O、dyld 能解析内嵌 framework、
> 图标与版本正确，并真实启动 GUI 存活 12 秒。
>
> Linux 的 `.deb` 还会在 runner 上真实 `apt install` 一次，校验 CLI、桌面入口与图标。

每个包内自带 `extension/` 浏览器扩展，装完在 `chrome://extensions` 加载即可接管下载。

> macOS 用户注意：`~/Library` 默认隐藏，在「加载已解压的扩展程序」的选文件夹窗口里按
> `Command + Shift + G`，粘贴 `~/Library/Application Support/ZDM/extension` 即可直接定位。

## 构建

```bash
python3 build_dist.py   # 在 dist/ 重新生成安装包（.deb 需 dpkg-deb，.exe 需 7z）
```

## Windows 原生客户端（v1.9.1）

Windows 上除 PyQt5 版之外，还提供一套 **C# / WPF / .NET** 原生客户端，源码在 `windows-native/`。
安装包 `ZDM-Setup-1.9.1.exe` 为 Inno Setup 用户级安装，**不需要管理员权限**，装完自动完成：

- 注册 `zdm://` 协议，浏览器里点链接可直接把下载任务交给 ZDM；
- 注册 Chrome / Edge / Chromium / Brave 的 **Native Messaging** 宿主（`com.zdm.launcher`），
  **ZDM 没运行时**，扩展一点下载就能把它拉起来，再把任务接过去；
- 校验并修复「开机自启」注册项。

安装包参数 `Zdm.exe --register` 可在无界面下重做上述注册（安装器与「设置 → 浏览器自动唤醒」按钮走的是同一段代码）。

**为什么是 Native Messaging 而不是 `zdm://`**：Chrome 只在**有用户手势**的页面里才允许跳转到外部协议，
扩展自己开的隐藏标签页没有手势，`zdm://` 导航会被静默丢弃——这正是 1.8.0 自动唤醒失效的原因。
扩展改为先 `chrome.runtime.connectNative("com.zdm.launcher")` 拉起一个短命宿主进程，由宿主 `ShellExecute`
打开 `zdm://add?url=…`，`zdm://` 只作为兜底。

构建：

```bash
cd windows-native
dotnet publish Zdm.App.csproj -c Release -r win-x64 --self-contained -o publish
```

打包安装器需要 Inno Setup 6：`iscc installer.iss`（版本号在 `installer.iss` 的 `AppVersion` 与
`Zdm.App.csproj` 的 `<Version>` 两处保持一致）。

## macOS：真正的原生 App（v1.2.0 起）

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
- **深色 / 浅色模式**：默认**跟随系统外观**自动切换（macOS 读 AppleInterfaceStyle、
  Windows 读 AppsUseLightTheme、Linux 读 gsettings），也可在「设置 → 外观」手动锁定为
  浅色或深色。所有颜色统一取自一套调色板（`zdm/theme.py`），深浅两套完全一致，
  切换时表格、进度条、状态色等自定义绘制部分同步刷新，不留异色碎片。
- 浏览器扩展一键接管下载
- **未启动也能接管**：ZDM 没开着时，浏览器里点下载会**自动把它拉起来**再接手任务（Windows 原生客户端
  通过 Native Messaging 实现，`zdm://` 协议兜底；其他平台靠协议唤起）
- 开机自启（设置里可勾选，macOS / Windows / Linux 三端均支持）

详见 [`zdm/README.md`](zdm/README.md)；macOS 踩坑与修复记录见 [`docs/ZDM_安装与使用说明.md`](docs/ZDM_安装与使用说明.md)。
