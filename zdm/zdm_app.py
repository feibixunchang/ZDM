"""Entry point for the frozen desktop build (PyInstaller).

Two names point at the same executable, which is what makes one binary serve
both roles - exactly like the macOS bundle's launcher:

* ``ZDM`` / ``zdm``       -> the CLI (``zdm start``, ``zdm add URL``, ...)
* ``zdm-desktop``         -> the Qt window (what the .desktop file launches)

``zdm gui`` still works: the CLI hands over to :func:`zdm.desktop.ensure_desktop`,
which looks up ``zdm-desktop`` on PATH and falls back to re-execing this same
binary.

``--selfcheck`` is for packaging/CI: it reports what the bundle can actually do
(interpreter, PyQt5, resource paths) without needing a display.
"""

from __future__ import annotations

import os
import sys


def _print_version() -> int:
    from zdm import __version__

    print(f"ZDM {__version__}")
    return 0


def _selfcheck() -> int:
    """Machine-readable smoke test for the packaged app (no GUI needed)."""
    import json

    from zdm import __version__, runtime

    root = runtime.app_root()
    report = {
        "version": __version__,
        "frozen": runtime.is_frozen(),
        "executable": runtime.exe_path(),
        "app_root": root,
        "python": sys.version.split()[0],
        "web_index": os.path.exists(os.path.join(root, "web", "index.html")),
        "extension_manifest": os.path.exists(
            os.path.join(root, "extension", "manifest.json")
        ),
        # The media sniffer is a separate content script; a package that
        # ships the manifest but forgets sniffer.js would look fine here and
        # still fail the moment the user opens a video page.
        "extension_sniffer": os.path.exists(
            os.path.join(root, "extension", "sniffer.js")
        ),
        # 文案层少了 ui18n.js，扩展照样装、照样接管下载，只是英文用户看到的全是
        # 中文 —— 而且 manifest 里那条 "js": ["ui18n.js", ...] 会让每个内容脚本
        # 在控制台报「找不到文件」。构建期就该挡住，别等用户发现界面没翻。
        "extension_ui18n": os.path.exists(
            os.path.join(root, "extension", "ui18n.js")
        ),
        "theme_module": False,
        "pyqt5": "",
        "qt": "",
        # 这两个二进制是「装完才暴露」的重灾区：少了 yt-dlp，YouTube 直接不能用；
        # 少了 ffmpeg，B站/YouTube 下完是一个只有画面的文件。
        # 在这里检查，构建期就能挡住，而不是等用户听不见声音。
        "ytdlp": "",
        "ffmpeg": "",
        # YouTube 的签名/n 参数需要 JS 运行时；现在随包带 deno。
        "deno": "",
        # HLS/DASH 流媒体引擎：少了它 m3u8 会退回 ffmpeg（能下完，但没有分片
        # 并发、单片重试和按分片续传）。发布包必须带，所以和上面几个一样是硬条件。
        "m3u8dl_re": "",
        # FTP/FTPS 走标准库，SFTP 要 paramiko。冻结包里少 paramiko，界面照样开，
        # 只有粘一条 sftp:// 链接时才在「添加」那步报「这台机器没有 SFTP 通道」——
        # 用户在下载页是没法 pip install 的，所以这里当构建期硬条件。
        "ftp_channel": False,
        "sftp": "",
        # Firefox 的 .xpi 是从磁盘上这份 extension/ 现打的（桌面端导出、网页版
        # 下载、CLI 三条路都靠它）。firefox_ext 进不了冻结包，三条入口一起变成
        # 报错，而且只有用户点击才知道 —— 所以也算构建期硬条件。
        "firefox_ext": False,
    }
    try:
        import theme  # noqa: F401  (sits next to gui.py, not in the package)

        report["theme_module"] = True
    except Exception as e:  # noqa: BLE001
        report["theme_module_error"] = f"{type(e).__name__}: {e}"
    try:
        from PyQt5.QtCore import QT_VERSION_STR, PYQT_VERSION_STR
        import PyQt5

        report["pyqt5"] = PYQT_VERSION_STR
        report["qt"] = QT_VERSION_STR
        report["pyqt5_path"] = os.path.dirname(PyQt5.__file__)
    except Exception as e:  # noqa: BLE001
        report["pyqt5_error"] = f"{type(e).__name__}: {e}"
    try:
        from zdm import ytdlp_bin

        report["ytdlp"] = ytdlp_bin.binary_path() or ""
    except Exception as e:  # noqa: BLE001
        report["ytdlp_error"] = f"{type(e).__name__}: {e}"
    try:
        from zdm import ytdlp_bin as _yb

        report["deno"] = _yb.find_js_runtime() or ""
    except Exception as e:  # noqa: BLE001
        report["deno_error"] = f"{type(e).__name__}: {e}"
    try:
        from zdm.core import _find_ffmpeg

        report["ffmpeg"] = _find_ffmpeg() or ""
    except Exception as e:  # noqa: BLE001
        report["ffmpeg_error"] = f"{type(e).__name__}: {e}"
    try:
        from zdm import re_bin

        report["m3u8dl_re"] = re_bin.binary_path() or ""
    except Exception as e:  # noqa: BLE001
        report["m3u8dl_re_error"] = f"{type(e).__name__}: {e}"
    # 探到了还不够：得证明它真能跑。Linux 包可能带错架构、macOS 上的 .NET
    # 单文件可能被签名/隔离属性拦住，而 binary_path() 只看文件在不在、能不能
    # 执行 —— 这些都要到第一次下 m3u8 才暴露。发布自检里跑一次 --version。
    if report["m3u8dl_re"]:
        try:
            import re as _re
            import subprocess

            r = subprocess.run(
                [report["m3u8dl_re"], "--version"],
                capture_output=True,
                timeout=60,
                **runtime.silent_proc_kwargs()
            )
            # 引擎的终端渲染器会把光标控制码混在版本串里（实测 0.6.0 输出带
            # \x1b[?12l），报告是给 CI 读的 JSON，去掉再存。
            txt = _re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", (r.stdout or b"").decode("utf-8", "ignore"))
            report["m3u8dl_re_version"] = txt.strip()[:60]
            if r.returncode != 0:
                report["m3u8dl_re"] = ""
                report["m3u8dl_re_error"] = "exit %d" % r.returncode
        except Exception as e:  # noqa: BLE001
            report["m3u8dl_re"] = ""
            report["m3u8dl_re_error"] = f"{type(e).__name__}: {e}"
    try:
        from zdm import ftp as _ftp
        from zdm import sftp as _sftp

        report["ftp_channel"] = _ftp.is_ftp("ftp://example.com/file.bin")
        report["sftp"] = _sftp.version()
    except Exception as e:  # noqa: BLE001
        report["ftp_error"] = f"{type(e).__name__}: {e}"
    try:
        from zdm import firefox_ext as _fx

        ext_dir = runtime.extension_dir()
        report["firefox_ext"] = bool(ext_dir) and len(_fx.build_zip(ext_dir)) > 1024
    except Exception as e:  # noqa: BLE001
        report["firefox_ext"] = False
        report["firefox_ext_error"] = f"{type(e).__name__}: {e}"
    # Windows 上的 exe 是 GUI 程序（console=False），没有可写的 stdout，CI 读不到
    # 下面这行 print。所以额外支持落文件；退出码仍然是唯一的通过/失败信号。
    out = os.environ.get("ZDM_SELFCHECK_OUT")
    if out:
        try:
            with open(out, "w", encoding="utf-8") as fh:
                json.dump(report, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass
    try:
        # 和 fetch_bin.py 同一件事：Windows 控制台是代码页（CI 上 cp1252），
        # 报告里出现中文路径时 print 会抛 UnicodeEncodeError，把「自检其实通过」
        # 变成非 0 退出、整条 Windows 流水线红掉。
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - console=False 的 exe 没有真 stdout
        pass
    print(json.dumps(report, ensure_ascii=False, indent=2))
    ok = bool(
        report["web_index"]
        and report["extension_manifest"]
        and report["extension_sniffer"]
        and report["extension_ui18n"]
        and report["theme_module"]
        and report["pyqt5"]
        and report["ytdlp"]
        and report["ffmpeg"]
        # deno 是 YouTube 签名/ n 参数解析需要的 JS 运行时。缺了它包照样能启动，
        # 但 YouTube 会在运行时才失败 —— 所以必须算进自检的通过条件。
        and report["deno"]
        # 流媒体引擎同理：包照样启动、mp4 照样下，只有第一次下 m3u8 才看出退化。
        and report["m3u8dl_re"]
        and report["ftp_channel"]
        and report["sftp"]
        and report["firefox_ext"]
    )
    return 0 if ok else 1


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv and argv[0] in ("--version", "-V", "version"):
        return _print_version()
    if argv and argv[0] == "--selfcheck":
        return _selfcheck()

    # Explicit role override: desktop.py re-execs the frozen binary with this
    # flag because argv[0] there is "ZDM" (CLI name), not "zdm-desktop".
    if argv and argv[0] == "--gui":
        import gui

        return gui.main(argv[1:])

    name = os.path.basename(sys.argv[0] or "").lower()
    if name.startswith(("zdm-desktop", "zdm-gui")):
        import gui

        return gui.main(argv)

    # Windows 的窗口版 exe 没有控制台：双击（或开始菜单快捷方式）启动时
    # argv 是空的，用户要的显然是**界面**。这里以前落到 CLI 的默认动作
    # （`start`：前台服务 + 浏览器网页版），于是「桌面界面没了，只剩网页版」。
    # `gui` 子命令会先确保本机服务在跑，再打开窗口（已开着则唤起）。
    if not argv and os.name == "nt":
        argv = ["gui"]

    from zdm.cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
