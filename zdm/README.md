# ZDM — 本机多线程下载管理器

零依赖（只用 Python 标准库；只有 `sftp://` 通道另需 `paramiko`，三端安装包已内置，从源码跑
`install.sh` 会顺带装上），带本地 Web 管理界面 + Chrome/Edge 扩展，功能对标 Neat DownloadManager。

## 特性

| 能力 | 说明 |
|---|---|
| 多连接分段下载 | HTTP Range 动态分段，最多 32 连接，连接空闲自动抢占剩余最大分片 |
| 多协议 | 除 http/https，还下 `ftp://`、`ftps://`、`sftp://`：同一套分块与断点续传，不是另开的第二条通路 |
| 断点续传 | `.part` + `.part.zdm` 元数据，暂停/重启服务/崩溃后都能续传 |
| 队列调度 | 并发任务数可配，排队自动开始 |
| 全局限速 | token bucket，KB/s 可调 |
| 实时界面 | SSE 推送，进度/速度/剩余时间实时刷新 |
| 桌面客户端 | 原生 Qt 窗口（PyQt5）：任务列表、托盘、拖放链接、右键菜单、设置 |
| 浏览器接管 | Chrome/Edge/Firefox 扩展：自动接管下载、右键「使用 ZDM 下载」（Firefox 用同一份源码打 `.xpi`） |
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

### Firefox（v1.18.0）

Firefox 没有「加载已解压的扩展程序」，只认打包好的 `.xpi`。**源码仍是同一份
`extension/`**，差别只在清单：`zdm/zdm/firefox_ext.py` 把 Chrome 特有的字段（`key`、
`minimum_chrome_version`、`downloads.open` 权限、`background.service_worker`）换成
Firefox 的写法，再打成 zip。不另存一份 JS，就不会出现「Chrome 修了 bug、Firefox 还带着
坏版本」。

拿 `.xpi` 有三个入口，任选一个：

| 入口 | 操作 |
|---|---|
| 桌面端 | 「浏览器扩展」对话框 → 「导出 Firefox 扩展」→ 选保存位置 |
| 网页版 | 页面底部「下载 Firefox 版扩展（.xpi）」（`GET /api/extension/firefox.xpi`，现场打包） |
| 命令行 | `zdm firefox-ext --out zdm-connector.xpi` |

装：Firefox 地址栏打开 `about:debugging#/runtime/this-firefox` → 「加载临时附加组件…」→
选那个 `.xpi`。

要知道的边界（都是 Firefox 侧的规则，不是 ZDM 的取舍）：

- **临时加载的扩展浏览器重启后会被移除**，每次开机要重新加载一次。要永久安装，得走
  Firefox 附加组件商店签名，或用 Dev Edition / Nightly / ESR 并把
  `xpinstall.signatures.required` 设为 `false`。
- 临时加载会分配一个**一次性的扩展 ID**，原生消息主机清单里那个固定 ID
  （`zdm-connector@feibixunchang.github.io`）对不上，所以「自动唤醒 ZDM」在临时加载下
  不生效：请先开着 ZDM，或用 `zdm://` 协议那条兜底路径。接管下载、网页视频嗅探、推送到
  本机服务这些都不受影响（它们走 HTTP，与扩展 ID 无关）。
- 永久安装（签名包 / Dev Edition）时，`install.sh`、`.deb`、Windows 安装包都已把
  Firefox 那棵原生消息注册表/目录一并写好，唤醒能力和 Chrome 一样。
- 最低 Firefox 115（`browser_specific_settings.gecko.strict_min_version`）。
- Promise 形式的 API 在 Firefox 只挂在 `browser.*` 上（`chrome.*` 那边只有回调），
  扩展里统一用一个 `EXT_API` 挑「返回 Promise 的那一套」；事件注册和回调式调用继续用
  `chrome.*`，两边都成立。

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
- **代理设置只作用于 http/https**。`ftp://` / `ftps://` / `sftp://` 走各自的协议通道，
  设置里那个代理字段对它们无效（不是「静默走直连当成功」——链接照下，只是别指望它翻代理）。

## FTP / FTPS / SFTP（v1.18.0）

链接直接粘进新建下载框即可，账号密码写在链接里：

```
ftp://user:pass@files.example.com/pub/big.iso
ftps://user:pass@files.example.com/pub/big.iso     # 显式 AUTH TLS（21）
ftps://user:pass@files.example.com:990/pub/big.iso # 隐式 TLS（要写端口）
sftp://user:pass@backup.example.com/data/big.iso
sftp://user@backup.example.com/data/big.iso        # 不写密码 = 交给 SSH 的密钥/agent
```

| 事项 | 说明 |
|---|---|
| 依赖 | ftp/ftps 只用标准库；sftp 需要 `paramiko`（缺了会在「添加」这一步就说清缺什么、怎么补） |
| 分块与续传 | 长度问 FTP 的 `SIZE` / SFTP 的 `stat`；FTP 用 `REST` 定位，SFTP 用协议自带的 seek |
| 服务器不配合 | 不认 `SIZE` 就显示「大小未知」，不认 `REST` 就退回单流顺序下载；绝不假装能续传 |
| TLS 校验 | `ftps://` 校验证书链，自签/链不完整一律连不上，不会退化成明文接着下 |
| 主机密钥 | SFTP 首次连接按 TOFU 记进配置目录里的 `sftp-known_hosts`（不动你的 `~/.ssh/known_hosts`）；之后密钥变了直接拒绝并说明怎么解除 |
| 目录 | 指向目录而不是文件会被明确拒绝（协议支持列目录，ZDM 的下载任务对应单个文件） |
| 密码含特殊字符 | 按 URL 规则百分号编码，例如密码里的 `@` 写成 `%40`、`/` 写成 `%2F` |

## 英文界面（v1.18.0）

网页版和浏览器扩展都是中英双语，默认跟随浏览器语言，也可以手动指定：

| 位置 | 怎么切 | 存在哪 |
|---|---|---|
| 网页版（`http://127.0.0.1:9000/`） | 右上角「界面语言 / Language」 | `localStorage` 的 `zdm_ui_lang` |
| 网页版链接 | 直接开 `/?lang=en`（或 `?lang=zh`） | 只影响这一次打开 |
| 扩展 | 扩展设置页「界面语言」 | `chrome.storage.sync`（跟着浏览器账号同步） |

扩展那一处覆盖 popup、设置页、唤醒中间页、网页里的嗅探面板 / 播放器按钮 / 清晰度菜单，以及
系统通知和右键菜单；Firefox 与 Chrome 用的是同一份语言文件。

没翻的部分要说明白：**Qt 桌面端**和诊断日志（`download-errors.log`、任务日志）在这一版仍是中文。
后端返回的报错里，认得出的（参数校验、FTP/SFTP、状态冲突、状态词）会跟着翻，认不出的一律原样
显示中文 —— 这套界面不做「猜不出就编一个英文」的事。

## CLI

```bash
zdm start --daemon                       # 后台启动
zdm status                               # 服务与任务状态
zdm add "https://example.com/a.iso" -c 16 -o ~/Downloads
zdm add "ftp://user:pass@files.example.com/pub/big.iso"   # ftp/ftps/sftp 同样分块、同样续传
zdm add URL -H "Referer: https://site/page" -H "Cookie: k=v"
zdm add URL -p 10                     # 优先级：数字越大越先下载（±99 之外按边界夹取）
zdm add URL -c 16                     # 连接数 1~32；越界会被夹到边界并在回执里写明
zdm add URL --start-at 2026-10-27T02:00:00   # 定时开始（也接受 Unix 时间戳）
zdm add "https://www.bilibili.com/video/BV..." --section 00:01:00-00:05:00
                                       # --section 只适用于视频页面链接，普通文件会被拒绝
                                       # 写法也校验：结束必须晚于开始，MM:SS / HH:MM:SS 都认
zdm playlist "https://www.youtube.com/playlist?list=PL..."     # 只列名单，不建任务
zdm playlist URL --max-items 300        # 展开上限：默认 100，最高 500
zdm add URL --playlist                  # 整份清单都下
zdm add URL --playlist 1-20,25 --paused # 只下这些序号；范围写法与网页版/桌面端同一份实现
zdm add URL --playlist 1-50 --no-subdir # 默认按清单标题建子目录，--no-subdir 并进 -o 的目录
                                       # 清单地址不加 --playlist 直接建任务会被拒绝
                                       # （引擎对播放列表输出固定文件名，一集覆盖一集）
zdm list --json
zdm pause <id> / zdm resume <id> / zdm restart <id> / zdm remove <id>
zdm config connections 16               # 1~32
zdm config speed_limit 2097152          # 2 MB/s，0 = 不限
zdm config mirror_accel true            # GitHub / Hugging Face 镜像加速（默认关）
zdm config clipboard_monitor true       # 复制下载链接（http/ftp/sftp）后填入新建下载输入框
zdm config mirror_accel off             # 关；认 true/false/yes/no/on/off
zdm config stream_engine re             # HLS/DASH 引擎：auto(默认) / re / ffmpeg
                                        # auto=优先 N_m3u8DL-RE，找不到或失败时退回 ffmpeg
                                        # re=只用它（找不到可执行文件就报错，不假装按你选的做完了）
                                        # ffmpeg=只用 ffmpeg
                                        # 换了引擎的任务会在列表里写明理由（stream_note）
zdm config archive_rules "mp4,mkv = Video
srt,ass = Subtitles"                    # 按后缀自动归档；整段留空 = 关闭（默认）
                                        # 每行「后缀 = 子目录」，`#` 开头的行跳过
                                        # 子目录必须是下载目录里的相对路径（`/x`、`D:\x`、`~/x` 会被拒）
                                        # 同一个后缀写在两条规则里也会被拒，并点名是哪两条
                                        # 写坏一项（zdm config port abc）会退出码 1 并指名是哪一项，
                                        # 且这一批配置整体不生效，不会改一半留一半
zdm archive                             # 按上面的规则清点**已经下好**的文件：只列清单，不动手
zdm archive --apply                     # 看过清单了，真搬（撞名只跳过、不自动改名）
zdm archive -o /Volumes/移动盘/剧集      # 整理别的目录（必须是下载目录或某个任务的目录）
zdm archive --json                      # 给脚本看的完整报告（moves / skipped / failed）
zdm stop
```

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/state` | 配置 + 任务列表 |
| GET | `/api/tasks` | 任务列表 |
| POST | `/api/tasks` | `{url, filename?, dir?, connections?, headers?, proxy?, speed_limit?, paused?, priority?, start_at?, section?}`；清单 / 合集地址在这里会被 400 拒绝，要走下面的 playlist 通道 |
| POST | `/api/playlist/preview` | `{url, max_items?}` → `{items:[{index,title,duration,url}], count, skipped, truncated, preview_truncated, limit, title, playlist_url}`：只展开名单，不建任务（`items` 最多回 `PLAYLIST_PREVIEW_MAX` 条，`count` 才是真实条数） |
| POST | `/api/playlist/add` | `{url, max_items?, range?, indices?, subdir?, dir?, priority?, start_at?, paused?, cookie?, source?}` → `{added, ids, failed:[{index,error}], directory, count, skipped, truncated, limit}`：一次建 N 条独立任务 |
| POST | `/api/tasks/{id}/edit` | 改 `{url?, headers?, proxy?, connections?, priority?, start_at?, section?}`，已下载进度尽量保留 |
| POST | `/api/tasks/{id}/pause\|resume\|restart\|cancel\|open` | 任务操作 |
| DELETE | `/api/tasks/{id}?files=0` | 删除任务（`files=0` 保留文件） |
| POST | `/api/config` | 修改配置 |
| POST | `/api/archive/sweep` | `{apply?, directory?}` → 按后缀规则一次性整理**已经下好**的文件。默认只列清单（`moves:[{name,sub,src,dst,size}]` + `skipped` + `rules` + `truncated`），`apply:true` 才真搬并回 `moved`/`failed`。`directory` 只接受下载目录或某个任务自己的目录（越界 403）；在跑/暂停中的任务持有的文件、半截 `.part`、子目录里的文件都不动，目标撞名只跳过 |
| GET | `/api/events` | SSE 实时推送 |
| GET | `/api/extension/firefox.xpi` | 把随包扩展现场打成 Firefox 的 `.xpi`（需令牌） |

## 默认配置

不限速（`speed_limit = 0`）、单任务 8 连接、同时 3 个任务、下载目录 `~/Downloads`。
需要限速时在桌面端「设置」里填 KB/s，或用 `zdm config speed_limit 2097152`（2 MB/s）。

## 配置项（`~/.config/zdm/state.json`）

`download_dir`、`connections`(8)、`max_active`(3)、`speed_limit`(bytes/s)、`proxy`、
`max_retries`(5)、`timeout`(30)、`port`(9000)、`auto_resume`、`clipboard_monitor`、
`mirror_accel`、`stream_engine`(`auto`)、`archive_rules`(`{}` = 关闭)。

`stream_engine` 只管 HLS `.m3u8` / DASH `.mpd` 这类分片流：`auto` 先用 N_m3u8DL-RE
（分片并发、单片重试、暂停后按分片续传），找不到它的可执行文件或它失败时用 ffmpeg
重下一遍；`re` / `ffmpeg` 是点名只用一个。降级或换引擎的理由会挂在任务上（`stream_note`），
桌面端、网页版和 `/api/tasks` 都看得到，不会静默换个引擎说「按你选的做好了」。

`archive_rules` 是「后缀 → 子目录」的对照表，正式形状是 `{"mp4,mkv": "Video"}`，界面上按每行
`mp4,mkv = Video` 编辑（`#` 开头的行跳过；`=` 也接受 `:`、`->`、`→`）。**默认空表 = 关闭**：
没人希望下载器悄悄改动自己的目录结构。命中的成品落进 `<任务目录>/<子目录>`，没命中的照旧；
字幕、额外音轨这类 sidecar 跟着成品走，不单独归档，免得 `.srt` 在 Subtitles、`.mkv` 在 Video。
子目录只能是任务目录里的相对路径 —— `/Volumes/x`、`D:\Video`、`~/x` 一律当场拒绝（硬把它改成
`<下载目录>/D/Video` 是猜，报错才是答），同一个后缀出现在两条规则里也拒绝并点名是哪两条。
规则在**这一轮真正开跑时**才取，改设置只影响之后的下载 —— 已经下好的文件不会被自动重排，
要归位得走 `zdm archive`（桌面端设置页 / 网页版设置页的「整理已经下好的文件…」，
`POST /api/archive/sweep`）：先列清单、`--apply` 或点确认才真搬，在跑的任务持有的文件不碰、
子目录不再递归、撞名只跳过不改名，搬成功的文件会把任务记录里的新路径一起改掉。
没归档成功时不静默回退原目录，而是在任务上留一句 `archive_note`（三端和 `/api/tasks` 同样可见）。

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
