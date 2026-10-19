using Microsoft.Win32;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Windows.Media;

namespace Zdm.App;

/// <summary>How the effective appearance is decided.</summary>
public enum AppThemeMode
{
    /// <summary>Follow the Windows app theme, switching live.</summary>
    Auto,
    Light,
    Dark,
}

/// <summary>
/// Semantic colour set. Ported from the Python client's theme.py so both
/// clients share one palette definition and one set of status colours.
///
/// Every colour the UI uses must come from here. Hardcoded hex values in XAML
/// are what produce light fragments on a dark background, so they are banned.
/// </summary>
public sealed class Palette
{
    public required string Window { get; init; }
    public required string Surface { get; init; }
    public required string SurfaceAlt { get; init; }
    public required string Hover { get; init; }
    public required string Pressed { get; init; }
    public required string Border { get; init; }
    public required string BorderSoft { get; init; }
    public required string Text { get; init; }
    public required string TextMuted { get; init; }
    public required string TextStrong { get; init; }
    public required string Accent { get; init; }
    public required string AccentHover { get; init; }
    public required string AccentPressed { get; init; }
    public required string AccentSoft { get; init; }
    public required string AccentSoftBorder { get; init; }
    public required string AccentText { get; init; }
    public required string Track { get; init; }
    public required string StatusDownloading { get; init; }
    public required string StatusCompleted { get; init; }
    public required string StatusPaused { get; init; }
    public required string StatusError { get; init; }
    public required string StatusQueued { get; init; }
    public required string StatusMerging { get; init; }
    public required string Selection { get; init; }
    public required string SelectionText { get; init; }

    // ---- resolved brushes (created once, frozen, safe to share) ----
    private readonly System.Collections.Concurrent.ConcurrentDictionary<string, SolidColorBrush> _cache = new();

    // Explicit hex lookup keyed by the same snake_case names the Python client
    // uses. Reflection by property name was tried first and is both slower and
    // fragile: the keys are snake_case while the C# properties are PascalCase.
    //
    // Built on first use rather than in a field initialiser, because the
    // properties are assigned by the object initialiser and are not yet set
    // when fields run.
    private Dictionary<string, string>? _hex;

    private Dictionary<string, string> HexMap
    {
        get
        {
            if (_hex is not null) return _hex;
            _hex = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                ["window"] = Window,
                ["surface"] = Surface,
                ["surface_alt"] = SurfaceAlt,
                ["hover"] = Hover,
                ["pressed"] = Pressed,
                ["border"] = Border,
                ["border_soft"] = BorderSoft,
                ["text"] = Text,
                ["text_muted"] = TextMuted,
                ["text_strong"] = TextStrong,
                ["accent"] = Accent,
                ["accent_hover"] = AccentHover,
                ["accent_pressed"] = AccentPressed,
                ["accent_soft"] = AccentSoft,
                ["accent_soft_border"] = AccentSoftBorder,
                ["accent_text"] = AccentText,
                ["track"] = Track,
                ["selection"] = Selection,
                ["selection_text"] = SelectionText,
                ["status_downloading"] = StatusDownloading,
                ["status_completed"] = StatusCompleted,
                ["status_paused"] = StatusPaused,
                ["status_error"] = StatusError,
                ["status_queued"] = StatusQueued,
                ["status_merging"] = StatusMerging,
            };
            return _hex;
        }
    }

    public SolidColorBrush Brush(string key)
        => _cache.GetOrAdd(key,
            k => HexMap.TryGetValue(k, out var hex)
                ? Frozen(Parse(hex))
                : Frozen(Colors.Magenta)); // loud, so a missing key is obvious

    public SolidColorBrush BWindow => Brush("window");
    public SolidColorBrush BSurface => Brush("surface");
    public SolidColorBrush BSurfaceAlt => Brush("surface_alt");
    public SolidColorBrush BHover => Brush("hover");
    public SolidColorBrush BPressed => Brush("pressed");
    public SolidColorBrush BBorder => Brush("border");
    public SolidColorBrush BBorderSoft => Brush("border_soft");
    public SolidColorBrush BText => Brush("text");
    public SolidColorBrush BTextMuted => Brush("text_muted");
    public SolidColorBrush BTextStrong => Brush("text_strong");
    public SolidColorBrush BAccent => Brush("accent");
    public SolidColorBrush BAccentHover => Brush("accent_hover");
    public SolidColorBrush BAccentPressed => Brush("accent_pressed");
    public SolidColorBrush BAccentSoft => Brush("accent_soft");
    public SolidColorBrush BAccentSoftBorder => Brush("accent_soft_border");
    public SolidColorBrush BAccentText => Brush("accent_text");
    public SolidColorBrush BTrack => Brush("track");
    public SolidColorBrush BSelection => Brush("selection");
    public SolidColorBrush BSelectionText => Brush("selection_text");

    public SolidColorBrush StatusBrush(string status) => status switch
    {
        "downloading" => Brush("status_downloading"),
        "completed" => Brush("status_completed"),
        "paused" => Brush("status_paused"),
        "error" => Brush("status_error"),
        "merging" => Brush("status_merging"),
        _ => Brush("status_queued"),
    };

    internal static Color Parse(string hex)
    {
        var s = hex.TrimStart('#');
        if (s.Length == 6)
        {
            return Color.FromRgb(
                Convert.ToByte(s[..2], 16),
                Convert.ToByte(s[2..4], 16),
                Convert.ToByte(s[4..6], 16));
        }
        return Colors.Black;
    }

    private static SolidColorBrush Frozen(Color c)
    {
        var b = new SolidColorBrush(c);
        b.Freeze();
        return b;
    }
}

public static class Palettes
{
    /// <summary>
    /// The palette currently in effect. Rows read status colours from here so
    /// they follow a theme switch; ThemeManager keeps it in sync.
    /// </summary>
    public static Palette Current { get; internal set; } = Light;

    public static readonly Palette Light = new()
    {
        Window = "#f5f7fa",
        Surface = "#ffffff",
        SurfaceAlt = "#fafbfc",
        Hover = "#f3f4f6",
        Pressed = "#e8ecf1",
        Border = "#e3e8ef",
        BorderSoft = "#f1f3f7",
        Text = "#1f2937",
        TextMuted = "#6b7280",
        TextStrong = "#374151",
        Accent = "#2563eb",
        AccentHover = "#1d4fd8",
        AccentPressed = "#1a45bf",
        AccentSoft = "#eff4ff",
        AccentSoftBorder = "#c7d7fe",
        AccentText = "#ffffff",
        Track = "#e8ecf1",
        StatusDownloading = "#2563eb",
        StatusCompleted = "#16a34a",
        StatusPaused = "#d97706",
        StatusError = "#dc2626",
        StatusQueued = "#94a3b8",
        StatusMerging = "#7c3aed",
        Selection = "#eff4ff",
        SelectionText = "#1f2937",
    };

    public static readonly Palette Dark = new()
    {
        Window = "#16181d",
        Surface = "#1e2128",
        SurfaceAlt = "#23262e",
        Hover = "#2a2e37",
        Pressed = "#333843",
        Border = "#333944",
        BorderSoft = "#282c34",
        Text = "#e6e8ec",
        TextMuted = "#9aa3b2",
        TextStrong = "#cfd4dc",
        Accent = "#3b82f6",
        AccentHover = "#2f6fe0",
        AccentPressed = "#2559bd",
        AccentSoft = "#1e2c4d",
        AccentSoftBorder = "#2f4a7a",
        AccentText = "#ffffff",
        Track = "#31353e",
        StatusDownloading = "#60a5fa",
        StatusCompleted = "#4ade80",
        StatusPaused = "#fbbf24",
        StatusError = "#f87171",
        StatusQueued = "#94a3b8",
        StatusMerging = "#a78bfa",
        Selection = "#24334f",
        SelectionText = "#e6e8ec",
    };
}

/// <summary>
/// Resolves the effective palette, persists the user's choice, and watches the
/// Windows app theme so "auto" switches live. Mirrors the Python ThemeManager.
/// </summary>
public sealed class ThemeManager
{
    private static string ConfDir => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "ZDM");

    private static string ConfPath => Path.Combine(ConfDir, "theme.json");

    public AppThemeMode Mode { get; private set; } = AppThemeMode.Auto;

    /// <summary>Raised when the effective palette changed (mode or system flip).</summary>
    public event Action? Changed;

    public bool IsDark { get; private set; }

    public Palette Current => IsDark ? Palettes.Dark : Palettes.Light;

    public void Init()
    {
        Mode = LoadMode();
        IsDark = Resolve();
        Publish();
    }

    private static AppThemeMode LoadMode()
    {
        try
        {
            var mode = JsonNode.Parse(File.ReadAllText(ConfPath))?["mode"]?.GetValue<string>();
            return mode switch
            {
                "light" => AppThemeMode.Light,
                "dark" => AppThemeMode.Dark,
                _ => AppThemeMode.Auto,
            };
        }
        catch
        {
            return AppThemeMode.Auto;
        }
    }

    private void SaveMode()
    {
        try
        {
            Directory.CreateDirectory(ConfDir);
            var mode = Mode switch
            {
                AppThemeMode.Light => "light",
                AppThemeMode.Dark => "dark",
                _ => "auto",
            };
            File.WriteAllText(ConfPath, $"{{\"mode\":\"{mode}\"}}");
        }
        catch
        {
            // persistence is best effort
        }
    }

    private bool Resolve()
    {
        if (Mode == AppThemeMode.Light) return false;
        if (Mode == AppThemeMode.Dark) return true;
        return SystemPrefersDark();
    }

    /// <summary>Publish the effective palette so row brushes pick it up.</summary>
    private void Publish() => Palettes.Current = Current;

    /// <summary>
    /// Read the Windows app theme. AppsUseLightTheme lives under
    /// HKCU\...\Themes\Personalize and is 0 when dark mode is on.
    /// </summary>
    public static bool SystemPrefersDark()
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(
                @"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize");
            if (key?.GetValue("AppsUseLightTheme") is int v)
                return v == 0;
        }
        catch
        {
            // fall through to the light default
        }
        return false;
    }

    public void SetMode(AppThemeMode mode)
    {
        if (Mode == mode) return;
        Mode = mode;
        SaveMode();
        IsDark = Resolve();
        Publish();
        Changed?.Invoke();
    }

    /// <summary>Poll the system theme; only acts when "auto" and it actually flipped.</summary>
    public void Poll()
    {
        if (Mode != AppThemeMode.Auto) return;
        var want = SystemPrefersDark();
        if (want != IsDark)
        {
            IsDark = want;
            Publish();
            Changed?.Invoke();
        }
    }
}