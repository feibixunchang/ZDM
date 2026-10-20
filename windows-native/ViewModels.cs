using System.Globalization;
using System.Windows;
using System.Windows.Data;
using System.Windows.Media;
using Zdm.App.Core;

namespace Zdm.App;

/// <summary>One row in the task grid. Rebuilt from the live task on each poll.</summary>
public sealed class TaskRow
{
    public Download Task { get; }

    public TaskRow(Download task) => Task = task;

    public string Id => Task.Id;
    public string Filename => Task.Filename;

    /// <summary>
    /// Source tag shown next to the file name, or empty when the task came from
    /// the desktop client ("manual" - the overwhelming majority, so tagging it
    /// would be pure noise). Colours come from the palette so they follow the
    /// theme instead of being baked into the XAML.
    /// </summary>
    public string SourceBadge => Task.Source switch
    {
        TaskSources.Browser => "浏览器",
        TaskSources.Sniffer => "嗅探",
        TaskSources.WebUi => "网页版",
        TaskSources.Protocol => "协议",
        _ => "",
    };

    public bool ShowSourceBadge => SourceBadge.Length > 0;

    public string SourceBadgeKey => Task.Source switch
    {
        TaskSources.Browser => "status_downloading",
        TaskSources.Sniffer => "status_merging",
        TaskSources.WebUi => "status_queued",
        TaskSources.Protocol => "status_paused",
        _ => "text_muted",
    };

    /// <summary>"视频"/"音频" for media sniffed by the extension, else empty.</summary>
    public string KindBadge => Task.Kind switch
    {
        TaskKinds.Video => "视频",
        TaskKinds.Audio => "音频",
        _ => "",
    };

    public bool ShowKindBadge => KindBadge.Length > 0;

    /// <summary>Dead records are dimmed so they do not read as actionable.</summary>
    public Brush FilenameBrush => IsMissingFile
        ? Palettes.Missing
        : Palettes.Current.BTextStrong;

    public string Url => Task.Url;

    /// <summary>
    /// Total size only. The downloaded amount used to be prefixed here
    /// ("71.2 MB / 71.2 MB"), which never fit the column and was cut to
    /// "71.2 MB / 7" — and it duplicated what the progress column already
    /// reports as a percentage and a bar.
    /// </summary>
    public string SizeText => Task.Size > 0 ? Human.Size(Task.Size) : "—";

    public string ProgressText => $"{Task.Progress:0.0}%";

    public double Progress => Task.Size > 0 ? Math.Clamp(Task.Progress / 100.0, 0, 1) : 0;

    /// <summary>
    /// Speed column. Only a running download has a speed; a paused or finished
    /// one shows an em dash rather than a misleading "0 B/s".
    /// </summary>
    public string SpeedText => Task.Status switch
    {
        TaskStatus.Downloading => Human.Speed(Task.SpeedForDisplay),
        // A finished transfer still has samples in the meter; showing that
        // stale rate would imply it is still going.
        TaskStatus.Queued or TaskStatus.Merging => "准备中",
        _ => "—",
    };

    public Core.TaskStatus Status => Task.Status;

    /// <summary>
    /// True when the task is marked completed but the file is gone. The record
    /// survives restarts while the file does not, so this has to be checked on
    /// disk rather than inferred from status — otherwise the list shows a
    /// confident "已完成" for something the user can no longer open.
    /// </summary>
    public bool IsMissingFile =>
        Task.Status == Core.TaskStatus.Completed
        && !File.Exists(Path.Combine(Task.Directory, Task.Filename));

    public string StatusText => IsMissingFile ? "文件已丢失" : TaskStatusNames.ToChinese(Task.Status);

    /// <summary>Muted for dead records so they read as inactive at a glance.</summary>
    public Brush StatusBrush => IsMissingFile
        ? Palettes.Missing
        : StatusColor(Task.Status);

    public Brush ProgressBrush => StatusBrush;

    /// <summary>Shows the missing-file hint under the file name.</summary>
    public string SubtitleText => IsMissingFile
        ? Task.Url
        : Task.Url;

    public bool ShowMissingHint => IsMissingFile;

    public string Directory => Task.Directory;
    public string Tooltip => IsMissingFile
        ? $"{Task.Filename}\n文件已不存在：\n{Path.Combine(Task.Directory, Task.Filename)}"
        : Task.Url;

    /// <summary>Remaining time, shown next to the percentage while downloading.</summary>
    public string EtaText
    {
        get
        {
            if (Task.Status != TaskStatus.Downloading) return "";
            var eta = Task.Eta;
            return eta is > 0 ? $" · 剩余 {Human.Eta(eta)}" : "";
        }
    }

    /// <summary>
    /// Status colours come from the active palette so they follow the theme.
    /// <see cref="Palette.StatusBrush"/> hands back a frozen instance, so no
    /// per-call allocation and no brush sharing problems.
    /// </summary>
    private static Brush StatusColor(Core.TaskStatus s)
    {
        var key = s switch
        {
            Core.TaskStatus.Downloading => "downloading",
            Core.TaskStatus.Completed => "completed",
            Core.TaskStatus.Paused => "paused",
            Core.TaskStatus.Error => "error",
            Core.TaskStatus.Merging => "merging",
            _ => "queued",
        };
        return Palettes.Current.StatusBrush(key);
    }
}

public sealed class RelayCommand : System.Windows.Input.ICommand
{
    private readonly Action<object?> _execute;
    private readonly Func<object?, bool>? _can;

    public RelayCommand(Action<object?> execute, Func<object?, bool>? canExecute = null)
    {
        _execute = execute;
        _can = canExecute;
    }

    public bool CanExecute(object? parameter) => _can?.Invoke(parameter) ?? true;
    public void Execute(object? parameter) => _execute(parameter);

    public event EventHandler? CanExecuteChanged
    {
        add => System.Windows.Input.CommandManager.RequerySuggested += value;
        remove => System.Windows.Input.CommandManager.RequerySuggested -= value;
    }
}

public sealed class BoolToVisibilityConverter : IValueConverter
{
    public object Convert(object? value, Type t, object? p, CultureInfo c)
        => value is true ? Visibility.Visible : Visibility.Collapsed;

    public object ConvertBack(object? value, Type t, object? p, CultureInfo c)
        => value is Visibility.Visible;
}

public sealed class StatusFilterConverter : IValueConverter
{
    public object Convert(object? value, Type t, object? p, CultureInfo c) => value?.ToString() ?? "";

    public object ConvertBack(object? value, Type t, object? p, CultureInfo c) => value ?? "";
}