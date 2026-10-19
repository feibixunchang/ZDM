using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Interop;
using System.Windows.Media;

namespace Zdm.App;

/// <summary>
/// Forces the window frame (caption, border and caption text) to the exact
/// colours of the current palette so the title bar and the page read as one
/// uninterrupted surface, in both the focused and the unfocused state.
///
/// Why the obvious approaches do not work:
///
///  * WPF cannot paint the caption — DWM draws the non-client area, so the
///    window background has no effect on it.
///  * <c>DWMWA_USE_IMMERSIVE_DARK_MODE</c> only tells DWM "use dark chrome";
///    DWM then picks *its own* colour (pure black on recent Windows 11
///    builds, a slightly different grey when the window is inactive). That is
///    precisely the "black band above the page" problem.
///  * <c>DwmExtendFrameIntoClientArea</c> with a 1px margin does not help
///    either: it only extends the glass 1px, it does not repaint the caption.
///
/// On Windows 11 22H2+ (build 22621 and later) DWM accepts explicit colours
/// for these parts, which is what is used here. The immersive-dark flag is
/// still set so the min/max/close glyphs and their hover highlights render
/// light-on-dark. On older builds every colour attribute fails harmlessly and
/// the app falls back to the immersive-dark flag alone.
/// </summary>
public static class TitleBarStyler
{
    private const int DWMWA_USE_IMMERSIVE_DARK_MODE = 20;
    private const int DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19;
    private const int DWMWA_BORDER_COLOR = 34;
    private const int DWMWA_CAPTION_COLOR = 35;
    private const int DWMWA_TEXT_COLOR = 36;

    [DllImport("dwmapi.dll", PreserveSig = true)]
    private static extern int DwmSetWindowAttribute(
        IntPtr hwnd, int attr, ref int value, int size);

    /// <summary>COLORREF is 0x00BBGGRR — not RGB.</summary>
    private static int ColorRef(Color c) => c.R | (c.G << 8) | (c.B << 16);

    /// <summary>Apply the current palette's chrome colours. Safe to call repeatedly.</summary>
    public static void Apply(Window window, bool dark)
    {
        if (window is null) return;

        var hwnd = new WindowInteropHelper(window).Handle;
        if (hwnd == IntPtr.Zero) return;

        // 1. Dark chrome so the caption buttons and their glyphs stay legible.
        var flag = dark ? 1 : 0;
        if (Set(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, flag) != 0)
            Set(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE_OLD, flag);

        var p = dark ? Palettes.Dark : Palettes.Light;
        var surface = Palette.Parse(p.Surface);

        // 2. Pin the caption and the 1px window border to the page colour. The
        //    border is set to the same colour on purpose: any difference shows
        //    up as an outline around an otherwise uniform window.
        var a = Set(hwnd, DWMWA_CAPTION_COLOR, ColorRef(surface));
        var b = Set(hwnd, DWMWA_BORDER_COLOR, ColorRef(surface));

        // 3. Caption text. Without this the title turns white on a white
        //    caption in light mode.
        var c = Set(hwnd, DWMWA_TEXT_COLOR, ColorRef(Palette.Parse(p.Text)));

        // Nothing to log on a supported system: all three return S_OK. A
        // non-zero HRESULT means this Windows build predates per-element
        // chrome colours, and the immutable-caption fallback is in effect.
        if (a != 0 || b != 0 || c != 0)
            Diag($"caption={a} border={b} text={c} surface={p.Surface} dark={dark}");
    }

    private static void Diag(string message)
    {
        try
        {
            var path = System.IO.Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                "ZDM", "zdm-native.log");
            System.IO.Directory.CreateDirectory(System.IO.Path.GetDirectoryName(path)!);
            System.IO.File.AppendAllText(path,
                $"[{DateTime.Now:HH:mm:ss}] dwm {message}\n");
        }
        catch
        {
            // diagnostics must never throw
        }
    }

    private static int Set(IntPtr hwnd, int attr, int value)
        => DwmSetWindowAttribute(hwnd, attr, ref value, sizeof(int));
}
