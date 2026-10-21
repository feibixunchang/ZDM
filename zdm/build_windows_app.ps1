# Build the self-contained Windows client and pack it into an installer.
#
#   powershell -ExecutionPolicy Bypass -File zdm\build_windows_app.ps1 [-OutDir DIR] [-Version X.Y.Z]
#
# This is the Windows counterpart of build_linux_app.sh: the same PyInstaller
# spec (zdm.spec) freezes CPython + PyQt5 + the app's resources into a onedir
# bundle, so the installed ZDM needs no Python and no pip on the user's machine.
#
# Why PyQt5 on Windows instead of the old C#/WPF client: the WPF client carried
# its own download engine and its own HTTP API, so every fix had to be written
# twice (the YouTube resolve / audio-mux work never reached Windows). One UI +
# one engine on all three platforms is worth ~25 MB of bundle size.
#
# Requires: python 3.9+ (with venv), network access to PyPI/GitHub,
#           Inno Setup 6 (iscc.exe) for the installer step.

param(
    [string]$OutDir = "",
    [string]$Version = "",
    [string]$YtDlpVersion = "2026.08.19",
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
# Invoke-WebRequest redraws a progress bar per chunk in PS 5.1; on a 130 MB
# ffmpeg zip that alone turns a 20 s download into a 10 minute one.
$ProgressPreference = "SilentlyContinue"
$Src = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Src
if (-not $OutDir) { $OutDir = Join-Path $Root "dist" }
$OutDir = [System.IO.Path]::GetFullPath($OutDir)
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

# ------------------------------------------------------------------ version
# zdm/zdm/__init__.py is the single source of truth (the /api endpoint reports
# the same value). The installer and the exe metadata both read it from here,
# which is why the "check update says 1.13.0 but the title bar says 1.14.7"
# class of bug cannot come back.
if (-not $Version) {
    $init = Join-Path $Src "zdm\__init__.py"
    if (Test-Path $init) {
        $m = Select-String -Path $init -Pattern '__version__\s*=\s*"([^"]+)"' | Select-Object -First 1
        if ($m) { $Version = $m.Matches[0].Groups[1].Value }
    }
}
if (-not $Version) { $Version = "0.0.0" }
Write-Step "version $Version"

# ------------------------------------------------------------------- python
# 顺序很重要：先 `python`（CI 的 setup-python 会把它排到 PATH 最前，版本可控），
# 再 `py -3`（Windows 官方安装器的启动器，取最高的 3.x —— 开发机往往只有它）。
# 反过来的话 CI 会用 runner 上那个没被固定的解释器，构建结果不可复现。
# $env:ZDM_PYTHON 可强制指定（例如 `ZDM_PYTHON=C:\Python312\python.exe`）。
function Find-Python {
    $cands = @()
    if ($env:ZDM_PYTHON) { $cands += @{ Exe = $env:ZDM_PYTHON; Args = @() } }
    $cands += @(
        @{ Exe = "python";  Args = @() },
        @{ Exe = "python3"; Args = @() },
        @{ Exe = "py";      Args = @("-3") }
    )
    foreach ($t in $cands) {
        if (-not (Get-Command $t.Exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $ver = & $t.Exe @($t.Args) -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -ne 0 -or -not $ver) { continue }
            $p = $ver.ToString().Trim().Split(".")
            if ([int]$p[0] -eq 3 -and [int]$p[1] -ge 9) { return $t }
        } catch { }
    }
    return $null
}

$PySpec = Find-Python
if (-not $PySpec) { Fail "找不到 Python 3.9+；请先安装 https://www.python.org/downloads/ 并勾选 Add to PATH" }
Write-Step "python: $($PySpec.Exe) $($PySpec.Args -join ' ')"

# --------------------------------------------------------------------- venv
# A throwaway venv keeps PyQt5/PyInstaller out of the user's site-packages and
# makes the build reproducible. It lives under OutDir so `git status` stays clean.
$Venv = Join-Path $OutDir "build-venv"
$VenvPy = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    Write-Step "creating build venv"
    & $PySpec.Exe @($PySpec.Args) -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Fail "venv 创建失败" }
}

Write-Step "installing PyQt5 + PyInstaller"
& $VenvPy -m pip install --quiet --upgrade pip
& $VenvPy -m pip install --quiet "PyQt5>=5.15,<5.16" "pyinstaller>=6.3"
if ($LASTEXITCODE -ne 0) { Fail "pip install 失败" }
& $VenvPy -c "import PyInstaller; from PyQt5.QtCore import QT_VERSION_STR, PYQT_VERSION_STR; print('    PyQt5 %s | Qt %s | PyInstaller %s' % (PYQT_VERSION_STR, QT_VERSION_STR, PyInstaller.__version__))"

# --------------------------------------------------------------- bundled bins
# Two binaries ride along, exactly like the Linux/macOS packages:
#   bin/yt-dlp.exe  — YouTube resolve + download (self-contained interpreter;
#                     the pip package refuses Python 3.9 since 2026.x)
#   bin/ffmpeg.exe  — muxes B站/YouTube DASH video+audio. Without it a "video"
#                     download silently produces a silent file, which is
#                     impossible to notice unless you actually play the result.
$Bin = Join-Path $Src "bin"
New-Item -ItemType Directory -Force -Path $Bin | Out-Null

$YtDlpExe = Join-Path $Bin "yt-dlp.exe"
$needYtDlp = $true
if (Test-Path $YtDlpExe) {
    & $YtDlpExe --version *> $null
    if ($LASTEXITCODE -eq 0) { $needYtDlp = $false }
}
if ($needYtDlp) {
    Write-Step "downloading yt-dlp.exe $YtDlpVersion"
    $url = "https://github.com/yt-dlp/yt-dlp/releases/download/$YtDlpVersion/yt-dlp.exe"
    try {
        Invoke-WebRequest -Uri $url -OutFile $YtDlpExe -UseBasicParsing
    } catch {
        Fail "下载 yt-dlp 失败（$url）：$($_.Exception.Message)"
    }
    & $YtDlpExe --version *> $null
    if ($LASTEXITCODE -ne 0) { Fail "随包 yt-dlp.exe 无法执行（文件损坏或被安全软件拦截）" }
}
Write-Step "yt-dlp: $(& $YtDlpExe --version)"

$FfmpegExe = Join-Path $Bin "ffmpeg.exe"
if (-not (Test-Path $FfmpegExe)) {
    Write-Step "downloading ffmpeg.exe"
    $zip = Join-Path $OutDir "ffmpeg-win64.zip"
    $tmp = Join-Path $OutDir "ffmpeg-extract"
    # BtbN 优先：它的 "latest" 资产地址稳定，实测 CI 下载约 3 秒（161 MB 的
    # gpl 版，打出来的安装包 91.6 MB）。gyan.dev 的 essentials 版更小，但它在
    # GitHub runner 上的速度不稳，所以只作兜底 —— 体积差十几 MB，比构建失败划算。
    $urls = @(
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
        "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    )
    $ok = $false
    foreach ($u in $urls) {
        try {
            Invoke-WebRequest -Uri $u -OutFile $zip -UseBasicParsing
            if ((Get-Item $zip).Length -gt 10MB) { $ok = $true; break }
        } catch {
            Write-Host "    fallback: $u 不可用"
        }
    }
    if (-not $ok) { Fail "下载 ffmpeg 失败；DASH 音视频将无法合流（B站下载会没声音）" }
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    $found = Get-ChildItem -Path $tmp -Filter "ffmpeg.exe" -Recurse | Select-Object -First 1
    if (-not $found) { Fail "ffmpeg 压缩包结构异常，找不到 ffmpeg.exe" }
    Copy-Item $found.FullName $FfmpegExe -Force
    Remove-Item -Recurse -Force $tmp, $zip -ErrorAction SilentlyContinue
}
Write-Step "ffmpeg: $((Get-Item $FfmpegExe).Length / 1MB) MB"

# deno.exe: yt-dlp 2026 起解 YouTube 签名/n 参数必须有 JS 运行时。Windows
# 安装包同样内置，用户装完即用（fetch_bin.py 固定版本，跨平台）。
$DenoExe = Join-Path $Bin "deno.exe"
if (-not (Test-Path $DenoExe)) {
    Write-Step "downloading deno.exe"
    & $VenvPy (Join-Path $Src "fetch_bin.py") --dest $Bin --os windows
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $DenoExe)) {
        Fail "下载 deno 失败（YouTube 解析将不可用）"
    }
}
& $DenoExe --version *> $null
if ($LASTEXITCODE -ne 0) { Fail "随包 deno.exe 无法执行（文件损坏或被安全软件拦截）" }
Write-Step "deno: $(& $DenoExe --version)"

# ------------------------------------------------------------------ freeze
# build/ and dist/ZDM are disposable; leaving a stale tree behind is how a
# "new" build ends up shipping last week's gui.py.
$Work = Join-Path $OutDir "build-windows"
Write-Step "freezing (PyInstaller onedir)"
if (Test-Path $Work) { Remove-Item -Recurse -Force $Work }
$log = Join-Path $OutDir "pyinstaller.log"
& $VenvPy -m PyInstaller --clean --noconfirm `
    --distpath $OutDir --workpath $Work `
    (Join-Path $Src "zdm.spec") *> $log
if ($LASTEXITCODE -ne 0) {
    Write-Host "---- tail of $log ----" -ForegroundColor Yellow
    Get-Content $log -Tail 40
    Fail "PyInstaller 失败"
}

$AppDir = Join-Path $OutDir "ZDM"
$AppExe = Join-Path $AppDir "ZDM.exe"
if (-not (Test-Path $AppExe)) { Fail "没有产出 $AppExe" }

# ----------------------------------------------------------------- self-check
# The windowed exe has no attached stdout, so the report has to go through a
# file. Exit code is still the pass/fail signal.
Write-Step "self-check"
$report = Join-Path $OutDir "selfcheck.json"
$env:ZDM_SELFCHECK_OUT = $report
$p = Start-Process -FilePath $AppExe -ArgumentList "--selfcheck" -Wait -PassThru -NoNewWindow
$env:ZDM_SELFCHECK_OUT = ""
if (Test-Path $report) { Get-Content $report }
if ($p.ExitCode -ne 0) { Fail "self-check 失败（exit $($p.ExitCode)）—— 缺 PyQt5 / web / extension 或 theme.py" }

$internal = Join-Path $AppDir "_internal"
foreach ($need in @("bin\yt-dlp.exe", "bin\ffmpeg.exe", "bin\deno.exe")) {
    if (-not (Test-Path (Join-Path $internal $need))) { Fail "打包结果缺少 _internal\$need" }
}
$hostExe = Join-Path $AppDir "ZDM-NativeHost.exe"
if (-not (Test-Path $hostExe)) { Fail "打包结果缺少 ZDM-NativeHost.exe（浏览器原生消息主机）" }

# ------------------------------------------------------------------ installer
if ($SkipInstaller) {
    Write-Step "跳过安装包（-SkipInstaller）"
} else {
    $iscc = $null
    foreach ($c in @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )) {
        if ($c -and (Test-Path $c)) { $iscc = $c; break }
    }
    if (-not $iscc) {
        $cmd = Get-Command iscc -ErrorAction SilentlyContinue
        if ($cmd) { $iscc = $cmd.Source }
    }
    if (-not $iscc) {
        Write-Host "WARNING: 找不到 Inno Setup 6（iscc.exe），只产出 $AppDir" -ForegroundColor Yellow
        Write-Host "         装一个再跑，或从 CI 的 artifact 里取已签好的安装包。" -ForegroundColor Yellow
    } else {
        Write-Step "packing installer (Inno Setup)"
        # 扩展 ID 由 manifest.json 的 "key" 推导（sha256 前 32 位映射到 a-p），
        # 和 install.sh 用的是同一个算法。这里现算而不是在 .iss 里写死：
        # key 一改而 .iss 没跟着改，原生消息会被 Chrome 拒绝，而且是静默失败。
        # 注意：不能改用 here-string（@" ... "@）—— 它的结束符必须顶格，
        # 在缩进块里写会直接语法错误。
        $extId = ""
        $pyExtId = "import base64,hashlib,json,sys;m=json.load(open(sys.argv[1],encoding='utf-8'));h=hashlib.sha256(base64.b64decode(m['key'])).hexdigest()[:32];print(''.join(chr(ord('a')+int(c,16)) for c in h))"
        try {
            $extId = "$(& $VenvPy -c $pyExtId (Join-Path $Src 'extension\manifest.json'))".Trim()
        } catch {
            Write-Host "WARNING: 推导扩展 ID 失败，installer.iss 用内置值" -ForegroundColor Yellow
        }
        if ($extId) { Write-Step "extension id: $extId" }

        $iss = Join-Path $Src "installer.iss"
        $defs = @("/DAppVersion=$Version", "/DSourceDir=$AppDir", "/DOutDir=$OutDir")
        if ($extId) { $defs += "/DExtId=$extId" }
        & $iscc @defs $iss
        if ($LASTEXITCODE -ne 0) { Fail "Inno Setup 失败" }
        $setup = Join-Path $OutDir "ZDM-Setup-$Version.exe"
        if (-not (Test-Path $setup)) { Fail "没有产出 $setup" }
        Write-Step "built $setup ($([math]::Round((Get-Item $setup).Length / 1MB, 1)) MB)"
    }
}

Write-Step "done: $OutDir"
