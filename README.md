# ZDM — 跨平台多线程下载管理器

本地版的 [NeatDownloadManager](https://www.neatdownloadmanager.com/) 克隆：多线程分块下载、断点续传、暂停/继续、队列、限速，并自带浏览器扩展接管下载。

支持 **Linux / macOS / Windows** 三端，零依赖（仅标准库 + 桌面端 PyQt5），安装后重装系统重装仍可用。

## 仓库结构

| 路径 | 说明 |
|---|---|
| `zdm/` | 完整源码（引擎、服务、CLI、桌面端、浏览器扩展、安装脚本） |
| `dist/` | 三端正式安装包 |
| `build_dist.py` | 打包脚本：重新生成 `dist/` 下各平台安装包 |

## 安装包（直接下载使用）

`dist/` 目录内含：

- `zdm-linux.tar.gz` — Linux 免 root 安装（解压后 `./install.sh`）
- `zdm_1.0.0_all.deb` — Linux deb 包（`sudo dpkg -i`）
- `zdm-macos.zip` — macOS（解压后 `bash install.sh`，自动生成 `ZDM.app` + 登录自启）
- `zdm-windows.zip` — Windows（右键 `install.ps1` 用 PowerShell 运行，或双击 `install.bat`）

每个包内自带 `extension/` 浏览器扩展，装完在 `chrome://extensions` 加载即可接管下载。

## 构建

```bash
python3 build_dist.py   # 在 dist/ 重新生成四个安装包
```

## 功能

- 多线程分块下载（动态切分，空闲连接抢占最大剩余块）
- 断点续传、暂停/继续、跨进程重启续传
- 队列与同时下载数限制、全局限速
- HTTP/2-only CDN 自动回退 curl 传输
- 桌面端单实例 + 系统托盘（右键含「开机自启」「退出」）
- 浏览器扩展一键接管下载

详见 [`zdm/README.md`](zdm/README.md)。
