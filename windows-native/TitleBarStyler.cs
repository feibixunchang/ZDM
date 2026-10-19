using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Interop;

namespace Zdm.App;

/// <summary>
/// Paints the window frame (title bar and borders) so it matches the client
/// area exactly.
///
/// The non-client area is drawn by DWM, not WPF, so the window background has
/// no effect on it. Merely setting DWMWA_USE_IMMERSIVE_DARK_MODE makes DWM use
/// its *own* dark colour, which is a different shade from the app background —
/// the title bar ends up visibly greyer than the content. To get one uniform
/// colour the frame is extended into the client area by 1px on each edge and
/// filled by the window background, with the DWM dark flag still enabled so the
/// caption text and min/max/close glyphs render light.
/// </summary>
public static class TitleBarStyler
{
    private const int DWMWA_USE_IMMERSIVE_DARK_MODE = 20;
    private const int DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19;

    [DllImport("dwmapi.dll", PreserveSig = true)]
    private static extern int DwmSetWindowAttribute(
        IntPtr hwnd, int attr, ref int value, int size);

    [DllImport("dwmapi.dll", PreserveSig = true)]
    private static extern int DwmExtendFrameIntoClientArea(
        IntPtr hwnd, ref MARGINS margins);

    [DllImport("user32.dll")]
    private static extern int GetWindowLong(IntPtr hwnd, int index);

    private const int GWL_EXSTYLE = -20;
    private const int WS_EX_LAYERED = 0x80000;

    [StructLayout(LayoutKind.Sequential)]
    private struct MARGINS
    {
        public int cxLeftWidth, cxRightWidth, cyTopHeight, cyBottomHeight;
    }

    /// <summary>Apply dark or light chrome. Safe to call on every theme change.</summary>
    public static void Apply(Window window, bool dark)
    {
        if (window is null) return;

        var hwnd = new WindowInteropHelper(window).Handle;
        if (hwnd == IntPtr.Zero) return;

        var value = dark ? 1 : 0;
        if (DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
                ref value, sizeof(int)) != 0)
        {
            DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE_OLD,
                ref value, sizeof(int));
        }

        // A layered window would show a hole where the frame is extended.
        var ex = GetWindowLong(hwnd, GWL_EXSTYLE);
        if ((ex & WS_EX_LAYERED) != 0) return;

        // Extending by1px on every edge lets the window background — already
        // set to the palette's surface colour — paint the caption area itself,
        // instead of DWM painting its own grey. In light mode no extension is
        // needed because the default chrome already matches.
        var margins = dark
            ? new MARGINS { cxLeftWidth = 1, cxRightWidth = 1, cyTopHeight = 1, cyBottomHeight = 1 }
            : default;

        DwmExtendFrameIntoClientArea(hwnd, ref margins);
    }
}