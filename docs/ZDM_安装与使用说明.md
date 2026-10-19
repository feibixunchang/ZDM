# ZDM 本机安装与使用说明

> 来源仓库：https://github.com/feibixunchang/ZDM
> 安装时间：2026-10-19 ｜ 系统：macOS（arm64）

## 已安装位置
- 程序代码：`~/Library/Application Support/ZDM`（含 `zdm/`、`web/`、`extension/`、`zdm.py`、`gui.py`）
- 运行环境：专属虚拟环境 `~/Library/Application Support/ZDM/venv`（基于系统 `/usr/bin/python3` 3.9.6，已装 PyQt5 5.15.11）
- 桌面 App：`~/Applications/ZDM.app`（双击即用，已去除隔离属性）
- 命令行：`~/.local/bin/zdm`
- 后台服务：launchd 代理 `~/Library/LaunchAgents/com.zdm.service.plist`（登录自启 + 崩溃自动重启）
- 配置/状态：`~/Library/Application Support/ZDM/zdm.json`（默认下载目录 `~/Downloads`，端口 9000）

## 三种使用入口
| 入口 | 命令 | 说明 |
|---|---|---|
| 桌面端 | `zdm gui` 或双击 `~/Applications/ZDM.app` | 原生 Qt 窗口，关闭后收进托盘，下载继续后台跑 |
| 网页端 | `zdm open` 或浏览器打开 http://127.0.0.1:9000/ | 适合远程/多设备管理；桌面端点「打开网页版」按钮即可跳转 |
| 命令行 | `zdm add/list/pause/resume/...` | 终端与脚本使用 |

三者共享同一后台服务与任务队列，可同时打开。

桌面端工具栏按钮：`＋ 下载`、`全部暂停`、`全部继续`、`清除已完成`、**`打开网页版`**、`打开目录`、`设置`。
托盘（菜单栏）图标右键同样提供「打开网页版」。

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
