ZDM 三端安装包 — v1.11.0
====================

1) macOS（推荐）
   - ZDM-1.11.0.dmg          ← 双击挂载后把 ZDM.app 拖进 Applications
   - zdm-macos.zip          ← 解压后运行 bash install.sh
                              （自动生成 ~/Applications/ZDM.app + 登录自启）
   dmg 内含：ZDM.app、extension/（浏览器扩展）、安装与使用说明.md

   ZDM.app 是**自包含**的：Python 运行时 + 含 PyQt5/PyObjC 的 venv 都嵌在包内，
   目标机器无需预装 Python。主程序为 clang 编译的 Mach-O（内嵌 CPython），
   Dock 图标、点击唤回窗口均为 macOS 原生行为。

   ※ 加载浏览器扩展时，~/Library 默认隐藏。在 Chrome「加载已解压的扩展程序」的
     选文件夹窗口按 Command + Shift + G，粘贴：
     ~/Library/Application Support/ZDM/extension

2) Linux
   - zdm_1.11.0_amd64.deb    ← sudo apt install ./zdm_1.11.0_amd64.deb（自带 Python + PyQt5/Qt）
   - zdm-linux.tar.gz       ← 免 root：解压后 ./install.sh

3) Windows
   - ZDM-PyQt5-Setup-1.11.0.exe  ← PyQt5 版，双击运行，用户级安装、无需管理员
                                  （自动检测/安装 Python 与 PyQt5）
   - ZDM-Setup-1.11.0.exe        ← 原生客户端（C#/WPF，推荐），双击安装

本版新增（v1.11.0）：
  · 主页面工具栏改为 ⏸ / ▶ 图标按钮（全部暂停 / 全部继续），托盘菜单同步
  · 「打开」打开下载的文件本身（.dmg 直接进安装界面），另设「目录」定位文件
  · 桌面端未运行时，浏览器扩展 / zdm add 发起下载会自动拉起窗口
  · 窗口标题、状态栏、托盘、关于框显示版本号
  · 有新版本时主界面弹横幅，可一键下载并打开安装包（也可忽略该版本）
  · 修复点菜单栏图标时菜单文字发灰、点不了的问题

注：全部平台安装包均由 GitHub Actions 自动构建：.deb / tarball / .exe 在
    ubuntu-latest 上产出，.dmg 在 macos-latest 上产出（zdm/build_macos_app.sh），
    推 tag 即自动挂到 GitHub Release。

安装后可在桌面端「设置」或托盘图标右键菜单里，随时开关「开机自启」与「退出」。
浏览器扩展：加载安装目录下的 extension/ 文件夹即可接管下载。
