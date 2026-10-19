using System.ComponentModel;
using System.Windows;
using Microsoft.Win32;
using Zdm.App.Core;

namespace Zdm.App;

/// <summary>
/// Settings dialog with live theme preview. Appearance changes apply
/// immediately so the user can judge them; Cancel restores the saved mode.
/// </summary>
public partial class SettingsDialog : Window
{
    private readonly Manager _manager;
    private readonly ThemeManager _theme;
    private readonly UpdateChecker _updates = new();

    /// <summary>Mode to restore when the user cancels.</summary>
    private readonly AppThemeMode _originalMode;

    public SettingsDialog(Manager manager, ThemeManager theme)
    {
        InitializeComponent();
        _manager = manager;
        _theme = theme;
        _originalMode = theme.Mode;

        // The caption bar is DWM-drawn; re-apply on every theme change so the
        // preview below flips the dialog chrome together with the content.
        _theme.Changed += OnThemeChangedForChrome;
        TitleBarStyler.Apply(this, _theme.IsDark);

        VersionLabel.Text = $"当前版本 {AppVersion.Display}";

        DirBox.Text = manager.Config.DownloadDir;
        ConnBox.Text = manager.Config.Connections.ToString();
        MaxBox.Text = manager.Config.MaxActive.ToString();
        SpeedBox.Text = manager.Config.SpeedLimit <= 0
            ? "0"
            : (manager.Config.SpeedLimit / 1048576.0).ToString("0.#");

        // Set after the handlers are attached so loading values cannot fire a
        // preview change, then select the right radio.
        switch (theme.Mode)
        {
            case AppThemeMode.Light: ThemeLight.IsChecked = true; break;
            case AppThemeMode.Dark: ThemeDark.IsChecked = true; break;
            default: ThemeAuto.IsChecked = true; break;
        }
    }

    private void OnThemeChangedForChrome()
    {
        if (!IsLoaded) return;
        // Must run on the UI thread; ThemeManager.Changed fires from the
        // dispatcher timer in this app, but guard anyway.
        if (Dispatcher.CheckAccess())
            TitleBarStyler.Apply(this, _theme.IsDark);
        else
            Dispatcher.BeginInvoke(() => TitleBarStyler.Apply(this, _theme.IsDark));
    }

    private void OnDialogLoaded(object sender, RoutedEventArgs e)
        => TitleBarStyler.Apply(this, _theme.IsDark);

    private void OnDialogClosing(object? sender, CancelEventArgs e)
    {
        _theme.Changed -= OnThemeChangedForChrome;
        if (DialogResult != true)
            _theme.SetMode(_originalMode);
    }

    private void OnThemeChanged(object sender, RoutedEventArgs e)
    {
        var mode = ThemeAuto.IsChecked == true ? AppThemeMode.Auto
            : ThemeLight.IsChecked == true ? AppThemeMode.Light
            : AppThemeMode.Dark;
        _theme.SetMode(mode);
    }

    private async void OnCheckUpdate(object sender, RoutedEventArgs e)
    {
        UpdateLink.Content = "检查中…";
        UpdateLink.IsEnabled = false;
        try
        {
            var info = await _updates.CheckAsync();
            UpdateLink.Content = "检查更新";
            UpdateLink.IsEnabled = true;

            if (info is null)
            {
                MessageBox.Show(this, "无法连接到 GitHub，请稍后再试。", "检查更新",
                    MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }

            if (!info.IsNewer)
            {
                MessageBox.Show(this, $"已是最新版本（{AppVersion.Display}）。", "检查更新",
                    MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }

            var answer = MessageBox.Show(this,
                $"发现新版本 {info.Tag}（当前 {AppVersion.Display}）。\n\n是否打开下载页面？",
                "检查更新", MessageBoxButton.YesNo, MessageBoxImage.Information);

            if (answer == MessageBoxResult.Yes && !string.IsNullOrEmpty(info.Url))
            {
                System.Diagnostics.Process.Start(
                    new System.Diagnostics.ProcessStartInfo(info.Url) { UseShellExecute = true });
            }
        }
        finally
        {
            UpdateLink.Content = "检查更新";
            UpdateLink.IsEnabled = true;
        }
    }

    private void OnBrowseDir(object sender, RoutedEventArgs e)
    {
        var dialog = new OpenFolderDialog
        {
            Title = "选择下载目录",
            InitialDirectory = Directory.Exists(DirBox.Text) ? DirBox.Text : _manager.Config.DownloadDir,
        };
        if (dialog.ShowDialog(this) == true)
            DirBox.Text = dialog.FolderName;
    }

    private void OnSave(object sender, RoutedEventArgs e)
    {
        var dir = string.IsNullOrWhiteSpace(DirBox.Text) ? null : DirBox.Text.Trim();
        var conn = int.TryParse(ConnBox.Text, out var c) ? Math.Clamp(c, 1, 32) : 8;
        var max = int.TryParse(MaxBox.Text, out var m) ? Math.Max(1, m) : 3;
        var mb = double.TryParse(SpeedBox.Text, out var s) ? Math.Max(0, s) : 0;

        var patch = new System.Text.Json.Nodes.JsonObject
        {
            ["connections"] = conn,
            ["max_active"] = max,
            // Config stores bytes/s; the dialog shows MB/s.
            ["speed_limit"] = (long)(mb * 1048576),
        };
        if (dir is not null)
            patch["download_dir"] = dir;

        _manager.SetConfig(patch);
        DialogResult = true;
        Close();
    }

    private void OnCancel(object sender, RoutedEventArgs e)
    {
        // Undo the live preview so the window returns to the saved mode.
        _theme.SetMode(_originalMode);
        DialogResult = false;
        Close();
    }

    protected override void OnClosed(EventArgs e)
    {
        // Safety net for paths that bypass Closing (e.g. an exception).
        _theme.Changed -= OnThemeChangedForChrome;
        // Covers the title-bar X button, which bypasses OnCancel.
        if (DialogResult != true)
            _theme.SetMode(_originalMode);
        base.OnClosed(e);
    }
}