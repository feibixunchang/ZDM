# ZDM installer for Windows - user level, no administrator rights needed.
# Usage (PowerShell):
#   .\install.ps1                 # install + autostart + launch desktop window
#   .\install.ps1 -NoAutoStart    # install without autostart
#   .\install.ps1 -Uninstall      # remove
param(
    [switch]$Uninstall,
    [switch]$NoAutoStart
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$App = Join-Path $env:LOCALAPPDATA "ZDM"
$StartMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\ZDM"
$Startup = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
$Agent = "com.zdm.service"

# 版本号只认 zdm/__init__.py 一个真源。这里以前写死 "1.11.0"，于是「应用与功能」
# 里永远显示 1.11.0，和界面/更新提示对不上。
$Version = "0.0.0"
$verFile = Join-Path $Root "zdm\__init__.py"
if (Test-Path $verFile) {
    $m = Select-String -Path $verFile -Pattern '^__version__\s*=\s*"([^"]+)"' |
        Select-Object -First 1
    if ($m) { $Version = $m.Matches[0].Groups[1].Value }
}

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

function Find-Python {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        try {
            $exe = & py -3 -c "import sys; sys.stdout.write(sys.executable)" 2>$null
            if ($exe -and (Test-Path $exe)) { return $exe.Trim() }
        } catch {}
    }
    foreach ($c in @("python", "python3")) {
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    return $null
}

function Test-PyVersion($exe) {
    try {
        $v = & $exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if (-not $v) { return $false }
        $parts = $v.Trim().Split(".")
        return ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 9)
    } catch { return $false }
}

# ---------------------------------------------------------------- uninstall
if ($Uninstall) {
    Write-Step "Uninstalling ZDM"
    $cmd = Join-Path $App "zdm.cmd"
    if (Test-Path $cmd) { & $cmd stop }
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*zdm.py*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    $pl = Join-Path $env:APPDATA "ZDM"
    Remove-Item -Recurse -Force $App -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force $StartMenu -ErrorAction SilentlyContinue
    Remove-Item -Force (Join-Path $Startup "ZDM Service.lnk") -ErrorAction SilentlyContinue
    Remove-Item -Force (Join-Path $Startup "ZDM Service.vbs") -ErrorAction SilentlyContinue
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -like "*$App*") {
        $new = ($userPath.Split(";") | Where-Object { $_ -and $_ -ne $App }) -join ";"
        [Environment]::SetEnvironmentVariable("Path", $new, "User")
    }
    Write-Host "ZDM removed. Downloads and settings under the install folder are gone," -ForegroundColor Yellow
    Write-Host "but your downloaded files stay where they are." -ForegroundColor Yellow
    exit 0
}

# ---------------------------------------------------------------- python
Write-Step "Checking Python"
$py = Find-Python
if (-not $py -or -not (Test-PyVersion $py)) {
    Write-Host "Python 3.9+ not found. Trying winget..." -ForegroundColor Yellow
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
        $py = Find-Python
    }
}
if (-not $py) {
    Write-Host "Could not find Python. Install it from https://www.python.org/downloads/" -ForegroundColor Red
    Write-Host "(tick 'Add python.exe to PATH'), then run this script again." -ForegroundColor Red
    exit 1
}
Write-Host "  python: $py"

# ---------------------------------------------------------------- files
Write-Step "Installing to $App"
New-Item -ItemType Directory -Force -Path $App | Out-Null
foreach ($item in @("zdm", "web", "extension")) {
    $dst = Join-Path $App $item
    if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
    Copy-Item -Recurse -Force (Join-Path $Root $item) $dst
}
# 随包二进制（yt-dlp / ffmpeg / deno）。干净 Windows 上没有 ffmpeg，缺了它
# B 站/YouTube 下完就是「只有画面没声音」。源码树里 bin/ 可能还没 fetch，
# 所以只在存在时复制，且不删除旧文件（升级时保留已下载的运行时）。
$binSrc = Join-Path $Root "bin"
if (Test-Path $binSrc) {
    $binDst = Join-Path $App "bin"
    New-Item -ItemType Directory -Force -Path $binDst | Out-Null
    Copy-Item -Recurse -Force (Join-Path $binSrc "*") $binDst
} else {
    Write-Host "  no bundled bin/ found - yt-dlp/ffmpeg must be on PATH" -ForegroundColor Yellow
}
# bin/ is gitignored, so a source checkout has no binaries at all. Fetch the
# pinned deno runtime (small) - without it yt-dlp cannot solve YouTube's JS
# challenge, and the same fetcher carries N_m3u8DL-RE for HLS/DASH.
# ffmpeg/yt-dlp cannot be fetched here cheaply; just say so.
# 两颗各自补齐：老版本升级上来的机器 bin/ 里已经有 deno，以前的「deno 在就
# 什么都不做」会让流媒体引擎永远补不上，设置里选了它却一直用 ffmpeg。
foreach ($need in @(
    @{ Tool = "deno"; Exe = "deno.exe"; Why = "required for YouTube" },
    @{ Tool = "re"; Exe = "N_m3u8DL-RE.exe"; Why = "required for HLS/DASH (m3u8/mpd)" }
)) {
    if (Test-Path (Join-Path $App "bin\$($need.Exe)")) { continue }
    # fetch_bin.py 要到下面才复制进安装目录，这里必须先看源码树里那份 ——
    # 以前只找 $App 里的，等于这一步从来没执行过，缺的二进制一直没补。
    $fetcher = Join-Path $Root "fetch_bin.py"
    if (-not (Test-Path $fetcher)) { $fetcher = Join-Path $App "fetch_bin.py" }
    if (-not (Test-Path $fetcher)) {
        Write-Host "  no fetch_bin.py - cannot fetch $($need.Exe)" -ForegroundColor Yellow
        continue
    }
    Write-Step "Fetching $($need.Exe) ($($need.Why))"
    try {
        & $py $fetcher --dest (Join-Path $App "bin") --os windows `
            --arch $env:PROCESSOR_ARCHITECTURE --tool $need.Tool | Out-Null
    } catch {
        Write-Host "  $($need.Exe) download failed - $($need.Why) may be unavailable" -ForegroundColor Yellow
    }
    if (-not (Test-Path (Join-Path $App "bin\$($need.Exe)"))) {
        Write-Host "  missing bundled $($need.Exe) - $($need.Why) 会退化" -ForegroundColor Yellow
    }
}
foreach ($t in @("yt-dlp.exe", "ffmpeg.exe")) {
    if (-not (Test-Path (Join-Path $App "bin\$t"))) {
        Write-Host "  missing bundled $t - will fall back to PATH" -ForegroundColor Yellow
    }
}
foreach ($f in @("zdm.py", "gui.py", "theme.py", "README.md", "install.ps1", "install.bat", "fetch_bin.py")) {
    Copy-Item -Force (Join-Path $Root $f) (Join-Path $App $f)
}
# theme.py lives next to gui.py (not inside the zdm package); without it the
# desktop client cannot import its stylesheet and fails to start.
if (-not (Test-Path (Join-Path $App "theme.py"))) {
    throw "install.ps1: theme.py 复制失败，桌面端将无法启动"
}
Get-ChildItem -Path $App -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# ---------------------------------------------------------------- launcher
$cmdPath = Join-Path $App "zdm.cmd"
Set-Content -Path $cmdPath -Encoding ASCII -Value @"
@echo off
"$py" "%~dp0zdm.py" %*
"@

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$App*") {
    [Environment]::SetEnvironmentVariable("Path", ($userPath.TrimEnd(";") + ";$App"), "User")
    $env:Path = $env:Path + ";$App"
}
Write-Host "  command: zdm  (restart the terminal to pick it up, or use $cmdPath)"

# ---------------------------------------------------------------- shortcuts
Write-Step "Creating shortcuts"
$ws = New-Object -ComObject WScript.Shell
New-Item -ItemType Directory -Force -Path $StartMenu | Out-Null
$icon = Join-Path $App "extension\icons\icon.ico"

$lnk = $ws.CreateShortcut((Join-Path $StartMenu "ZDM 下载管理器.lnk"))
$lnk.TargetPath = $cmdPath
$lnk.Arguments = "gui"
$lnk.WorkingDirectory = $App
if (Test-Path $icon) { $lnk.IconLocation = $icon }
$lnk.Description = "ZDM 多线程下载管理器"
$lnk.Save()

$lnk2 = $ws.CreateShortcut((Join-Path $StartMenu "ZDM 网页界面.lnk"))
$lnk2.TargetPath = $cmdPath
$lnk2.Arguments = "open"
$lnk2.WorkingDirectory = $App
if (Test-Path $icon) { $lnk2.IconLocation = $icon }
$lnk2.Save()

# ---------------------------------------------------------------- autostart
if (-not $NoAutoStart) {
    $pyw = Join-Path (Split-Path $py) "pythonw.exe"
    if (-not (Test-Path $pyw)) { $pyw = $py }
    $lnk3 = $ws.CreateShortcut((Join-Path $Startup "ZDM Service.lnk"))
    $lnk3.TargetPath = $pyw
    $lnk3.Arguments = "zdm.py serve"
    $lnk3.WorkingDirectory = $App
    $lnk3.WindowStyle = 7
    $lnk3.Description = "ZDM background download service"
    $lnk3.Save()
    Write-Host "  autostart: yes (Startup folder)"
} else {
    Remove-Item -Force (Join-Path $Startup "ZDM Service.lnk") -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------- PyQt5 (optional)
Write-Step "Checking PyQt5 for the desktop app"
$hasQt = & $py -c "import PyQt5.QtWidgets" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  installing PyQt5 (needed only for the desktop window)..." -ForegroundColor Yellow
    & $py -m pip install --user PyQt5
    $hasQt = & $py -c "import PyQt5.QtWidgets" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  PyQt5 unavailable - the desktop window will fall back to the web UI." -ForegroundColor Yellow
    }
} else {
    Write-Host "  PyQt5 already installed"
}

# ---------------------------------------------------------------- start
Write-Step "Starting service"
Start-Process -FilePath $pyw -ArgumentList "zdm.py","serve" -WorkingDirectory $App -WindowStyle Hidden
Start-Sleep -Seconds 2

# Register an uninstall entry so ZDM shows up in "Apps & Features" / Control Panel.
Write-Step "Registering uninstall entry"
try {
    $unKey = "Software\Microsoft\Windows\CurrentVersion\Uninstall\ZDM"
    $reg = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($unKey)
    $reg.SetValue("DisplayName", "ZDM Download Manager")
    $reg.SetValue("UninstallString", "powershell -ExecutionPolicy Bypass -File `"$App\install.ps1`" -Uninstall")
    $reg.SetValue("QuietUninstallString", "powershell -ExecutionPolicy Bypass -File `"$App\install.ps1`" -Uninstall")
    $reg.SetValue("DisplayIcon", (Join-Path $App "extension\icons\icon.ico"))
    $reg.SetValue("InstallLocation", $App)
    $reg.SetValue("Publisher", "ZDM")
    $reg.SetValue("DisplayVersion", $Version)
    $reg.SetValue("NoModify", 1)
    $reg.SetValue("NoRepair", 1)
    $reg.Close()
    Write-Host "  uninstall entry: added"
} catch {
    Write-Host "  could not write uninstall registry key (non-fatal)" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "ZDM installed." -ForegroundColor Green
Write-Host ""
Write-Host "  Desktop app : ZDM 下载管理器  (Start menu), or  zdm gui"
Write-Host "  Web UI      : http://127.0.0.1:9000/"
Write-Host "  Extension   : chrome://extensions -> Developer mode -> Load unpacked"
Write-Host "                -> select  $App\extension"
Write-Host ""
Write-Host "  If Windows Firewall asks about python, allow access on private networks" -ForegroundColor Yellow
Write-Host "  (ZDM only listens on 127.0.0.1)." -ForegroundColor Yellow
