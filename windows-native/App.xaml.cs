using System.Windows;
using System.Windows.Threading;
using Zdm.App.Core;

namespace Zdm.App;

public partial class App : Application
{
    private static string LogPath => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        "ZDM", "zdm-native.log");

    /// <summary>Held for the process lifetime; releasing it would let a second instance start.</summary>
    private static SingleInstance? _instance;

    private static void Log(string tag, Exception ex)
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(LogPath)!);
            File.AppendAllText(LogPath, $"[{DateTime.Now:HH:mm:ss}] {tag}: {ex}\n");
        }
        catch
        {
            // logging must never throw
        }
    }

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);

        var args = Environment.GetCommandLineArgs();

        // Diagnostics and autostart must never be silently swallowed by the
        // single-instance guard, so they are exempt from it.
        var headless = args.Contains("--selftest") || args.Contains("--uitest");

        if (!headless)
        {
            _instance = new SingleInstance();
            if (!_instance.IsPrimaryInstance)
            {
                // Another copy owns the session. Ask it to show its window so
                // the user sees the app instead of a silent no-op, then quit.
                _instance.SignalPrimary();
                _instance.Dispose();
                _instance = null;
                Shutdown();
                return;
            }
        }

        // Any unhandled exception must be written down: without this the app
        // vanishes at launch with no trace, which is very hard to diagnose.
        AppDomain.CurrentDomain.UnhandledException += (_, ev) =>
        {
            if (ev.ExceptionObject is Exception ex) Log("fatal", ex);
        };
        DispatcherUnhandledException += (_, ev) =>
        {
            if (ev.Exception is Exception ex) Log("ui", ex);
        };
        System.Threading.Tasks.TaskScheduler.UnobservedTaskException += (_, ev) =>
        {
            if (ev.Exception is Exception ex) Log("task", ex);
            ev.SetObserved();
        };

        try
        {
            var window = new MainWindow();
            MainWindow = window;

            // --selftest drives the API headlessly and shuts itself down;
            // --autostart hides into the tray instead of popping up.
            if (!headless && !args.Contains("--autostart"))
                window.Show();

            if (!headless)
            {
                // A second launch pokes the pipe; surface the existing window.
                _instance?.StartListener(() =>
                {
                    void Activate() => window.ActivateFromAnotherInstance();
                    if (window.Dispatcher.CheckAccess())
                        Activate();
                    else
                        window.Dispatcher.BeginInvoke(Activate);
                });
            }
        }
        catch (Exception ex)
        {
            Log("startup", ex);
            MessageBox.Show($"启动失败：\n\n{ex.Message}", "ZDM 下载管理器",
                MessageBoxButton.OK, MessageBoxImage.Error);
            Shutdown(1);
        }
    }

    protected override void OnExit(ExitEventArgs e)
    {
        _instance?.Dispose();
        _instance = null;
        base.OnExit(e);
    }
}