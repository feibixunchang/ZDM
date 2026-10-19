using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Interop;

namespace Zdm.App;

/// <summary>
/// Paints the window frame (title bar and borders).
///
/// The non-client area is drawn by DWM, not by WPF, so setting the window
/// background has no effect on it: in dark mode the client area goes dark while
/// the title bar stays white. Two mechanisms are needed:
///
/// 1. `DwmSetWindowAttribute(DWMWA_USE_IMMERSIVE_DARK_MODE)` tells DWM to draw
///    its own chrome dark on Windows 10 1809+ / Windows 11. On older builds the
///    attribute id differs, so both are tried.
/// 2. `DwmExtendFrameIntoClientArea` with a dark background brush paints the
///    frame ourselves, which also covers builds where (1) is ignored.
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

    [DllImport("user32.dll")]
    private static extern int SetWindowLong(IntPtr hwnd, int index, int value);

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

        // Windows 11 and recent Windows 10 use id 20; older builds use 19.
        if (DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
                ref value, sizeof(int)) != 0)
        {
            DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE_OLD,
                ref value, sizeof(int));
        }

        // With layered windows the client area would otherwise punch a hole in
        // the frame, so only extend the frame for a solid window.
        var ex = GetWindowLong(hwnd, GWL_EXSTYLE);
        if ((ex & WS_EX_LAYERED) != 0)
            return;

        var margins = dark
            ? new MARGINS { cyTopHeight = 1, cxRightWidth = 1, cxLeftWidth = 1, cyBottomHeight = 1 }
            : new MARGINS { cxLeftWidth = 0, cxRightWidth = 0, cyTopHeight = 0, cyBottomHeight = 0 };

        // In dark mode the 1px extension is filled by the window background,
        // which ApplyTheme has already set to the dark surface colour.
        DwmExtendFrameIntoClientArea(hwnd, ref margins);
    }
}