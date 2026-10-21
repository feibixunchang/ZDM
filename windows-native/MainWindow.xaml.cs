using Microsoft.Win32;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Diagnostics;
using System.Text.Json.Nodes;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using Zdm.App.Core;
using Forms = System.Windows.Forms;

namespace Zdm.App;

public partial class MainWindow : Window
{
    private readonly Manager _manager;
    private readonly ApiServer _server;
    private readonly ThemeManager _theme = new();
    private readonly DispatcherTimer _timer = new() { Interval = TimeSpan.FromMilliseconds(500) };
    private readonly DispatcherTimer _themeTimer = new() { Interval = TimeSpan.FromSeconds(2) };
    private readonly Forms.NotifyIcon _tray = new();
    private readonly UpdateChecker _updates = new();
    private string _filter = "all";
    private bool _exiting;

    public MainWindow()
    {
        InitializeComponent();

        // The version belongs in the title bar so it is visible in task
        // previews and Alt-Tab, not buried in the status bar only.
        Title = $"ZDM 下载管理器 {AppVersion.Display}";

        _theme.Init();
        ApplyTheme();

        _manager = new Manager();
        _server = new ApiServer(_manager, _manager.Config.Port);
        _server.Start();

        // Shell-action self check: proves OpenFile / OpenInExplorer really launch.
        if (Environment.GetCommandLineArgs().Contains("--uitest"))
        {
            _ = Task.Run(() =>
            {
                UiSelfTest.Run(_manager);
                Application.Current.Shutdown();
            });
            return;
        }

        // Headless verification mode: exercise the API without showing UI.
        if (Environment.GetCommandLineArgs().Contains("--selftest"))
        {
            _ = Task.Run(async () =>
            {
                await Core.SelfTest.RunAsync(_manager.Config.Port);
                Application.Current.Shutdown();
            });
            return;
        }

        SetupTray();

        // --autostart is set by the installer's Run key: start straight into
        // the tray so the service is available without stealing focus.
        if (Environment.GetCommandLineArgs().Contains("--autostart"))
        {
            Hide();
            _tray.Visible = true;
        }

        TaskGrid.ItemsSource = Rows.Items;
        FolderLabel.Text = _manager.Config.DownloadDir;
        ConnectionBox.Text = _manager.Config.Connections.ToString();

        _timer.Tick += (_, _) => Poll();
        _timer.Start();

        // Poll the Windows theme so "auto" follows the system live.
        _theme.Changed += ApplyTheme;
        _themeTimer.Tick += (_, _) => _theme.Poll();
        _themeTimer.Start();

        var autoStart = Environment.GetCommandLineArgs().Contains("--autostart");
        Loaded += (_, _) =>
        {
            Poll();
            UpdateTabStyles();
            // ApplyTheme runs in the constructor, before the window has a usable
            // HWND, so the frame styling has to be repeated once it exists.
            TitleBarStyler.Apply(this, _theme.IsDark);
            if (autoStart) return;
            Activate();
            UrlBox.Focus();
        };

        // DWM re-evaluates the caption colours when the window gains or loses
        // activation, and on some builds it re-tints a custom caption colour
        // for the inactive state. Re-applying on every transition keeps the
        // title bar pinned to the page colour in both states.
        Activated += (_, _) => TitleBarStyler.Apply(this, _theme.IsDark);
        Deactivated += (_, _) => TitleBarStyler.Apply(this, _theme.IsDark);

        // Check for a newer release shortly after launch so the window is
        // already usable; never block startup on the network. The automatic
        // pass only surfaces a badge — a popup unprompted would be intrusive.
        if (!autoStart)
        {
            var updater = new DispatcherTimer { Interval = TimeSpan.FromSeconds(4) };
            updater.Tick += (_, _) =>
            {
                updater.Stop();
                CheckUpdatesAsync(interactive: false);
            };
            updater.Start();
        }
    }

    // ---------------- theme ----------------

    /// <summary>
    /// Push the active palette into the application resources. Every style uses
    /// DynamicResource, so replacing these entries repaints the whole tree.
    /// </summary>
    private void ApplyTheme()
    {
        var p = _theme.Current;
        var res = Application.Current.Resources;

        void Put(string key, Brush value) => res[key] = value;

        Put("AppBg", p.BWindow);
        Put("Surface", p.BSurface);
        Put("SurfaceAlt", p.BSurfaceAlt);
        Put("HoverBg", p.BHover);
        Put("PressedBg", p.BPressed);
        Put("BorderBrushSoft", p.BBorder);
        Put("BorderSoft", p.BBorderSoft);
        Put("TextPrimary", p.BText);
        Put("TextMuted", p.BTextMuted);
        Put("TextStrong", p.BTextStrong);
        Put("Accent", p.BAccent);
        Put("AccentHover", p.BAccentHover);
        Put("AccentPressed", p.BAccentPressed);
        Put("AccentSoft", p.BAccentSoft);
        Put("AccentSoftBorder", p.BAccentSoftBorder);
        Put("AccentText", p.BAccentText);
        Put("Track", p.BTrack);
        Put("Selection", p.BSelection);
        Put("SelectionText", p.BSelectionText);
        Put("StatusDownloading", p.StatusBrush("downloading"));
        Put("StatusCompleted", p.StatusBrush("completed"));
        Put("StatusPaused", p.StatusBrush("paused"));
        Put("StatusError", p.StatusBrush("error"));
        Put("StatusQueued", p.StatusBrush("queued"));
        Put("StatusMerging", p.StatusBrush("merging"));

        // The title bar is drawn by DWM and ignores the WPF background, so it
        // needs its own dark/light flag; otherwise the chrome stays white while
        // the rest of the window goes dark.
        TitleBarStyler.Apply(this, _theme.IsDark);

        // Status brushes are captured by each row object rather than resolved
        // through DynamicResource, so rebuild the visible rows to pick up the
        // new palette.
        if (TaskGrid.ItemsSource is not null)
            Poll();
    }

    // ---------------- toolbar ----------------

    private void OnUrlKeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key != Key.Enter) return;
        AddUrl();
        e.Handled = true;
    }

    /// <summary>
    /// Hide each hint as soon as the box has content. Without this the grey
    /// prompt sits on top of what the user typed.
    /// </summary>
    private void OnUrlTextChanged(object sender, TextChangedEventArgs e)
    {
        if (UrlHint is null || ConnectionBox is null) return;
        UrlHint.Visibility = string.IsNullOrEmpty(UrlBox.Text)
            ? Visibility.Visible : Visibility.Collapsed;
        ConnHint.Visibility = string.IsNullOrEmpty(ConnectionBox.Text)
            ? Visibility.Visible : Visibility.Collapsed;
    }

    private void OnAddClick(object sender, RoutedEventArgs e) => AddUrl();

    private void AddUrl()
    {
        var url = UrlBox.Text.Trim();
        if (url.Length == 0) return;

        try
        {
            var conn = int.TryParse(ConnectionBox.Text, out var c) ? c : _manager.Config.Connections;
            var task = _manager.Add(url, connections: Math.Clamp(conn, 1, 32));
            UrlBox.Clear();
            Filter = "all";
            StatusHint.Text = $"已添加：{task.Filename}";
        }
        catch (ArgumentException ex)
        {
            MessageBox.Show(this, ex.Message, "ZDM 下载管理器",
                MessageBoxButton.OK, MessageBoxImage.Warning);
        }
    }

    /// <summary>
    /// Take a download handed over from outside the app: a <c>zdm://</c> URI
    /// from the browser (which is also what wakes the app up when it is not
    /// running) or a URL on the command line. Always marshalled to the UI
    /// thread by the caller.
    /// </summary>
    public void HandleExternal(ExternalRequest request)
    {
        try
        {
            var headers = request.Headers();
            var task = _manager.Add(
                request.Url,
                directory: request.Directory,
                filename: request.Filename,
                connections: request.Connections,
                headers: headers.Count > 0 ? headers : null,
                source: request.Source,
                kind: request.Kind);

            Filter = "all";
            StatusHint.Text = request.Source == Core.TaskSources.Sniffer
                ? $"已接收嗅探到的媒体：{task.Filename}"
                : $"已接管浏览器下载：{task.Filename}";
        }
        catch (Exception ex)
        {
            // A rejected handover must still tell the user something: the
            // browser tab it came from has already closed by now.
            StatusHint.Text = $"接管失败：{ex.Message}";
        }

        ActivateFromAnotherInstance();
    }

    private async void OnPauseAll(object sender, RoutedEventArgs e)
    {
        await _manager.PauseAllAsync();
        Poll();
    }

    private async void OnResumeAll(object sender, RoutedEventArgs e)
    {
        await _manager.ResumeAllAsync();
        Poll();
    }

    private void OnClearFinished(object sender, RoutedEventArgs e)
    {
        var n = _manager.ClearFinished();
        Poll();
        StatusHint.Text = $"已清除 {n} 项";
    }

    private void OnOpenFolder(object sender, RoutedEventArgs e)
    {
        var dir = _manager.Config.DownloadDir;
        if (!Directory.Exists(dir))
        {
            MessageBox.Show(this, $"目录已不存在：\n{dir}", "ZDM 下载管理器",
                MessageBoxButton.OK, MessageBoxImage.Warning);
            return;
        }
        if (!Core.Human.OpenInExplorer(dir))
            MessageBox.Show(this, $"无法打开目录：\n{dir}", "ZDM 下载管理器",
                MessageBoxButton.OK, MessageBoxImage.Warning);
    }

    private void OnBrowseFolder(object sender, RoutedEventArgs e)
    {
        var dialog = new OpenFolderDialog
        {
            Title = "选择下载目录",
            InitialDirectory = _manager.Config.DownloadDir,
        };
        if (dialog.ShowDialog(this) != true) return;

        _manager.SetConfig(new JsonObject { ["download_dir"] = dialog.FolderName });
        FolderLabel.Text = dialog.FolderName;
        StatusHint.Text = "下载目录已更新";
    }

    private void OnOpenWebUi(object sender, RoutedEventArgs e)
    {
        if (!string.IsNullOrEmpty(_server.Url))
            Process.Start(new ProcessStartInfo(_server.Url) { UseShellExecute = true });
    }

    private void OnOpenSettings(object sender, RoutedEventArgs e)
    {
        var dialog = new SettingsDialog(_manager, _theme) { Owner = this };
        if (dialog.ShowDialog() == true)
        {
            FolderLabel.Text = _manager.Config.DownloadDir;
            ConnectionBox.Text = _manager.Config.Connections.ToString();
            StatusHint.Text = "设置已保存";
        }
        UpdateTabStyles();
    }

    private string Filter
    {
        get => _filter;
        set
        {
            _filter = value;
            Poll();
            UpdateTabStyles();
        }
    }

    private void UpdateTabStyles()
    {
        PaintTab(FilterAll, _filter == "all");
        PaintTab(FilterActive, _filter == "active");
        PaintTab(FilterPaused, _filter == "paused");
        PaintTab(FilterDone, _filter == "completed");
        PaintTab(FilterSniffed, _filter == "sniffer");
    }

    /// <summary>
    /// Filter tab click. The Tag sits on the wrapping Border (the ToggleButton
    /// inside is deliberately not hit-testable so it cannot swallow the click);
    /// a Border with Background="Transparent" still receives mouse events.
    /// </summary>
    private void OnFilterTabClick(object sender, MouseButtonEventArgs e)
    {
        if (sender is Border { Tag: string tag } && tag.Length > 0)
            Filter = tag;
    }

    private void PaintTab(Border tab, bool active)
    {
        var res = Application.Current.Resources;
        tab.Background = (Brush)res[active ? "AccentSoft" : "Window"];
        // The Border wraps a ToggleButton; set the brush on the button itself
        // because SetResourceReference only exists on FrameworkElement.
        if (tab.Child is ToggleButton btn)
            btn.SetResourceReference(ToggleButton.ForegroundProperty,
                active ? "Accent" : "TextMuted");
    }

    // ---------------- row actions ----------------

    private TaskRow? Selected => TaskGrid.SelectedItem as TaskRow;

    /// <summary>
    /// Row the context menu should act on: the one captured when the menu
    /// opened, falling back to the current selection for keyboard use.
    /// </summary>
    private TaskRow? MenuRow => _menuRow ?? Selected;

    private void OnPauseSelected(object sender, RoutedEventArgs e)
    {
        if (MenuRow is not { } row) return;
        _ = _manager.PauseAsync(row.Id);
        // Reflect immediately; the manager finishes winding down in
        // the background so the grid never feels stuck.
        Poll();
    }

    private void OnResumeSelected(object sender, RoutedEventArgs e)
    {
        if (MenuRow is not { } row) return;
        _ = _manager.ResumeAsync(row.Id);
        // Reflect immediately; the manager finishes winding down in
        // the background so the grid never feels stuck.
        Poll();
    }

    private void OnRestartSelected(object sender, RoutedEventArgs e)
    {
        if (MenuRow is not { } row) return;
        _ = _manager.RestartAsync(row.Id);
        // Reflect immediately; the manager finishes winding down in
        // the background so the grid never feels stuck.
        Poll();
    }

    private void OnDeleteSelected(object sender, RoutedEventArgs e)
    {
        if (MenuRow is not { } row) return;
        _ = _manager.RemoveAsync(row.Id);
        // Reflect immediately; the manager finishes winding down in
        // the background so the grid never feels stuck.
        Poll();
    }

    private async void OnCancelSelected(object sender, RoutedEventArgs e)
    {
        if (MenuRow is not { } row) return;
        await _manager.CancelAsync(row.Id);
        Poll();
    }

    /// <summary>Open the downloaded file itself with its default app.</summary>
    private void OnOpenFile(object sender, RoutedEventArgs e)
    {
        if (MenuRow is not { } row) return;
        var path = Path.Combine(row.Directory, row.Filename);

        if (row.Status != Core.TaskStatus.Completed)
        {
            MessageBox.Show(this, "任务尚未完成，文件还不存在。", "ZDM 下载管理器",
                MessageBoxButton.OK, MessageBoxImage.Information);
            return;
        }
        if (!File.Exists(path))
        {
            // Common for tasks restored from a previous session: the record
            // survives but the file was moved or deleted.
            MessageBox.Show(this,
                $"文件已不存在：\n{path}\n\n它可能已被移动、删除，或清理工具已将其移除。",
                "ZDM 下载管理器", MessageBoxButton.OK, MessageBoxImage.Warning);
            return;
        }

        if (!Core.Human.OpenFile(path))
            MessageBox.Show(this,
                $"无法打开此文件。系统可能没有为它设置默认程序。\n\n{path}",
                "ZDM 下载管理器", MessageBoxButton.OK, MessageBoxImage.Warning);
    }

    /// <summary>Reveal the file in Explorer, or open the folder when incomplete.</summary>
    private void OnOpenRowFolder(object sender, RoutedEventArgs e)
    {
        if (MenuRow is not { } row) return;

        var folder = row.Directory;
        var file = Path.Combine(folder, row.Filename);

        // Completed tasks reveal the exact file; otherwise just open the folder.
        var target = row.Status == Core.TaskStatus.Completed && File.Exists(file)
            ? file
            : folder;

        if (!Directory.Exists(folder))
        {
            MessageBox.Show(this, $"目录已不存在：\n{folder}", "ZDM 下载管理器",
                MessageBoxButton.OK, MessageBoxImage.Warning);
            return;
        }

        if (!Core.Human.OpenInExplorer(target))
            MessageBox.Show(this,
                $"无法打开所在位置：\n{target}", "ZDM 下载管理器",
                MessageBoxButton.OK, MessageBoxImage.Warning);
    }

    private void OnCopyUrl(object sender, RoutedEventArgs e)
    {
        if (MenuRow is not { } row) return;
        try
        {
            Clipboard.SetText(row.Url);
            StatusHint.Text = "链接已复制到剪贴板";
        }
        catch
        {
            StatusHint.Text = "复制失败";
        }
    }

    private void OnOpenSelected(object sender, RoutedEventArgs e) => OnOpenFile(sender, e);

    /// <summary>
    /// Right-click on a row selects it and points the context menu at it, so the
    /// menu always acts on the row under the cursor rather than a stale
    /// selection.
    /// </summary>
    /// <summary>
    /// Row the context menu acts on.
    ///
    /// Menu actions must NOT read DataGrid.SelectedItem: opening a ContextMenu
    /// moves focus into the popup, and the DataGrid drops its selection as a
    /// result, so every action would see null and silently do nothing —
    /// which is exactly the "menu opens but nothing happens" symptom.
    /// Double-click already worked because it resolves the row from the mouse
    /// position instead. The menu now does the same and caches it here.
    /// </summary>
    private TaskRow? _menuRow;

    /// <summary>
    /// Prepare the row context menu.
    ///
    /// ContextMenuOpening is raised *on the DataGrid*, so its OriginalSource is
    /// the grid itself — walking up looking for a DataGridRow finds nothing.
    /// The row under the cursor therefore has to be resolved by hit-testing the
    /// mouse position.
    ///
    /// Visibility and PlacementTarget are left alone: WPF opens an assigned
    /// DataGrid.ContextMenu by itself, and forcing it visible here breaks that.
    /// </summary>
    private void OnGridContextMenu(object sender, ContextMenuEventArgs e)
    {
        TaskRow? hit = RowUnderCursor();
        _menuRow = hit;

        if (hit is null)
        {
            // Clicked empty space: clear the selection so no row action applies.
            TaskGrid.SelectedItem = null;
            return;
        }

        TaskGrid.SelectedItem = hit;

        // Only "open file" needs the file to exist; everything else applies
        // regardless of state.
        foreach (var item in RowMenu.Items.OfType<MenuItem>())
        {
            if (item.Header as string == "打开文件")
                item.IsEnabled = hit.Status == Core.TaskStatus.Completed;
        }
    }

    /// <summary>The row under the current mouse position, or null.</summary>
    private TaskRow? RowUnderCursor()
    {
        // InputHitTest needs a Visual, and it only answers once the grid has
        // been hit-tested at least once — not guaranteed while the context menu
        // owns the mouse. VisualTreeHelper.HitTest works either way.
        var pos = Mouse.GetPosition(TaskGrid);
        var hit = VisualTreeHelper.HitTest(TaskGrid, pos)?.VisualHit;

        DependencyObject? node = hit;
        while (node is not null && node is not DataGridRow)
            node = VisualTreeHelper.GetParent(node);

        if (node is DataGridRow row)
            return row.Item as TaskRow;

        return TaskGrid.SelectedItem as TaskRow;
    }

    /// <summary>Double-clicking a finished task opens the file, like a file manager.</summary>
    private void OnGridDoubleClick(object sender, MouseButtonEventArgs e)
    {
        if (RowUnderCursor() is not { } row) return;
        TaskGrid.SelectedItem = row;
        _menuRow = row;
        if (row.Status == Core.TaskStatus.Completed)
            OnOpenFile(sender, new RoutedEventArgs());
    }

    private static T? Ancestor<T>(DependencyObject node) where T : DependencyObject
    {
        while (node is not null)
        {
            if (node is T hit) return hit;
            node = VisualTreeHelper.GetParent(node);
        }
        return null;
    }

    // ---------------- polling ----------------

    private void Poll()
    {
        foreach (var d in _manager.All())
            d.RefreshMetrics();

        var visible = _manager.All()
            .Where(MatchesFilter)
            .OrderByDescending(d => d.CreatedAt)
            .Select(d => new TaskRow(d))
            .ToList();

        Rows.Sync(visible);
        UpdateEmptyHint();
        UpdateStatusBar();
    }

    private void UpdateEmptyHint()
        => EmptyHint.Visibility = Rows.Items.Count == 0 ? Visibility.Visible : Visibility.Collapsed;

    private bool MatchesFilter(Download d) => _filter switch
    {
        "active" => d.Status is Core.TaskStatus.Downloading or Core.TaskStatus.Queued
                           or Core.TaskStatus.Merging,
        "paused" => d.Status == Core.TaskStatus.Paused,
        "completed" => d.Status == Core.TaskStatus.Completed,
        // The only filter that keys off provenance rather than status: media the
        // browser extension found on a page.
        "sniffer" => d.Source == Core.TaskSources.Sniffer,
        _ => true,
    };

    private void UpdateStatusBar()
    {
        var all = _manager.All();
        var active = all.Count(d => d.Status == Core.TaskStatus.Downloading);
        // Only running tasks contribute; a finished one still holds samples in
        // its meter and would inflate the total.
        var speed = all.Where(d => d.Status == Core.TaskStatus.Downloading)
            .Sum(d => d.SpeedForDisplay);

        StatusText.Text = string.Join(" · ", new[]
        {
            string.IsNullOrEmpty(_server.Url) ? $"端口 {_manager.Config.Port}（未监听）" : "已连接",
            $"进行中 {active}",
            $"总速度 {Human.Speed(speed)}",
            $"共 {all.Count} 项",
        });
    }

    /// <summary>Shown in the title bar and the status bar.</summary>
    public string VersionText => AppVersion.Display;

    // ---------------- updates ----------------

    private UpdateInfo? _pendingUpdate;
    private bool _updating;

    /// <summary>
    /// Check GitHub for a newer release. Every outcome reports back explicitly —
    /// a button that does nothing reads as broken. Failures never interrupt
    /// downloads.
    /// </summary>
    private async void CheckUpdatesAsync(bool interactive = true)
    {
        if (_updating) return; // 正在下载安装包时别去搅局
        SetUpdateButtonBusy(true);
        StatusHint.Text = "正在检查更新…";

        UpdateInfo? info;
        try
        {
            info = await _updates.CheckAsync();
        }
        catch
        {
            info = null;
        }
        finally
        {
            SetUpdateButtonBusy(false);
        }

        if (info is null)
        {
            StatusHint.Text = "检查更新失败：无法连接到 GitHub";
            UpdateButton.Visibility = Visibility.Collapsed;
            if (interactive)
                MessageBox.Show(this,
                    "无法连接到 GitHub，请检查网络后重试。",
                    "检查更新", MessageBoxButton.OK, MessageBoxImage.Warning);
            return;
        }

        if (!info.IsNewer)
        {
            StatusHint.Text = $"已是最新版本（{AppVersion.Display}）";
            UpdateButton.Visibility = Visibility.Collapsed;
            _pendingUpdate = null;
            if (interactive)
                MessageBox.Show(this,
                    $"已是最新版本（{AppVersion.Display}）。",
                    "检查更新", MessageBoxButton.OK, MessageBoxImage.Information);
            return;
        }

        _pendingUpdate = info;
        UpdateButton.Visibility = Visibility.Visible;
        // 有安装包直链就自己下自己装；没有才退回网页（只有源码包的老版本会这样）
        UpdateButton.Content = info.CanInstall
            ? $"{info.Tag} · 立即更新"
            : $"{info.Tag} · 去下载";
        StatusHint.Text = $"发现新版本 {info.Tag}";

        if (!interactive)
            return;

        var text = info.CanInstall
            ? $"发现新版本 {info.Tag}（当前 {AppVersion.Display}）。\n\n" +
              "是否现在下载并安装？\n" +
              "下载完成后 ZDM 会自动退出，安装程序静默覆盖升级，无需再点“下一步”。"
            : $"发现新版本 {info.Tag}（当前 {AppVersion.Display}）。\n\n" +
              "这个版本没有提供可直接下载的安装包，是否打开下载页面？";

        var answer = MessageBox.Show(this, text, "发现新版本",
            MessageBoxButton.YesNo, MessageBoxImage.Information);

        if (answer != MessageBoxResult.Yes) return;
        if (info.CanInstall) BeginUpdate(info);
        else OpenUrl(info.Url);
    }

    private void SetUpdateButtonBusy(bool busy)
    {
        if (UpdateButton is null) return;
        UpdateButton.IsEnabled = !busy;
        UpdateButton.Content = busy ? "检查中…" : "检查更新";
    }

    /// <summary>状态栏那颗按钮：还不知道有没有新版就先查，已知有就直接装。</summary>
    private void OnOpenUpdatePage(object sender, RoutedEventArgs e)
    {
        if (_updating) return;
        var info = _pendingUpdate;
        if (info is null)
        {
            CheckUpdatesAsync(interactive: true);
            return;
        }
        if (!info.CanInstall)
        {
            OpenUrl(info.Url);
            return;
        }
        BeginUpdate(info);
    }

    /// <summary>
    /// 下载并静默安装。装完就退出 —— Inno 要覆盖的是我们正在运行的 exe，
    /// 不退出的话 /CLOSEAPPLICATIONS 只能强行结束进程，用户会看到窗口突然消失。
    /// </summary>
    internal async void BeginUpdate(UpdateInfo info)
    {
        if (_updating) return;
        _updating = true;
        UpdateButton.IsEnabled = false;
        UpdateButton.Content = "准备下载…";
        StatusHint.Text = "正在下载安装包…";

        try
        {
            var progress = new Progress<(long Got, long Total)>(p =>
            {
                if (p.Total > 0)
                {
                    var pct = (int)Math.Clamp(p.Got * 100 / p.Total, 0, 100);
                    UpdateButton.Content = $"下载 {pct}%";
                    StatusHint.Text =
                        $"正在下载更新 {Mb(p.Got):0.0} / {Mb(p.Total):0.0} MB（{pct}%）";
                }
                else
                {
                    StatusHint.Text = $"正在下载更新 {Mb(p.Got):0.0} MB";
                }
            });

            var path = await UpdateInstaller.DownloadAsync(info, progress);

            StatusHint.Text = "下载完成，正在安装…";
            if (!UpdateInstaller.RunInstaller(path))
            {
                _updating = false;
                UpdateButton.IsEnabled = true;
                UpdateButton.Content = "重试更新";
                StatusHint.Text = "安装程序启动失败";
                var open = MessageBox.Show(this,
                    $"安装包已下载到：\n{path}\n\n自动安装没能启动，是否手动打开这个安装包？",
                    "更新", MessageBoxButton.YesNo, MessageBoxImage.Warning);
                if (open == MessageBoxResult.Yes) OpenUrl(path);
                return;
            }

            // 安装器已接手，本进程立即退出
            Application.Current.Shutdown();
        }
        catch (Exception ex)
        {
            _updating = false;
            UpdateButton.IsEnabled = true;
            UpdateButton.Content = "重试更新";
            StatusHint.Text = "更新失败";
            var go = MessageBox.Show(this,
                $"下载更新失败：{ex.Message}\n\n要不要打开下载页面手动下载？",
                "更新失败", MessageBoxButton.YesNo, MessageBoxImage.Warning);
            if (go == MessageBoxResult.Yes) OpenUrl(info.Url);
        }
    }

    private static double Mb(long bytes) => bytes / 1048576.0;

    private static void OpenUrl(string url)
    {
        if (string.IsNullOrWhiteSpace(url)) return;
        try
        {
            // 本地文件（下载好的安装包）也要能这样打开，所以不判断 http
            Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
        }
        catch
        {
            // opening a browser is best effort
        }
    }

    // ---------------- tray ----------------

    /// <summary>
    /// Tray icon and menu, created in code so the icon always comes from the
    /// exe itself.
    ///
    /// Menu handlers must receive an explicit parameterless Action. Passing a
    /// method group straight into Dispatcher.Invoke makes the compiler bind the
    /// Invoke(Delegate) overload and it throws "Parameter count mismatch" — which
    /// is what made the Exit item fail.
    /// </summary>
    private void SetupTray()
    {
        var iconPath = Path.Combine(AppContext.BaseDirectory, "Assets", "app.ico");
        if (File.Exists(iconPath))
            _tray.Icon = new System.Drawing.Icon(iconPath);

        _tray.Text = "ZDM 下载管理器";
        // Windows users expect a tray icon whenever the app runs, not only after
        // it is minimized: downloads continue in the background and the tray is
        // the way back in.
        _tray.Visible = true;
        _tray.DoubleClick += (_, _) => RunOnUi(ShowFromTray);

        var menu = new Forms.ContextMenuStrip();
        var header = new Forms.ToolStripLabel($"ZDM {AppVersion.Display}");
        header.Font = new System.Drawing.Font("Microsoft YaHei UI", 9F,
            System.Drawing.FontStyle.Bold);
        header.Enabled = false;
        menu.Items.Add(header);
        menu.Items.Add(new Forms.ToolStripSeparator());
        menu.Items.Add("显示主窗口", null, (_, _) => RunOnUi(ShowFromTray));
        menu.Items.Add(new Forms.ToolStripSeparator());
        menu.Items.Add("全部暂停", null, (_, _) => RunOnUi(PauseAllFromTray));
        menu.Items.Add("全部继续", null, (_, _) => RunOnUi(ResumeAllFromTray));
        menu.Items.Add("清除已完成", null, (_, _) => RunOnUi(ClearFinishedFromTray));
        menu.Items.Add(new Forms.ToolStripSeparator());
        menu.Items.Add("打开下载目录", null, (_, _) => RunOnUi(OpenFolderFromTray));
        menu.Items.Add("打开网页版", null, (_, _) => RunOnUi(OpenWebFromTray));
        menu.Items.Add("设置…", null, (_, _) => RunOnUi(OpenSettingsFromTray));
        menu.Items.Add("检查更新", null, (_, _) => RunOnUi(() => CheckUpdatesAsync(interactive: true)));
        menu.Items.Add(new Forms.ToolStripSeparator());
        menu.Items.Add("退出", null, (_, _) => RunOnUi(ExitApp));

        _tray.ContextMenuStrip = menu;
    }

    /// <summary>Run a parameterless action on the UI thread.</summary>
    private void RunOnUi(Action action)
    {
        if (Dispatcher.CheckAccess())
            action();
        else
            Dispatcher.BeginInvoke(action);
    }

    private void ShowFromTray()
    {
        // The tray icon stays visible for the whole session, so restoring the
        // window must not touch it.
        Show();
        WindowState = WindowState.Normal;
        Activate();
    }

    private void PauseAllFromTray()
    {
        ShowFromTray();
        _ = _manager.PauseAllAsync().ContinueWith(_ => RunOnUi(Poll));
    }

    private void ResumeAllFromTray()
    {
        ShowFromTray();
        _ = _manager.ResumeAllAsync().ContinueWith(_ => RunOnUi(Poll));
    }

    private void ClearFinishedFromTray()
    {
        _manager.ClearFinished();
        ShowFromTray();
    }

    private void OpenFolderFromTray()
    {
        ShowFromTray();
        OnOpenFolder(this, new RoutedEventArgs());
    }

    private void OpenWebFromTray()
    {
        if (!string.IsNullOrEmpty(_server.Url))
            Process.Start(new ProcessStartInfo(_server.Url) { UseShellExecute = true });
        ShowFromTray();
    }

    private void OpenSettingsFromTray()
    {
        Show();
        OnOpenSettings(this, new RoutedEventArgs());
    }

    private void ExitApp()
    {
        _exiting = true;
        Application.Current.Shutdown();
    }

    /// <summary>
    /// Called when a second launch signals us. The window may be hidden in the
    /// tray, so restore it and force it to the front: SetForegroundWindow is
    /// refused by Windows unless the calling process owns the foreground, hence
    /// the AttachThreadInput dance.
    /// </summary>
    public void ActivateFromAnotherInstance()
    {
        ShowFromTray();
        ForceForeground();
    }

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern bool IsIconic(IntPtr hWnd);

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr hWnd, IntPtr pid);

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool attach);

    [System.Runtime.InteropServices.DllImport("kernel32.dll")]
    private static extern uint GetCurrentThreadId();

    private void ForceForeground()
    {
        var hwnd = new System.Windows.Interop.WindowInteropHelper(this).Handle;
        if (hwnd == IntPtr.Zero) return;

        if (IsIconic(hwnd))
            ShowWindow(hwnd, 9); // SW_RESTORE

        var fg = GetForegroundWindow();
        var fgThread = GetWindowThreadProcessId(fg, IntPtr.Zero);
        var myThread = GetCurrentThreadId();

        AttachThreadInput(myThread, fgThread, true);
        SetForegroundWindow(hwnd);
        AttachThreadInput(myThread, fgThread, false);

        Activate();
        Topmost = true;
        Topmost = false;
    }

    private void OnMinimizeToTray(object sender, RoutedEventArgs e)
    {
        Hide();
        _tray.Visible = true;
        _tray.ShowBalloonTip(2000, "ZDM", "已最小化到托盘，右键图标可退出",
            Forms.ToolTipIcon.Info);
    }

    private void OnShowFromTray(object sender, RoutedEventArgs e) => ShowFromTray();

    private void OnExitApp(object sender, RoutedEventArgs e) => ExitApp();

    protected override void OnClosing(CancelEventArgs e)
    {
        if (!_exiting)
        {
            // Closing the window parks the app in the tray, matching the
            // previous behaviour and keeping downloads alive.
            e.Cancel = true;
            Hide();
            _tray.Visible = true;
            return;
        }

        _timer.Stop();
        _themeTimer.Stop();
        _theme.Changed -= ApplyTheme;
        _tray.Visible = false;
        _tray.Dispose();
        _server.Dispose();
        _manager.Shutdown();
        base.OnClosing(e);
    }

    /// <summary>Rows exposed to XAML.</summary>
    public ObservableCollectionShim Rows { get; } = new();
}

/// <summary>Observable list that syncs by task id so selection survives updates.</summary>
public sealed class ObservableCollectionShim
{
    public ObservableCollection<TaskRow> Items { get; } = new();

    public void Sync(List<TaskRow> next)
    {
        for (var i = Items.Count - 1; i >= 0; i--)
        {
            if (next.All(n => n.Id != Items[i].Id))
                Items.RemoveAt(i);
        }

        var index = 0;
        foreach (var row in next)
        {
            var existing = IndexOf(row.Id);
            if (existing < 0)
            {
                Items.Insert(Math.Min(index, Items.Count), row);
            }
            else
            {
                Items[existing] = row;
                if (existing != index)
                    Items.Move(existing, index);
            }
            index++;
        }
    }

    private int IndexOf(string id)
    {
        for (var i = 0; i < Items.Count; i++)
            if (Items[i].Id == id) return i;
        return -1;
    }
}