ZDM 安装包 — v1.1.0
====================

1) macOS（推荐）
   - ZDM-1.1.0.dmg          ← 双击挂载后把 ZDM.app 拖进 Applications
   - zdm-macos.zip          ← 解压后运行 bash install.sh
                              （自动生成 ~/Applications/ZDM.app + 登录自启）
   dmg 内含：ZDM.app、extension/（浏览器扩展）、安装与使用说明.md

   v1.1.0 起 macOS 桌面端是**真正的原生 App**：主程序为 clang 编译的 Mach-O，
   通过 Python C API 内嵌解释器，Dock 图标、点击唤回窗口均为 macOS 原生行为。

   ※ 加载浏览器扩展时，~/Library 默认隐藏。在 Chrome「加载已解压的扩展程序」的
     选文件夹窗口按 Command + Shift + G，粘贴：
     ~/Library/Application Support/ZDM/extension

2) Linux
   - zdm_1.1.0_all.deb      ← sudo dpkg -i zdm_1.1.0_all.deb（自动安装 PyQt5）
   - zdm-linux.tar.gz       ← 免 root：解压后 ./install.sh

3) Windows
   - ZDM-Setup-1.1.0.exe    ← 双击运行，用户级安装、无需管理员
                              （自动检测/安装 Python 与 PyQt5）

注：全部平台安装包均由 GitHub Actions 自动构建：.deb / tarball / .exe 在
    ubuntu-latest 上产出，.dmg 在 macos-latest 上产出（zdm/build_macos_app.sh），
    推 tag 即自动挂到 GitHub Release。

    macOS 的 ZDM.app 是**自包含**的：Python 运行时 + 含 PyQt5/PyObjC 的 venv
    都嵌在包内，目标机器无需预装 Python，拖进 Applications 即可运行。

安装后可在桌面端「设置」或托盘图标右键菜单里，随时开关「开机自启」与「退出」。
浏览器扩展：加载安装目录下的 extension/ 文件夹即可接管下载。