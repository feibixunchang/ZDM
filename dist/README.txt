ZDM 安装包 — v1.1.0
====================

1) macOS（推荐，最新版）
   - ZDM-1.1.0.dmg          ← v1.1.0，双击挂载后把 ZDM.app 拖进 Applications
   - zdm-macos.zip          ← v1.1.0，解压后运行 bash install.sh
                              （自动生成 ~/Applications/ZDM.app + 登录自启）
   dmg 内含：ZDM.app、extension/（浏览器扩展）、安装与使用说明.md

   v1.1.0 起 macOS 桌面端是**真正的原生 App**：主程序为 clang 编译的 Mach-O，
   通过 Python C API 内嵌解释器，Dock 图标、点击唤回窗口均为 macOS 原生行为。

   ※ 加载浏览器扩展时，~/Library 默认隐藏。在 Chrome「加载已解压的扩展程序」的
     选文件夹窗口按 Command + Shift + G，粘贴：
     ~/Library/Application Support/ZDM/extension

2) Linux
   - zdm-linux.tar.gz       ← v1.1.0，免 root：解压后 ./install.sh
   - zdm_1.0.0_all.deb      ← 仍为 1.0.0；.deb 需在 Linux 上用 dpkg-deb 构建，
                              想要 1.1.0 请到 Linux 仓库跑 python3 build_dist.py

3) Windows
   - ZDM-Setup-1.0.0.exe    ← 仍为 1.0.0；.exe 需用 7-Zip 的 7zS.sfx 构建，
                              想要 1.1.0 请在装有 7z 的环境跑 python3 build_dist.py

   双击运行，按提示安装（用户级、无需管理员；自动检测/安装 Python 与 PyQt5）

安装后可在桌面端「设置」或托盘图标右键菜单里，随时开关「开机自启」与「退出」。
浏览器扩展：加载安装目录下的 extension/ 文件夹即可接管下载。