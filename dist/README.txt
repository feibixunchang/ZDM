ZDM 三端安装包
=============

1) Linux
   - zdm_1.0.0_all.deb   （推荐，需 root：sudo dpkg -i zdm_1.0.0_all.deb）
   - zdm-linux.tar.gz    （免 root：解压后 ./install.sh）

2) macOS
   - zdm-macos.zip
       解压后运行:  bash install.sh   （自动生成 ~/Applications/ZDM.app + 登录自启）
       若要 .dmg：在 Mac 上执行包内 build_macos_dmg.sh 一键生成 ZDM-1.0.0.dmg
       （.dmg 需 macOS 的 hdiutil 生成，无法在 Linux 上构建）

3) Windows
   - ZDM-Setup-1.0.0.exe
       双击运行，按提示安装（用户级、无需管理员；自动检测/安装 Python 与 PyQt5）

安装后在桌面端「设置」或托盘图标右键菜单里，可随时开关「开机自启」与「退出」。
浏览器扩展：加载各包内 extension/ 目录即可接管下载。
