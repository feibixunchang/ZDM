# ZDM — 跨平台多线程下载管理器

本地版的 [NeatDownloadManager](https://www.neatdownloadmanager.com/) 克隆：多线程分块下载、断点续传、暂停/继续、队列、限速，并自带浏览器扩展接管下载。

支持 **Linux / macOS / Windows** 三端，零依赖（仅标准库 + 桌面端 PyQt5），安装后重装系统重装仍可用。

当前版本：**v1.1.0**

## 仓库结构

| 路径 | 说明 |
|---|---|
| `zdm/` | 完整源码（引擎、服务、CLI、桌面端、浏览器扩展、安装脚本） |
| `zdm/zdm_launcher.m` | macOS 原生 App 启动器源码（`install.sh` 用 clang 编译成 `ZDM.app` 主程序） |
| `dist/` | 三端正式安装包 |
| `docs/` | 安装与使用说明文档 |
| `build_dist.py` | 打包脚本：重新生成 `dist/` 下各平台安装包 |

## 安装包（推荐从 [Releases](https://github.com/feibixunchang/ZDM/releases) 下载）

| 平台 | 安装包 | 安装方式 |
|---|---|---|
| macOS | `ZDM-1.1.0.dmg` | 双击挂载，把 `ZDM.app` 拖进 Applications（内含扩展与中文说明） |
| macOS | `zdm-macos.zip` | 解压后 `bash install.sh`，自动生成 `ZDM.app` + 登录自启 |
| Linux | `zdm_1.1.0_all.deb` | `sudo dpkg -i zdm_1.1.0_all.deb`（自动装 PyQt5） |
| Linux | `zdm-linux.tar.gz` | 免 root：解压后 `./install.sh` |
| Windows | `ZDM-Setup-1.1.0.exe` | 双击运行，用户级安装、无需管理员 |

> **自动构建**：`.deb` / `zdm-linux.tar.gz` / `ZDM-Setup-*.exe` 由 GitHub Actions
> （`.github/workflows/build-packages.yml`）在每次 push main 与推 tag 时自动构建，
> 推 tag 时产物直接挂到对应 Release。CI 会在容器里真实安装一次 `.deb`，
> 校验 CLI、桌面入口与图标后才发布。
> macOS 的 `.dmg` 依赖 `hdiutil` 与 `clang`，需在 macOS 上构建后提交到 `dist/`。

每个包内自带 `extension/` 浏览器扩展，装完在 `chrome://extensions` 加载即可接管下载。

> macOS 用户注意：`~/Library` 默认隐藏，在「加载已解压的扩展程序」的选文件夹窗口里按
> `Command + Shift + G`，粘贴 `~/Library/Application Support/ZDM/extension` 即可直接定位。

## 构建

```bash
python3 build_dist.py   # 在 dist/ 重新生成安装包（.deb 需 dpkg-deb，.exe 需 7z）
```

## macOS：真正的原生 App（v1.1.0 起）

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
- 浏览器扩展一键接管下载

详见 [`zdm/README.md`](zdm/README.md)；macOS 踩坑与修复记录见 [`docs/ZDM_安装与使用说明.md`](docs/ZDM_安装与使用说明.md)。
