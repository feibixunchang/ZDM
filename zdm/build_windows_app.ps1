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
# `py -3` is the Windows launcher and always exists when Python is installed
# properly; the bare names are the fallback for scoop/conda setups.
function Find-Python {
    $tries = @(
        @{ Exe = "py";      Args = @("-3") },
        @{ Exe = "python";  Args = @() },
        @{ Exe = "python3"; Args = @() }
    )
    foreach ($t in $tries) {
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
    # BtbN first (stable "latest" asset URL), gyan.dev as fallback: one of them
    # being slow/blocked from CI should not fail the whole release.
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
foreach ($need in @("bin\yt-dlp.exe", "bin\ffmpeg.exe")) {
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
        $iss = Join-Path $Src "installer.iss"
        & $iscc "/DAppVersion=$Version" "/DSourceDir=$AppDir" "/DOutDir=$OutDir" $iss
        if ($LASTEXITCODE -ne 0) { Fail "Inno Setup 失败" }
        $setup = Join-Path $OutDir "ZDM-Setup-$Version.exe"
        if (-not (Test-Path $setup)) { Fail "没有产出 $setup" }
        Write-Step "built $setup ($([math]::Round((Get-Item $setup).Length / 1MB, 1)) MB)"
    }
}

Write-Step "done: $OutDir"
