# ZDM — 本机多线程下载管理器

零依赖（只用 Python 标准库），带本地 Web 管理界面 + Chrome/Edge 扩展，功能对标 Neat DownloadManager。

## 特性

| 能力 | 说明 |
|---|---|
| 多连接分段下载 | HTTP Range 动态分段，最多 32 连接，连接空闲自动抢占剩余最大分片 |
| 断点续传 | `.part` + `.part.zdm` 元数据，暂停/重启服务/崩溃后都能续传 |
| 队列调度 | 并发任务数可配，排队自动开始 |
| 全局限速 | token bucket，KB/s 可调 |
| 实时界面 | SSE 推送，进度/速度/剩余时间实时刷新 |
| 桌面客户端 | 原生 Qt 窗口（PyQt5）：任务列表、托盘、拖放链接、右键菜单、设置 |
| 浏览器接管 | Chrome/Edge 扩展：自动接管下载、右键「使用 ZDM 下载」 |
| CLI | `zdm add/list/pause/resume/cancel/restart/remove/config` |
| 无 Range 服务器 | 自动降级单连接流式下载 |

## 安装（Linux / macOS / Windows）

三端都是**用户级安装，不需要管理员/root 权限**，装完就能用。

> 想在其他电脑上安装？直接拿对应的**安装包**（见仓库根 `dist/` 目录）：
> `zdm-linux.tar.gz`（或 `zdm_1.0.0_all.deb`）、`zdm-macos.zip`、`zdm-windows.zip`，
> 解压后跑里面的安装脚本即可，无需从源码复制。

### Linux（Deepin / Ubuntu / Arch …）

```bash
cd zdm
./install.sh                 # 安装 + 开机自启 + 启动服务
./install.sh --systemd       # 改用 systemd user service 常驻
./install.sh --uninstall     # 卸载
```

安装后：命令 `~/.local/bin/zdm`，应用菜单「ZDM 下载管理器」。

### macOS

```bash
cd zdm
./install.sh
```

- 代码装到 `~/Library/Application Support/ZDM`
- 后台服务注册为 launchd agent（`~/Library/LaunchAgents/com.zdm.service.plist`）：登录自启、崩溃自动重启
- 生成 `~/Applications/ZDM.app`（含 icns 图标、已去除隔离属性，双击即用）
- 命令行 `~/.local/bin/zdm`（需要把 `~/.local/bin` 加进 PATH）

### Windows

双击 **`install.bat`**（或管理员无关的 PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

- 代码装到 `%LOCALAPPDATA%\ZDM`，并把该目录加入用户 PATH（命令 `zdm`）
- 开始菜单生成「ZDM 下载管理器」「ZDM 网页界面」两个快捷方式
- 开机自启：启动文件夹里的 `ZDM Service.lnk`（用 `pythonw.exe` 无黑窗）
- 没有 Python 时会尝试用 `winget` 装；桌面端需要 PyQt5，脚本会自动 `pip install --user PyQt5`，
  装不上则桌面端自动回退到网页界面
- 首次启动若 Windows 防火墙询问 python，允许「专用网络」即可（服务只监听 127.0.0.1）

### 重装系统后

重新跑一次对应的安装脚本，再重新加载一次浏览器扩展，即可完全恢复：
下载目录、连接数、限速等设置会按平台默认重建，未完成的任务元数据随 `.part` 文件保留，
服务起来后会自动继续。

## 三种入口

| 入口 | 命令 | 说明 |
|---|---|---|
| 桌面端 | `zdm gui` | 原生 Qt 窗口，关闭窗口收进托盘 |
| 网页端 | `zdm open` 或 <http://127.0.0.1:9000/> | 浏览器里管理，适合远程/多设备 |
| 命令行 | `zdm add/list/...` | 脚本与终端使用 |

三者共享同一个后台服务与任务队列，可同时打开。

桌面端是**单实例**：再次点击图标只会唤醒已运行窗口（顺带把命令行里的链接加进队列），不会开第二个窗口；
关闭窗口会收进系统托盘，下载继续在后台跑。

托盘图标**右键菜单**：显示/隐藏、新建下载、打开下载目录、**开机自启（可勾选，实时开关）**、退出。
「开机自启」与「退出」同样在**设置窗口**底部提供：勾选「开机自动启动 ZDM 服务」即可让三端在登录时
自动拉起后台服务（分别写入 Linux `~/.config/autostart/zdm.desktop`、macOS LaunchAgent、
Windows 注册表 Run 键）；「退出」会干净地关闭桌面端与轮询线程（后台服务仍在，可用 `zdm start` 再拉起）。

## 浏览器扩展（三平台步骤相同）

1. `chrome://extensions` → 开启右上角「开发者模式」
2. 「加载已解压的扩展程序」→ 选择安装目录下的 `extension` 文件夹：

   | 平台 | 扩展目录 |
   |---|---|
   | Linux | `~/.local/share/zdm/extension` |
   | macOS | `~/Library/Application Support/ZDM/extension` |
   | Windows | `%LOCALAPPDATA%\ZDM\extension` |

3. 点扩展图标 → ⚙ → 「测试连接」→ 勾选「自动接管浏览器下载」

工作方式：扩展监听 `chrome.downloads.onCreated`，**先把下载项暂停**，把 URL 发给本机
`127.0.0.1:9000`，确认 ZDM 接手后才取消浏览器自身的下载。Chrome/Edge/Brave 等 Chromium 内核通用。

ZDM 没开着时（扩展 **v1.2.1** 起）：

- **Windows**：扩展通过 Native Messaging（宿主 `com.zdm.launcher` → 随包的
  `ZDM-NativeHost.exe`）直接拉起 ZDM，不弹任何确认框。这是唯一可靠的方式——Chrome 只允许
  **有用户手势**的页面跳转外部协议，扩展自己开的隐藏页会被静默拦掉，所以 `zdm://` 只作兜底。
- 其他情况：扩展先尝试 `zdm://add?url=…`，浏览器会弹一次「是否打开 ZDM」，确认后即可。

两条路都失败时，扩展会把暂停的下载**恢复**，浏览器照常自己下载，不会丢文件。

## 平台差异与注意

| 事项 | Linux | macOS | Windows |
|---|---|---|---|
| 后台服务自启 | autostart / systemd user | launchd LaunchAgent | 启动文件夹 `pythonw` 快捷方式 |
| 桌面端 | PyQt5 | PyQt5 | PyQt5 |
| 分片写入 | `os.pwrite` | `os.pwrite` | 无 `pwrite` 时用每线程独立句柄 `lseek+write` |
| 预分配空间 | `posix_fallocate` | 不支持 → `ftruncate` | 不支持 → `ftruncate` |
| 配置目录 | `~/.config/zdm` | `~/Library/Application Support/ZDM` | `%APPDATA%\ZDM` |
| 打开文件位置 | `xdg-open` | `open -R` | `explorer /select,` |
| 文件名清洗 | `/ \` | `/ :` | `\ / : * ? " < > |` 与保留名 |
| 防火墙提示 | 无 | 无 | 首次可能被问，允许专用网络 |

## 代理

- 默认自动读取环境变量 `http_proxy` / `https_proxy` / `all_proxy`（走 CONNECT 隧道）。
- 也可在设置界面或 `zdm config proxy http://127.0.0.1:7890` 显式指定；留空 = 走环境变量。
- 部分 CDN 只对 HTTP/2 客户端返回 200，纯标准库客户端会被 403；检测到这种情况会自动切换到
  系统 `curl` 作为传输后端（仍保持多线程分片），无需配置。

## CLI

```bash
zdm start --daemon                       # 后台启动
zdm status                               # 服务与任务状态
zdm add "https://example.com/a.iso" -c 16 -o ~/Downloads
zdm add URL -H "Referer: https://site/page" -H "Cookie: k=v"
zdm add URL -p 10                     # 优先级：数字越大越先下载（±99 之外按边界夹取）
zdm add URL -c 16                     # 连接数 1~32；越界会被夹到边界并在回执里写明
zdm add URL --start-at 2026-10-27T02:00:00   # 定时开始（也接受 Unix 时间戳）
zdm add "https://www.bilibili.com/video/BV..." --section 00:01:00-00:05:00
                                       # --section 只适用于视频页面链接，普通文件会被拒绝
                                       # 写法也校验：结束必须晚于开始，MM:SS / HH:MM:SS 都认
zdm list --json
zdm pause <id> / zdm resume <id> / zdm restart <id> / zdm remove <id>
zdm config connections 16               # 1~32
zdm config speed_limit 2097152          # 2 MB/s，0 = 不限
zdm config mirror_accel true            # GitHub / Hugging Face 镜像加速（默认关）
zdm config clipboard_monitor true       # 复制 http(s) 链接后填入新建下载输入框
zdm config mirror_accel off             # 关；认 true/false/yes/no/on/off
zdm config stream_engine re             # HLS/DASH 引擎：auto(默认) / re / ffmpeg
                                        # auto=优先 N_m3u8DL-RE，找不到或失败时退回 ffmpeg
                                        # re=只用它（找不到可执行文件就报错，不假装按你选的做完了）
                                        # ffmpeg=只用 ffmpeg
                                        # 换了引擎的任务会在列表里写明理由（stream_note）
                                        # 写坏一项（zdm config port abc）会退出码 1 并指名是哪一项，
                                        # 且这一批配置整体不生效，不会改一半留一半
zdm stop
```

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/state` | 配置 + 任务列表 |
| GET | `/api/tasks` | 任务列表 |
| POST | `/api/tasks` | `{url, filename?, dir?, connections?, headers?, proxy?, speed_limit?, paused?, priority?, start_at?, section?}` |
| POST | `/api/tasks/{id}/edit` | 改 `{url?, headers?, proxy?, connections?, priority?, start_at?, section?}`，已下载进度尽量保留 |
| POST | `/api/tasks/{id}/pause\|resume\|restart\|cancel\|open` | 任务操作 |
| DELETE | `/api/tasks/{id}?files=0` | 删除任务（`files=0` 保留文件） |
| POST | `/api/config` | 修改配置 |
| GET | `/api/events` | SSE 实时推送 |

## 默认配置

不限速（`speed_limit = 0`）、单任务 8 连接、同时 3 个任务、下载目录 `~/Downloads`。
需要限速时在桌面端「设置」里填 KB/s，或用 `zdm config speed_limit 2097152`（2 MB/s）。

## 配置项（`~/.config/zdm/state.json`）

`download_dir`、`connections`(8)、`max_active`(3)、`speed_limit`(bytes/s)、`proxy`、
`max_retries`(5)、`timeout`(30)、`port`(9000)、`auto_resume`、`clipboard_monitor`、
`mirror_accel`、`stream_engine`(`auto`)。

`stream_engine` 只管 HLS `.m3u8` / DASH `.mpd` 这类分片流：`auto` 先用 N_m3u8DL-RE
（分片并发、单片重试、暂停后按分片续传），找不到它的可执行文件或它失败时用 ffmpeg
重下一遍；`re` / `ffmpeg` 是点名只用一个。降级或换引擎的理由会挂在任务上（`stream_note`），
桌面端、网页版和 `/api/tasks` 都看得到，不会静默换个引擎说「按你选的做好了」。

## 结构

```
zdm.py              入口
gui.py              桌面客户端（PyQt5，独立运行，走 HTTP API）
install.sh          Linux / macOS 安装器
install.ps1          Windows 安装器（install.bat 双击入口）
zdm/http.py         轻量 HTTP 客户端（Range / 重定向 / 超时）
zdm/core.py         分块下载引擎（Segmenter / RateLimiter / Download）
zdm/manager.py      队列、调度、持久化、事件
zdm/autostart.py    跨平台登录自启开关（Linux/macOS/Windows）
zdm/server.py       REST + SSE + 静态 UI
zdm/cli.py          命令行
web/index.html      Web 管理界面
extension/          Chrome MV3 扩展
```
