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

设置不再是一屏到底的长表单（那样在笔记本上会把「保存」顶出屏幕）：桌面端按用途分成  
**下载 / 界面与启动 / 网络与加速 / 流媒体与归档 / 外部接入与更新** 五页，每页自己可滚，  
「保存 / 取消」常驻在窗口底部；网页版是主页面里的一个二级页面，左列分组跳转、顶部常驻  
「保存」，地址 `http://127.0.0.1:9000/#settings` 可以直接分享或收藏夹里留着。  
改的只是摆放位置，字段一项没少，命令行 `zdm config 键 值` 能改的东西照旧。

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
sudo apt install ./zdm_1.13.0_amd64.deb
zdm --version          # ZDM 1.13.0
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

## Firefox 扩展（v1.18.0 新增）

Firefox 不认「加载已解压的扩展程序」，只吃打包好的 `.xpi`。**源码仍是上面那一份
`extension/`**，只是清单按 Firefox 的写法翻译了一遍（去掉 Chrome 独有的 `key`、
`minimum_chrome_version`、`downloads.open`，把 `background.service_worker` 换成
`background.scripts`，补上固定的 gecko ID），所以两边的功能与 bug 修复是同步的。

1. 拿包，任选一个入口：
   - 桌面端：工具栏/托盘菜单的「浏览器扩展」→「导出 Firefox 扩展」，选个保存位置；
   - 网页版：页面底部「下载 Firefox 版扩展（.xpi）」；
   - 命令行：`zdm firefox-ext --out zdm-connector.xpi`。
2. Firefox 地址栏打开 `about:debugging#/runtime/this-firefox` → 「加载临时附加组件…」→
   选中那个 `.xpi`。
3. 之后与 Chrome 一样：点扩展图标 → ⚙ → 「测试连接」→ 勾选「自动接管浏览器下载」。

要先知道的边界（都是 Firefox 的规则）：

| 事项 | 说明 |
|---|---|
| 临时加载会被清掉 | **浏览器重启后扩展自动移除**，每次开机要重新加载。要永久装得走官方签名，或用 Dev Edition / Nightly / ESR 并把 `xpinstall.signatures.required` 设为 `false` |
| 自动唤醒 | 临时加载会分配一次性扩展 ID，与原生消息清单里那个固定 ID 对不上，所以「ZDM 没开时自动拉起」这一步在临时加载下不生效 —— 请先开着 ZDM。永久安装（签名包 / Dev Edition）不受影响：`install.sh`、`.deb`、Windows 安装包都会额外注册 Firefox 那棵原生消息目录 |
| 其余功能 | 接管下载、网页视频/音频嗅探、播放器上的「下载此视频」、推送任务到本机服务都照常工作，它们走 HTTP，与扩展 ID 无关 |
| 版本要求 | 最低 Firefox 115；`.xpi` 与 Chrome 扩展共用 `extension/manifest.json` 的版本号（当前 1.5.x），三处入口打出的是同一个版本 |

## 视频 / 音频嗅探（一键推送下载，v1.13.0 新增）

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
  面板不会列出它们（清单本身不是一个能播的文件）。要整段下载请把清单地址交给「＋下载」或
  `zdm add`：粘贴 `.m3u8` / `.mpd` 就自动按流媒体任务处理，由内置的 **N_m3u8DL-RE** 分片并发下载并合流，
  暂停后按分片续传。
  引擎可在设置里选（`自动` / `只用 N_m3u8DL-RE` / `只用 ffmpeg`），命令行
  `zdm config stream_engine ffmpeg`；换了引擎的任务会写明理由，不会静默降级。
  多音轨 / 多字幕可以点名要哪几路：`zdm add "URL" --select-audio 'lang="eng"'
  --select-subtitle 'lang="eng"'`，桌面端的新建/编辑对话框与网页版地址条下方也有同样三项。
  写法就是引擎的「子键=值（多个用冒号分隔）」：`id` / `lang` / `name` / `codecs` / `res` /
  `frame` / `for`…，或整体写 `all` / `best` / `worst1`；挑中的画面、声音、字幕最后混进**同一个**
  mp4 文件里，不留 `.m4a` / `.srt` 散件。写错的子键会在建任务时就拒（原因直接显示在界面和 CLI 上）。
- 需要登录才能看的站点：扩展只在**与页面同站**时才会带上 Cookie；跨站 CDN 不带（避免越权）。
- 加密流（DRM，如 Widevine / FairPlay）无法下载。

## 直播流录制（v1.17.3 新增）

把**还在播的** `.m3u8` 直链交给「＋下载」或 `zdm add`，ZDM 会自动认出这是直播（判据是播放列表里
没有 `#EXT-X-ENDLIST`，不是看后缀），按直播参数录制，不需要额外声明什么。

- **录制时长上限**（可选）：桌面端新建 / 编辑对话框、网页版地址条下方的轨道条与编辑对话框里
  的「直播录制上限」，命令行 `zdm add "URL" --live-limit 30:00`，接口字段 `stream_live_limit`。
  写 `900`、`15:00`、`1:00:00` 都认，最长 24 小时；**留空 = 一直录到你手动停止，或直播自己结束**。
  注意站点行为：很多直播站只保留最近几十秒的分片，上限设得比站点窗口短并不会「只录这么长」；
  站点窗口比上限更短时反而会录满整个窗口。
- **进度看的是「已录多久」**：直播没有总量，所以进度列显示 `已录 12:30`，设了上限时再跟一句
  `· 剩 5分0秒`；还没追上直播边缘时显示 `等直播开始…`。
- **停止 = 收工，不是暂停**：点行上的暂停按钮（或网页版的 ⏸）就是「停止录制」，已经录下的那段
  会立刻封成成品。「全部暂停」和退出应用时同样按停止录制处理 —— 直播的边缘已经过去，没有
  「暂停后续传」这回事。想丢弃这次录制请用「取消」。
- **成品在哪**：就是你指定的保存目录、指定的文件名（`.mp4`）。录制过程中的中间文件（`.ts`）
  在转封装成功后自动清掉；万一这台机器上 ffmpeg 没能转成 mp4，会保留 `.ts` 并在任务上写明原因，
  不会报一个假的「已完成」。
- **直播必须用 N_m3u8DL-RE 引擎**（随包内置，`stream_engine` 保持 `自动` 就行）。用 `只用 ffmpeg`
  时任务会直接报错说明原因，而不是留下一个永远不结束的任务。
- 正在录制时不能改上限（改了也不会对这一段生效）；要先「停止录制」，再重新下载。

## 用 AriaNg / Motrix 这类 aria2 客户端接管（v1.18.0 新增）

ZDM 本机服务在**同一个端口**上还认一套 aria2 的 JSON-RPC，路径 `POST /jsonrpc`（别名 `/rpc`）。
默认就是开着的，不需要额外启动什么进程。

1. 打开设置（桌面端「设置」→「外部接入与更新」这一页；网页版齿轮 →「外部客户端接入」那一栏），把 **RPC 地址** 和 **RPC 令牌** 复制出来。
2. 在 AriaNg 的设置里填：RPC 路径 `http://127.0.0.1:9000/jsonrpc`，RPC Secret 填那枚令牌
   （aria2 的写法 `token:<令牌>` 作为第一个参数也照样认）。
3. 之后添加 / 暂停 / 继续 / 删除 / 排队 / 改并发与限速都能在外部客户端里做。任务进的还是
   ZDM 的同一个队列，桌面端、网页版、扩展里看到的完全一样。

支持的方法：`addUri`、`tellActive` / `tellWaiting` / `tellStopped` / `tellStatus`、
`getDownloadResult`、`getTaskList`、
`pause` / `unpause` / `pauseAll` / `unpauseAll`（含 `force*` 别名）、`remove` / `forceRemove`、
`removeDownloadResult`、`changePosition`、`changeGlobalOption` / `changeOption`、`getGlobalStat`、
`getVersion`、`newSessionId` / `getSessionId`、`checkToken`、`system.listMethods`、
`system.multicall`。

要提前知道的边界：

- **BT / 磁力 / Metalink 不支持**（ZDM 只做单文件下载：http/https/ftp/ftps/sftp），这些方法会明确报错，不会「收下任务却什么都不下」。
- 不认识的 aria2 选项（`ftp-pasv`、`bt-max-peers` 等）**直接报错**，而不是忽略掉继续下。
- `changeOption` 只能改连接数；改 `dir` / `out` 会被拒，因为那动的是已经落盘的路径。
- **删除不会碰磁盘**：`remove` / `forceRemove` 只清任务记录，已下好的文件留在原地（这是 aria2 的语义）。
- 长度、速度这类字段一律是**字符串**（aria2 的口径）；`tellActive` 里也会包含已暂停的任务。
- 排队任务超过约 199 条时，`changePosition` 会说明「无法保证精确位置」而不是给一个错的顺序。
- 不想开这条通道就在设置里取消勾选（`rpc_enabled`），或 `zdm config rpc_enabled false`；
  关掉只影响 `/jsonrpc`，ZDM 自己的界面与 `/api/*` 一切照旧。
- 令牌等于「能操作这台机器上的 ZDM」，别贴到网上、别写进公开配置。服务只监听 127.0.0.1。

## FTP / FTPS / SFTP 下载（v1.18.0 新增）

链接直接粘进「新建下载」就行，也可以拖进桌面端窗口、从扩展推送或 `zdm add`。账号密码写在链接里：

```
ftp://user:pass@files.example.com/pub/big.iso
ftps://user:pass@files.example.com/pub/big.iso      # 显式 AUTH TLS，默认 21 端口
ftps://user:pass@files.example.com:990/pub/big.iso  # 隐式 TLS：只有端口写 990 时才走
sftp://user:pass@backup.example.com/data/big.iso
sftp://user@backup.example.com/data/big.iso         # 不写密码 = 用 SSH 的密钥 / agent
```

`ftp://` 匿名服务器不写账号即可（会按 `anonymous` 登录）。密码里有 `@` `:` `/` 这些字符时按
URL 规则百分号编码，例如 `p%40ss`。

下到的东西和 http 完全一个待遇：**同一套分块并发、同一套断点续传**（暂停、重启服务、崩溃后接着
传），列表里的速度 / 剩余时间 / 失败原因也都是同一套，不存在「FTP 任务是另一条队列」这种事。

| 事项 | 表现 |
|---|---|
| 依赖 | ftp / ftps 只用标准库，装完即可用；sftp 需要 `paramiko`，三端安装包已内置，源码安装由 `install.sh` 顺带装上 |
| 服务器不给长度 | 列表显示「大小未知」，退回单连接顺序下载，不显示假百分比 |
| 服务器不肯续传 | FTP 的 `REST` 没被答应（回复不是 3xx）时直接报错重试，绝不「从 0 收到的字节写到中间位置」 |
| TLS | `ftps://` 校验证书链：自签、链不完整一律连不上，不会退化成明文继续下 |
| 主机密钥 | SFTP 首次连接记进配置目录里的 `sftp-known_hosts`（**不会改你的 `~/.ssh/known_hosts`**）；之后再连同一主机，密钥对不上就拒绝，并说明怎么确认、删哪一行能解除 |
| 指向目录 | 明确拒绝「这是个目录，不是文件」；ZDM 的下载任务对应单个文件 |
| 代理 | 设置里的代理只对 http/https 生效，ftp/ftps/sftp 走各自的协议通道，不认那个字段 |
| 没有 paramiko | 添加 `sftp://` 时当场拒绝并说明「这台机器缺 paramiko，怎么补」—— 不会先建一条永远下不完的任务 |

## 英文界面（v1.18.0 新增）

网页版和浏览器扩展都支持中英文，**默认跟随浏览器的界面语言**，也可以自己指定：

- **网页版**（`http://127.0.0.1:9000/`）：右上角「界面语言 / Language」下拉，选一次就记住
  （存在浏览器本地）。想直接给别人一个英文界面的链接，用 `http://127.0.0.1:9000/?lang=en`。
- **扩展**：扩展图标 → ⚙ 设置 → 「界面语言」（自动 / 中文 / English）。这一项存在扩展的同步
  存储里，换机器登录同一浏览器账号还是英文；改完立刻生效，网页上的嗅探面板、播放器按钮和
  清晰度菜单跟着变，不用刷新页面。
- **Firefox 与 Chrome 同一份语言文件**：导出的 `.xpi` 里带的就是同一套文案，不存在「Chrome 已经
  英文、Firefox 还是中文」。

覆盖范围：扩展的弹出面板、设置页、唤醒中间页、网页里的各类界面，以及系统通知和右键菜单。

**暂时还是中文的部分**：桌面端程序窗口（Qt 那一套）和日志文件（`download-errors.log`、任务日志）。
后端报错里认得出的那些（参数校验、FTP/SFTP、状态冲突）会跟着界面语言翻译，认不出的原样保留中文，
不做「猜一个英文出来」的事。

## 播放列表批量下载（v1.18.4 新增）

一份 YouTube 播放列表 / 频道投稿页，或 B站的投稿、合集、收藏夹、稍后再看，会**展开成一条条独立任务**：
每集有自己的文件名、自己的进度、自己的重试，某一层失败不会把整份清单带崩。

- **桌面端**：工具栏「播放列表…」（输入框空时自动取剪贴板）。先「展开」列名单（标题 + 时长），
  在「选哪几条」里写 `all` / `1-20` / `1,3,5-8`，勾选是否收进以清单标题命名的子目录，
  确认前还能改「最多展开」并「重新展开」。批量导入里混进清单地址时，只点名有几个是清单、
  不擅自展开（展开一份频道投稿页要翻几十页）；整批都是清单才直接进展开对话框。
- **网页版**（`http://127.0.0.1:9000/`）：顶部「播放列表…」同一套对话框；把清单地址直接填进
  「＋下载」会被拒绝并告诉你改用哪个入口。
- **这一份清单落到哪儿**（v1.18.10）：对话框里的「存到」只改变本次加入的条目，留空＝用「设置」里的
  下载目录（占位提示就把当前设置写给你看），旁边「浏览…」直接挑目录；勾上「先暂停，不自动开始下载」
  则几十条只建记录不开跑，回头挑要下的逐个继续。命令行同义：`-o/--dir` 指定目录、`--paused` 先不入队。
- **命令行**：`zdm playlist <链接>` 只看名单不建任务，`zdm add <链接> --playlist 1-20,25` 建任务，
  `--no-subdir` 并进 `-o` 指定的目录，`--max-items 300` 改展开上限。

默认一次展开 100 条、最多 500 条；名单被截断时界面会写明「按上限 N 条截断，后面还有没列出」，
按序号选超出名单的那些照样有效。为什么必须展开：**清单地址建成一条任务时，yt-dlp 对整份清单
输出的是同一个固定文件名，一集覆盖一集，最后只剩最后一集**，而回执上会写着「已完成」——这种成功
比失败更难发现。

## 按后缀自动归档（v1.18.4 新增，默认关闭）

「下载目录里一堆文件混在一起」的解法：设置里写几行 `后缀 = 子目录`，命中的成品直接落进对应子目录。

```
mp4,mkv,webm = Video
mp3,m4a,flac = Music
zip,7z,rar = Archives
srt,ass = Subtitles
```

- 三端是同一套解释：桌面端「设置 → 按后缀自动归档」（有「套用常见分档」一键填好）、
  网页版设置里同一栏、`zdm config archive_rules "mp4 = Video"`。
- **默认空 = 关闭**。下载器不该不打招呼就重排你的目录。
- 只决定**落在哪儿**，不决定**落不落盘**：字幕、额外音轨这类附带文件跟着成品走，
  不会出现 `.mkv` 在 Video、配它的 `.srt` 在 Subtitles。
- 子目录必须是**下载目录里的相对路径**：`/Volumes/x`、`D:\Video`、`~/x` 当场拒绝；
  同一个后缀写进两条规则也拒绝，并点名是哪两条。写坏一行不会影响其它行。
- 只对**之后的下载**生效，已经下好的文件要靠下面那条「整理」按钮来补，规则改动不会自动去重排你的目录；
  没归档成功时不静默回到原目录，而是在任务上留一句理由（桌面端状态列悬停、网页版任务行橙色提示、
  `/api/tasks` 的 `archive_note`）。

### 把规则补到已经下好的文件上（v1.18.11 新增）

规则是后配的、文件早就散在下载目录根上——这是历史欠账。桌面端设置页的「按后缀自动归档」里、
网页版设置页的同一张卡片上都有 **「整理已经下好的文件…」**，命令行是 `zdm archive`：

```bash
zdm archive                        # 只列清单：将移动哪些、跳过哪些、为什么
zdm archive --apply                # 看过清单，再真搬
zdm archive -o /Volumes/移动盘/剧集  # 整理别的目录（必须是下载目录或某个任务的目录）
zdm archive --json                 # 完整报告，给脚本看
```

三端点出来的都是**先看清单、确认了才动手**：界面第一屏只回答「会搬成什么样」，
点了「真正移动这些文件」才执行。几条边界是刻意这么定的：

- **在跑的任务不碰**：还在下载、暂停中、排队里的任务持有的文件（成品名、`.part`、`.zdm`）一律跳过，
  并写明是哪个任务的哪个状态；不然搬运和写入方同时碰同一个文件，半截文件就废了。
- **只整理这一层**：子目录里不再套一层，符号链接不动。`.mp4.part` 这种半截文件只有它的**逻辑名**
  命中规则时才列进跳过——`#EXT-X` 之类无关的临时文件不会来刷屏。
- **撞名只跳过、不自动改名**：目标位置已有同名文件时列为跳过。自动改名成 `xxx(1).mp4` 看着贴心，
  实际是把两份内容都留着、还让你自己猜哪份是哪份；要覆盖请自己搬走那个占位的。
- **出不了下载目录**：`directory` 只接受下载目录或某个任务自己的目录（越界回 403），规则里的子目录
  也只能是相对路径，清单里出现绝对路径落点的行会被直接判为跳过。
  占用判定认的是**文件本身**：目录走符号链接挂载（外置盘、把 `~/Downloads` 软链到别处）时，
  「下载目录」和「任务目录」那两串字符可能写法不同，整理前都会解析成同一个真实路径再比 ——
  只按字符串比会漏，漏了就等于把某个任务正在写的文件搬走。
- **改完规则没保存会提醒**：整理用的是**已保存**的那份；框里改了没点保存，清单上会写明这一点，
  免得你以为按新规则整理、结果规则根本没生效。
- 搬成功的文件，任务记录里的路径会同步改掉（并写回 `state.json`），所以「打开文件 / 在文件夹中显示」
  不会在你整理完之后还指着老位置。一次最多处理 5000 个，超了会说明「整理完可以再点一次」。

## 常用命令

```bash
zdm start --daemon          # 后台启动服务
zdm status                  # 查看服务与任务
zdm add "URL" -c 16 -o ~/Downloads   # 添加下载，16 连接
zdm add "https://…/live.m3u8" --live-limit 30:00   # 录 30 分钟直播后自动收工
zdm playlist "https://www.youtube.com/playlist?list=PL…"   # 只列名单，不建任务
zdm add "https://www.youtube.com/playlist?list=PL…" --playlist 1-20   # 展开成 20 条任务
zdm config archive_rules "mp4,mkv = Video"   # 按后缀归档到 下载目录/Video
zdm archive                     # 用现有规则清点已经下好的文件（只列清单，不动手）
zdm archive --apply             # 上面那步确认过了，真搬
zdm list                    # 列出任务
zdm config speed_limit 2097152        # 限速 2 MB/s
zdm config rpc_enabled false          # 关掉 aria2 JSON-RPC 兼容层
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

## v1.13.0 新增：视频卡片的「下载此视频」按钮（2026-10-20）

装好扩展后，**B站 / YouTube 的每个视频卡片右上角**，鼠标移上去会浮出一颗
「下载此视频」按钮：

- 点它 → 弹出该视频的**清晰度列表**（1080P / 720P / 480P / 音频…）
- 选一个 → 直接推送给 ZDM 下载
- 按钮用 Shadow DOM 隔离，站点自己的 CSS 不会把它打乱

**清晰度是怎么来的**：扩展就地读取页面里已经加载好的媒体数据（不额外发请求）：
YouTube 读播放器的 `streamingData`，B站读 `__playinfo__` 里的 `dash`。
所以在**列表页**只能看到「这个视频在页面上」，真正的清晰度列表要在
**打开视频进入播放页**后才完整。

**音视频自动合并**：B站/YouTube 的 DASH 流把画面和声音分成两个文件。
扩展会把配套的音频轨一起交给 ZDM，ZDM 下载后用 `ffmpeg` 合成一个**有声音**的视频。
机器上没有 ffmpeg 时视频照旧保存，只是没有声音（任务上会写明）。

**设置里的站点 Cookie**：设置 → 「站点 Cookie」可以填 YouTube / B站的 Cookie。
扩展推送任务时本来就会带当前页面的 Cookie，这里填的是**兜底**——
从桌面端手动添加链接、或者服务器明确要求登录态时才需要。
填了之后，ZDM 会按 URL 域名自动带上对应的 Cookie 与 Referer。

**安装包**

| 平台 | 文件 |
|---|---|
| macOS | `ZDM-<版本>-<架构>.dmg` |
| Linux | `zdm_1.13.0_amd64.deb` / `zdm-linux-amd64.tar.gz` |
| Windows | `ZDM-Setup-1.13.0.exe`（原生 C#/WPF） |

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
20. **删除/暂停谎报失败**：`Manager.remove()` 在 HTTP 请求线程里同步等 worker 退出（最坏 12 秒）、
    再删文件并回收分片目录，而命令行 10 秒、桌面端 8 秒就超时 —— 服务端删成了，界面回
    「连不上服务」。现在请求线程只等 3 秒，列表与 `state.json` 当场更新，等 worker 退出后的
    文件清理放到后台线程（顺带解决 Windows 上「句柄没关删不掉」）；`shutdown()` 退出前给这些
    线程 1.5 秒预算，太长的等待会被 launchd 记成意外退出。
21. **暂停→继续把任务改名，旧半截文件成孤儿**：单流任务每次重新开始都重新探查一次，之后的
    `_unique_name()` 把目录里那份 `xxx.bin.part`（正是它自己断点续传的凭据）当成重名冲突，改成
    `xxx(1).bin`：已下字节作废、每轮继续再加一个后缀，删除只删得掉当前名字那一份，其余永久留在
    下载目录；重启服务后 `state.json` 里的名字和磁盘上的 `.part` 对不上，续传同样变重下。
    现在改名会把「自己那份 `.part`」排除在冲突之外，别人的同名文件/同名 `.part` 照旧让位。

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
