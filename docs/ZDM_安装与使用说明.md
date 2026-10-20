ZDM 本机安装与使用说明

> 来源仓库：<https://github.com/feibixunchang/ZDM>  
> 安装时间：2026-10-19 ｜ 系统：macOS（arm64）

## 已安装位置

- 程序代码：`~/Library/Application Support/ZDM`（含 `zdm/`、`web/`、`extension/`、`zdm.py`、`gui.py`）
- 运行环境：专属虚拟环境 `~/Library/Application Support/ZDM/venv`（基于系统 `/usr/bin/python3` 3.9.6，已装 PyQt5 5.15.11）
- 桌面 App：`~/Applications/ZDM.app`（双击即用，已去除隔离属性）
- 命令行：`~/.local/bin/zdm`
- 后台服务：launchd 代理 `~/Library/LaunchAgents/com.zdm.service.plist`（登录自启 + 崩溃自动重启）
- 配置/状态：`~/Library/Application Support/ZDM/zdm.json`（默认下载目录 `~/Downloads`，端口 9000）

## 三种使用入口

| 入口  | 命令                                         | 说明                           |
| --- | ------------------------------------------ | ---------------------------- |
| 桌面端 | `zdm gui` 或双击 `~/Applications/ZDM.app`     | 原生 Qt 窗口，关闭后收进托盘，下载继续后台跑     |
| 网页端 | `zdm open` 或浏览器打开 <http://127.0.0.1:9000/> | 适合远程/多设备管理；桌面端点「打开网页版」按钮即可跳转 |
| 命令行 | `zdm add/list/pause/resume/...`            | 终端与脚本使用                      |

三者共享同一后台服务与任务队列，可同时打开。

桌面端工具栏按钮：`＋ 下载`、`⏸`（全部暂停）、`▶`（全部继续）、`清除已完成`、**`打开网页版`**、`打开目录`、`设置`。  
右下角常显当前版本号，点一下即可检查更新。

## 深色 / 浅色模式（v1.2.0）

桌面端默认**跟随系统的深浅色外观自动切换**：macOS 切换「系统设置 → 外观」，  
Windows 切换「设置 → 个性化 → 颜色」、Linux 切换 GNOME/KDE 的深色主题后，  
ZDM 窗口会自动跟着变，无需重启。

若想固定为某一种，在「设置 → 外观（深浅色）」里选择「浅色」或「深色」；  
选「跟随系统」则恢复自动。三端（macOS / Linux / Windows）行为一致。

所有界面颜色（背景、文字、边框、表格、进度条、各状态色）都来自同一套调色板  
定义在 `zdm/theme.py`，因此深浅两套配色完全统一，不存在深色背景配浅色控件的情况。  
托盘（菜单栏）图标右键同样提供「打开网页版」。

## Linux：真正的原生安装包（v1.11.0 重做）

旧版 `zdm_1.10.0_all.deb` 只有 **87 KB**：里面是约 350 KB 的 `.py` 源码，加一个 4 行的 shell 启动器
（`exec "$PY" /usr/local/share/zdm/zdm.py`）。它 `Depends: python3` 却没声明 PyQt5，`postinst` 里用
`pip install --user PyQt5 || true` 兜底——以 root 跑时装进 `/root/.local`（普通用户读不到），
Ubuntu 23.04+/Debian 12+ 还会被 PEP 668 拦下，失败又被静默吞掉。结果：`zdm gui` 拿不到 PyQt5，
自动退回浏览器界面，用户以为装了个网页版。

v1.11.0 起改为和 macOS `.dmg` 同一思路：**自带运行时**。

| 项目 | 旧包（≤1.10.0） | 新包（1.11.0） |
|---|---|---|
| 体积 | 87 KB | 约 45–60 MB（xz 压缩后） |
| 运行时 | 依赖系统 `python3` + 用户自己 pip 装 PyQt5 | 内嵌 CPython + PyQt5/Qt，全部在 `/opt/zdm` |
| 安装位置 | `/usr/local/share/zdm`（源码） | `/opt/zdm/ZDM`（真正的 ELF 可执行文件） |
| 启动器 | `/usr/local/bin/zdm` 是 4 行 sh 脚本 | `/usr/bin/zdm`、`/usr/bin/zdm-desktop` 都是指向 ELF 的符号链接 |
| 依赖 | `python3` | Qt 运行所需的系统库（libGL/libxcb 等），不需要 Python、pip、`python3-pyqt5` |
| 架构 | `all` | `amd64`（按构建机架构；arm64 同理） |
| postinst | 以 root 执行 pip | 只刷新桌面数据库与图标缓存 |

安装与验证：

```bash
sudo apt install ./zdm_1.12.0_amd64.deb
zdm --version          # ZDM 1.12.0
zdm --selfcheck        # 报告内嵌 Python / PyQt5 / 资源路径
zdm-desktop            # 打开桌面窗口（菜单里也有「ZDM 下载管理器」图标）
```

免 root 版本 `zdm-linux-amd64.tar.gz` 是同一份 bundle：解压后 `./install.sh` 会装到
`~/.local/share/zdm`，并在 `~/.local/bin` 建 `zdm` / `zdm-desktop` 两个链接。

打包实现见 `zdm/build_linux_app.sh` 与 `zdm/zdm.spec`（PyInstaller onedir）。脚本自带自检：
必须是 ELF、`--selfcheck` 必须通过、所有 `.so` 无未解析依赖、并在 xvfb 里真实启动一次 GUI 才出包。

托盘/右键菜单在 Linux 上没有 `NSVisualEffectView` 这类系统级模糊 API，改为自绘**圆角半透明磨砂面板**
（`GlassMenu`，12px 圆角、配色与 macOS 同一套调色板）；没有合成管理器（如裸 XFCE/i3）时自动退成
不透明，避免出现黑底。

## v1.11.0 修复与新增

### 1. 点菜单栏图标时菜单文字发灰、点不了

**根因**：macOS 上点状态栏（菜单栏）图标**不会**让 App 变成活动应用。非活动 App 弹出的菜单会  
被系统按「未激活」渲染成灰色，且第一次点击只被用于激活、不会落在菜单项上——所以要「先点一下主  
页面」才能用。

**处理**：托盘改为「先激活 App、再自己弹出菜单」（`activate_app()` + `QMenu.popup`），并且这次  
激活不会顺带把主窗口拽出来（用 `_suppress_restore_until` 抑制唤回钩子）。窗口显示走统一的  
`_show_front()`：先激活 → 再 show/raise/activateWindow，避免出现可见但半激活的灰窗口。

顺带修掉一个判断错误：Qt 的 `applicationState()` 在 macOS 上会**停留在 ApplicationActive**（实测  
另一 App 在前台时它仍报 active），所以「窗口是否需要恢复」「点托盘是显示还是隐藏」改用  
`NSApp.isActive()`（`app_is_active()`）判断，恢复条件再加一条 `isActiveWindow()`。

菜单本身也换成了 macOS 原生的**毛玻璃**（vibrancy）：背景真的会模糊菜单后面的内容，圆角 12px，  
浅色用 VibrantLight、深色用 VibrantDark，文字/分隔线跟着主题走。做法是在菜单自己的 NSWindow 里  
把 `NSVisualEffectView` 作为**同级视图**放到 Qt 视图**下方**（当子视图会盖住文字），并让窗口  
非不透明；QSS 只画文字、发丝边框和圆角，不再画底色。PyQt 没有这个能力，属于 macOS 专属增强：  
其它平台或 PyObjC 缺失时自动退回原来的不透明菜单。

### 2. 每个任务行都有暂停 / 继续按钮

暂停 / 继续是**针对单个文件**的：每行操作区最前面就是 `⏸`（下载中/排队中）或 `▶`（已暂停/出错/  
已取消）图标按钮，鼠标悬停提示「暂停这个任务 / 继续这个任务」。图标用当前调色板现画，深浅色都清晰。

**每一行都保留这个按钮位**：已完成的行同样显示 `⏸`，只是置灰不可点（提示「已完成，无需暂停」）。  
这样按钮不会因为任务状态变化而忽隐忽现——看不到按钮容易让人以为功能不存在。

「全部暂停 / 全部继续」也在主界面上：筛选标签（全部 / 进行中 / 已暂停 / 已完成）右侧并排两个  
中文按钮 `⏸ 全部暂停`、`▶ 全部继续`，作用于所有任务；托盘菜单和任务右键菜单里同样有这两项。  
没有可暂停（或可继续）的任务时，对应按钮自动置灰。

### 3. 没开桌面端时，下载会自动拉起

新增 `zdm/desktop.py`：任务创建后若桌面端未运行则自动启动（macOS 用 `open -a ZDM.app`，  
Windows 找 `ZDM.exe`，Linux 找 `zdm-gui`，都找不到再退回 `python gui.py`）。  
已运行的情况下，`open -a` 只是把窗口激活；Windows/Linux 由单实例守卫把第二次启动变成「唤醒」。  
带 5 秒节流，连续多个下载只拉一次；「设置 → 下载时自动打开桌面端」可关闭。

### 4. 「打开」打开的是文件本身

- 任务行：`打开` = 用默认程序打开文件（`.dmg` 直接进安装界面），`目录` = 在文件管理器中定位
- 双击任务行 = 打开文件；右键菜单另有「打开文件 / 在文件夹中显示」
- 网页版：📂 = 打开文件，🗂 = 在文件夹中显示
- 未完成的任务没有文件，仍然打开下载目录

### 5. 显示版本号

窗口标题、状态栏右下角、托盘 tooltip、托盘「关于 ZDM（vX）」、设置里的「当前版本」都显示版本，  
版本号唯一来源是 `zdm/zdm/__init__.py` 的 `__version__`（服务端 `/api/` 也返回它）。

### 6. 更新提示与一键更新

新增 `zdm/update.py`（纯标准库）：

- 启动 2 秒后静默查 `api.github.com/repos/feibixunchang/ZDM/releases/latest`
- 有新版 → 主界面顶部横幅「发现新版本 vX（当前 vY）」+ 托盘通知
- 横幅按钮：`下载并安装`（按平台挑 `.dmg/.exe/.deb` 下载到 `~/Downloads` 后自动打开安装程序）、  
  `更新说明`、`忽略此版本`（记住该版本，不再提示）、`✕`
- 设置里可关「启动时自动检查更新」，也可「立即检查更新」；托盘也有「检查更新」
- 网络不可达时静默失败，不打扰使用（手动检查才提示）

### 7. 安装包不再依赖本机的 Xcode / Python

排查上面几项时发现两个只会在**别人机器**上才炸的打包问题，一并修掉了：

- **启动器把构建目录的绝对路径编进了二进制**，而且优先级高于运行时推导。装到 `/Applications`  
  后它会去构建目录找自己的运行库，找不到就直接退出（表现为「双击没反应」）。现在改为**先按自身  
  可执行文件位置推导** `<App>/Contents/Resources`，环境变量与编译期路径只作兜底，并且每级都会先  
  校验目录里确实有 `zdm.py` 才采用。
- **内置 venv 的解释器指向 `/Applications/Xcode.app/...`**（建 venv 时用的系统 Python），  
  `pyvenv.cfg` 里也是同一个绝对路径。没有装 Xcode 的 Mac 上，GUI 能开、但后台服务进程起不来，  
  也就是「界面正常却完全下不动」。现在 `venv/bin/python` 换成指向**内嵌 framework** 的**相对**软链、  
  删掉 `pyvenv.cfg`，再用一个 `.pth` 文件把 framework 的 site-packages 指回 venv 的包目录。  
  包里不再有任何绝对路径，解释器按自身位置解析 prefix，整个 `.app` 复制/移动到任何地方都能独立运行。

构建脚本里还加了两道自检，防止这两类问题复发：把成品 `.app` **改名搬到另一个目录**再跑，  
断言①启动器报出的资源目录就是新位置，②新位置的解释器能导入 PyQt5 且 `sys.prefix` 落在包内。

## 已验证可用

- 服务启动正常，Web UI 返回 200，`/api/state` 正常。
- 真实下载测试成功：从 `ftp.gnu.org` 多线程（8 连接）下载 `wget-1.21.4.tar.gz`，落盘 5,059,591 字节、校验为合法 gzip、100% 完成。
- 引擎容错正常：对不支持/被代理拦截的 URL 会正确回退 curl 后端并上报错误（不会静默丢文件）。
- 桌面端（PyQt5）冒烟测试通过：窗口正常创建、连接服务、不崩溃。

## 已修复的三个问题（2026-10-19）

### 1. 下载一直失败（界面正常但下不动）

**根因**：后台服务被从自动化沙箱启动，继承了沙箱的代理变量；且该服务长期占用 9000 端口，  
导致你从自己终端/双击 `.app` 启动时，启动脚本探测到"服务已在运行"就**跳过启动服务**，  
只拉起 GUI —— 结果你的界面连的仍是我在沙箱里启动的那个连不上 GitHub 的引擎。  
**处理**：已停掉沙箱服务并清理 pid。现在请**从你自己的终端**启动一次，让引擎归属你的网络：

```bash
zdm stop && zdm start
```

之后所有下载都走你本机真实网络。

### 2. Dock / 启动器显示通用占位图标（⊘）

**根因**：`install.sh` 生成 icns 的 iconset 不规范 —— 只嵌了 @1x 尺寸、命名也不标准（缺全部 `@2x`），  
产出**残缺 icns**，macOS 解析失败就回退到通用图标。  
**处理**：已用标准 iconset 规范重建完整 icns（含 16/32/128/256/512 全部 @1x + @2x，1024px，`ic12`），  
同步进 `.app`，并刷新了 Dock/Finder 图标缓存。若仍显示旧图标，重启 Dock 或注销重登一次即可。

### 3. 关闭主窗口后，点 Dock 图标唤不回主界面

**最终根因（重要）**：不是唤醒逻辑的问题，而是 **ZDM 在 Dock 里没有自己的身份**。  
实测发现运行中的进程可执行文件是 `Python.app`（`com.apple.python3`），不是 ZDM.app——因为原 `.app` 的  
`Contents/MacOS/ZDM` 是 shell 脚本，它 exec venv python 后，进程镜像变成 Python.app，macOS 的  
Dock / LaunchServices / reopen 回调全部按 Python.app 处理，所以点 Dock 相当于点 Python 图标，  
ZDM 的 reopen 钩子永远收不到（而菜单栏 tray 属于 ZDM 自己的 Qt 代码，所以一直正常）。

**彻底修复 —— 改造成真正的原生 App（已落地）**：

- 新增 `zdm/zdm_launcher.m`：一个真正的 Mach-O 主程序，用 **Python C API 把 CPython 解释器内嵌进  
  ZDM.app 自己的进程**（`Py_InitializeFromConfig` + `import gui`）。整个 App 只有一个进程，且这个进程  
  就是 ZDM.app。
- `install.sh` 现在会用 clang 编译这个 launcher，并把 `Python3.framework` 嵌入  
  `ZDM.app/Contents/Frameworks/`，rpath 指向 `@executable_path/../Frameworks`。
- 因为 PyQt5 的 `QApplication` 本身就是原生 NSApplication，它在 ZDM.app 进程内运行时，  
  Dock 图标归属 ZDM、`applicationShouldHandleReopen:`（Dock 点击）也由 Qt 原生处理，窗口即可唤回。
- 若系统没有 clang，脚本会自动回退到旧的脚本启动器（功能可用，仅 Dock 行为受限）。

> 现在 `ZDM.app` 是**真正的原生 App**（Mach-O arm64 可执行文件 + 内嵌 Python 运行时 + adhoc 签名），  
> 不是脚本壳。Dock 图标应显示 ZDM 自己的图标，点它即可唤回关掉的主窗口。

> 注意：修改过 `gui.py` 后，**必须完全退出旧的 ZDM 进程再重开**（托盘右键「退出」或 `pkill -f
> "Contents/MacOS/ZDM"`），否则仍在跑旧代码。

> 说明：以上修复已同步到**已安装目录**（`~/Library/Application Support/ZDM`）和你现有的  
> `~/Applications/ZDM.app`，同时也改回了仓库源码（`zdm/install.sh`、`zdm/gui.py`、`zdm/zdm_launcher.m`），  
> 以后重新安装/打包不会再复现这三个问题。

## 关于“登录自启”（重要）

本机 macOS 版本已移除 `launchctl load`，原 `install.sh` 用的就是这条废弃命令，导致代理无法被加载。  
已修改 `install.sh` 的 macOS 分支，改用现代 API：

```bash
launchctl bootout "gui/$(id -u)/com.zdm.service" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.zdm.service.plist"
launchctl kickstart -k "gui/$(id -u)/com.zdm.service"
```

由于当前自动安装的执行环境无法与 per-user launchd 通信，上述命令需**你在自己的终端（Terminal.app）里跑一次**才能让“登录自启/崩溃重启”立即生效；  
通常下次正常登录时，系统也会自动加载 `~/Library/LaunchAgents` 下的代理。  
当前服务是由安装脚本手动以守护方式拉起的，现在就能直接使用。

## ⚠️ 重要：如果你在自动化/沙箱环境里启动过 ZDM（下载失败时看这里）

在 WorkBuddy 这类带网络沙箱的环境里启动 ZDM 服务，会出现**所有走 curl 的下载失败**，报错形如：  
`curl: (7) Failed to connect to 127.0.0.1 port xxxxx: Couldn't connect to server`。

原因：服务进程继承了沙箱注入的代理环境变量（`http_proxy/https_proxy`），而系统 `curl` 会自动读取它们，  
于是所有请求都被塞进那个（可能已失效的）本地代理端口里，导致失败。ZDM 本身没配代理（配置里 `proxy` 为空）。

**解决办法（任选，推荐第 1 种）**：

1. **在你自己的终端里启动 ZDM**（Terminal.app 执行 `zdm start`，或直接双击 `~/Applications/ZDM.app`）。  
   这样进程不带沙箱代理变量，走正常直连/你自己的网络。
2. 若必须在沙箱里跑，用清空代理变量的方式启动：
   ```bash
   env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY zdm start --daemon
   ```
   注意：这只解决“坏代理”问题，**沙箱本身可能仍屏蔽部分站点**（例如 GitHub 在本环境直连与经代理都返回 000，属于环境限制，ZDM 无过错）。

补充说明：GitHub 的 `.dmg` 等 Release 下载走 GitHub 的对象存储/CDN，部分网络（含本沙箱）本就难以连通；  
在你自己能正常访问 GitHub 的网络里，ZDM 可正常下载。若你日常需要代理，可在 ZDM「设置」或  
`zdm config proxy http://127.0.0.1:<你的代理端口>` 里显式指定。

## 浏览器扩展接管下载（Chrome / Edge）

1. 打开 `chrome://extensions` → 右上角开启「开发者模式」
2. 「加载已解压的扩展程序」→ 选择 `~/Library/Application Support/ZDM/extension`
3. 点扩展图标 → ⚙ → 「测试连接」→ 勾选「自动接管浏览器下载」

## 视频 / 音频嗅探（一键推送下载，v1.12.0 新增）

装好上面那个扩展之后，**浏览器的每个网页右下角会出现一个 ZDM 圆形浮动按钮**。

- 按钮上带数字角标 = 当前页面嗅探到的媒体数量（视频 / 音频）。
- 点一下展开面板，里面列出嗅探到的每一个媒体：图标区分视频 / 音频，下面显示文件名与大小。
- 每条右侧有「推送」按钮，点一下就直接把这个媒体交给 ZDM 下载，**走的是和「接管浏览器下载」完全一样的通道**（冷启动唤醒、原生消息、协议兜底三条路径都支持）。
- 推送成功的条目会从面板里移除，角标数字同步减少。

任务列表里会带上来源标记：`[嗅探]`、`[浏览器]`、`[网页版]`、`[协议]`，
点顶部的「嗅探」筛选按钮可以只看嗅探来的任务。

**嗅探是怎么工作的（两条来源合并）**

| 来源 | 覆盖到的场景 |
|---|---|
| `webRequest` 响应头 | 播放、拖动进度条、切换清晰度时新发出的媒体请求（能看到 Content-Type 与大小） |
| 页面 DOM 扫描 | 首屏就摆在那儿的直链媒体（`<video>` / `<audio>` / `<source>`） |

两条来源会合并去重后一起显示，不会互相覆盖。

**已知的边界（本期不支持）**

- **HLS（`.m3u8`）与 DASH（`.mpd`）流媒体**：嗅探到的是「分片清单」而不是可直接下载的媒体文件，
  面板不会列出它们。要整段下载需要引入流媒体合并引擎，计划在后续版本处理。
- 需要登录才能看的站点：扩展只在**与页面同站**时才会带上 Cookie；跨站 CDN 不带（避免越权）。
- 加密流（DRM，如 Widevine / FairPlay）无法下载。

## 常用命令

```bash
zdm start --daemon          # 后台启动服务
zdm status                  # 查看服务与任务
zdm add "URL" -c 16 -o ~/Downloads   # 添加下载，16 连接
zdm list                    # 列出任务
zdm config speed_limit 2097152        # 限速 2 MB/s
zdm stop                   # 停止服务
```

## 卸载

```bash
cd <仓库>/zdm && ./install.sh --uninstall
```

（会移除程序、App、LaunchAgent 与命令行链接；已下载文件和 `~/Downloads` 配置保留。）

## v1.11.1 修复清单（2026-10-20）

本次为 1.11.0 的补丁版本，集中修复实机测试发现的问题：

1. **桌面端无法添加下载**：`Api.add()` 只接受关键字参数，界面却传字典，回车 / ＋下载 / 拖拽 / 命令行传 URL 全部报错。
2. **浏览器接管后重复下载**：服务端与 CLI 把已入队的 URL 再次交给 GUI，GUI 又添加了一次。
3. **打包版 curl 回退失败**：PyInstaller 的 `LD_LIBRARY_PATH` 泄漏给系统 curl，触发 OpenSSL 版本冲突；现已在子进程中剥离。
4. **404/500 被误当 403 重试 curl**：现在只对 403/406/429 或连接失败回退，错误信息不再被掩盖。
5. **中文文件名乱码**：修复 RFC 5987 `filename*=UTF-8''...` 被按 Latin-1 解码的问题。
6. **删除任务不删文件**：删除已完成任务时现在会真正移除文件；GUI 增加确认框并可选择保留文件。
7. **假 Range 服务器损坏文件**：只按 206/Content-Range 判定分段，服务器忽略 Range 时自动降级单连接。
8. **过滤/重名时操作错行**：右键与双击改为按行保存的任务 ID 查找。
9. **Qt 标准按钮英文**：加载 Qt 中文翻译，`保存 / 取消 / 是 / 否` 正常显示。
10. **浏览器接管弹出旧版界面**：不再回退到 PATH 中的旧 `zdm-desktop`，优先使用当前安装的 GUI。
11. **扩展**：popup 对远端文件名做 HTML 转义；`install.sh` 注册 Linux Chrome/Chromium 原生消息宿主，未启动时可自动唤醒。

## v1.12.0 修复清单与新增（2026-10-20）

### 新增：媒体嗅探

1. **扩展新增浮动按钮 + 媒体列表面板**（`extension/sniffer.js`，Shadow DOM 隔离，站点 CSS 打不坏它）。
2. **两条嗅探来源合并**：`webRequest` 响应头（能看到 Content-Type / 大小）+ 页面 DOM 扫描（首屏直链媒体），
   合并去重，不会互相覆盖。
3. **一键推送复用接管下载的通道**：`handOff()` 三条路径（冷启动唤醒 / 原生消息 / `zdm://` 协议兜底）全部支持。
4. **任务新增来源与类型字段**：`source`（manual / browser / sniffer / webui / protocol）与 `kind`（video / audio），
   三端（macOS / Linux / Windows）+ 网页版 UI 全部贯通，并带「嗅探」筛选。
5. **Cookie 只发同站**：`sameSite()` 判定失败时不带 Cookie，避免越权。

### 修复：macOS / Linux（Python 端）

6. **打包版 GUI 起不来**：frozen 分支传 `--gui` 而不依赖 `argv[0]`（原名 `ZDM` 会进 CLI 分支，
   argparse 把裸端口当非法子命令 exit 2）。
7. **`zdm start` 谎报「已在运行」**：端口被非 ZDM 程序占用时不再误判，改用 `/api/state` 应答结构校验。
8. **误杀别的进程**：kill 前用 `ps -o args=` 验证进程身份，验证不了就拒绝。
9. **开机自启静默失败**：plist 日志改写 `~/Library/Logs`（原来写进 root 所有的 bundle，job 直接加载失败）；
   launchd 改用 `bootstrap` + `kickstart`，失败才回退 `load -w`。
10. **更新下载无校验**：按资产 size 校验，不符即报错；资产选择修正
    （Windows 优先原生 `zdm-setup*.exe` 避开 PyQt5 薄壳，Linux 按架构匹配 deb，macOS 优先无架构标 dmg）。
11. **表格控件泄漏**：`setCellWidget` 替换时不会删旧控件，旧控件被 reparent 到 viewport 永久驻留。
    已在 `setRowCount` 之前统一摘除并释放。
12. **Dock 唤不回窗口**：Qt 实际安装了 `QCocoaApplicationDelegate`（旧结论已过时），
    自建 delegate 改为 `forwardingTargetForSelector_` 转发未实现的 selector。
13. **深浅色不跟随系统**：改用 `NSDistributedNotificationCenter` 的 `AppleInterfaceThemeChangedNotification`
    （`NSWorkspaceDidChangeNotification` 根本不存在），并去掉重复装的观察者。
14. **设置对话框预览不切主题**：取消与保存两条路径都补 `prime()`。
15. **退出时下载线程还在跑**：退出前等待 worker，超时强制终止。
16. **扩展冷启动丢选项**：唤醒路径补传连接数与保存目录。
17. **扩展误判任务已存在**：`taskExists` 加 `created_at` 下限，历史同 URL 任务不再导致新任务从未创建。
18. **原生消息 5 秒超时过短**：放宽到 15 秒（宿主冷启动可合法耗时约 20 秒）。
19. **网页版 XSS 与状态卡死**：`esc()` 补单引号转义；SSE 断开后状态不再永久显示「连接中断」。
19b. **来源徽标会显示成乱码**：`QTableWidgetItem` 不解析 HTML，`<font>` 标签会被原样显示出来。
    改为在文件名单元格放 `QLabel`（原生支持富文本），并对文件名做 HTML 转义（远端文件名可能含尖括号）。
19c. **任务字段缺失会让整个列表空白**：`t['speed']` / `t['status']` / `t['created_at']` 等
    原本是直接索引，一个字段缺失就抛 KeyError 并中断整次渲染（不是只影响那一行）。
    现在全部改为 `.get()` + 数值兜底，残缺或类型异常的任务都能正常显示。

### 修复：Windows 原生端（C#）

20. **筛选标签全是坏的**：四个筛选标签没绑任何点击事件（`ToggleButton` 还是 `IsHitTestVisible="False"`），
    界面看着能点却毫无反应。现已补 `OnFilterTabClick` 并新增「嗅探」筛选。
21. **「重新下载」无效**：`RestartAsync` 取消后没有换新 `CancellationTokenSource`，probe 立刻被取消，任务变暂停。
22. **删除任务不删文件**：补 `DeleteTaskFiles`，下载中分支等 worker 空闲后再删。
23. **Range 被服务器忽略仍静默损坏文件**：服务器返回 200 而非 206 时直接报错。
24. **慢网 / 大文件 30 秒必失败**：总超时取消，改为每次读、每次写各自独立计时。
25. **Web UI 实时更新全失效**：`/api/events` 路由缺失（Web UI 连的是这个路径），已补别名。
26. **状态保存竞态**：`SaveState()` 整体加锁。
27. **扩展注册竞态**：注册加锁，避免并发写出半截清单。
28. **版本号写死**：`ApiServer.Version` 改为读程序集版本。

### 修复：打包与 CI

29. **Linux deb 缺原生消息清单**：deb 里补 `zdm-native-host` 包装脚本与
    `/etc/opt/chrome|chromium/native-messaging-hosts/` 清单（扩展 ID 已固定为
    `mpodbnemeeghjpllgphpemhoogddfhjg`），并声明 `zdm://` 协议。
30. **CI 不编译 Windows 代码**：工作流触发分支补上 `linux-native`，C# 改动不再无人验证。
31. **CI 缺扩展校验**：新增作业校验 JS 语法、manifest 合法性与扩展 ID 未被改动；
    deb 校验增加「必须含原生消息清单」与「体积不得小于 10 MB」（防止又打成源码壳）。
