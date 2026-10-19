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

    private static void Log(string tag, Exception ex) => Info($"{tag}: {ex}");

    /// <summary>
    /// Plain informational line. The log is the only way to diagnose a launch
    /// that the user never sees — a browser handover happens before the window
    /// exists, so there is nothing else to inspect.
    /// </summary>
    private static void Info(string message)
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(LogPath)!);
            File.AppendAllText(LogPath, $"[{DateTime.Now:HH:mm:ss}] {message}\n");
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

        // One line per process start, with the arguments: without it a log full
        // of "listening on 9000" cannot be told apart between the UI instance,
        // a handover and a native-messaging host.
        Info($"start pid={Environment.ProcessId} args=[{string.Join(' ', args.Skip(1))}]");

        // --register is what the installer runs: it writes the zdm:// scheme,
        // the native-messaging host manifest and any stale autostart path, then
        // quits without ever creating a window. Doing it from inside the app
        // keeps a single implementation of the paths and ids.
        if (args.Contains("--register"))
        {
            ProtocolRegistrar.Register();
            NativeMessagingRegistrar.Register();
            AutoStart.RepairIfStale();
            Environment.Exit(0);
            return;
        }

        // A browser launched us as its native-messaging host: read the request,
        // hand it to the real UI process through the shell, answer, and leave.
        // This process is disposable — Chrome kills it as soon as the extension
        // closes the port, so it must never be the one doing the downloading.
        if (NativeHost.LooksLikeHost(args))
        {
            Info($"host mode: {NativeHost.StdinType()} chain={NativeHost.AncestorChain()}");
            var raw = NativeHost.ReadRawMessage();
            var request = raw is null
                ? null
                : ExternalRequest.FromJson(raw) ?? ExternalRequest.Parse(raw);

            // No download inside the message (the user just pressed "启动 ZDM")
            // still opens the window; the bare scheme carries no query.
            var target = request?.ToUri() ?? "zdm://open";
            Info($"native-host: {(raw ?? "<no message>")} -> {target}");
            var launched = NativeHost.LaunchViaShell(target);
            Info($"native-host: launch={launched}");
            NativeHost.Reply(new { ok = launched, url = request?.Url });
            Environment.Exit(0);
            return;
        }

        // A zdm:// handover from the browser, or a bare URL on the command line.
        var handover = ExternalRequest.FromArgs(args);

        // Diagnostics and autostart must never be silently swallowed by the
        // single-instance guard, so they are exempt from it.
        var headless = args.Contains("--selftest") || args.Contains("--uitest");

        if (!headless)
        {
            // Registered on every launch: the exe path differs between a
            // portable run and an installed copy, and a stale target is
            // invisible to the user until a handover mysteriously fails.
            ProtocolRegistrar.Register();
            NativeMessagingRegistrar.Register();
            AutoStart.RepairIfStale();

            _instance = new SingleInstance();
            if (!_instance.IsPrimaryInstance)
            {
                // Another copy owns the session. Ask it to show its window and
                // take the download with it, then quit.
                var signalled = false;
                for (var i = 0; i < 3 && !signalled; i++)
                {
                    if (i > 0) Thread.Sleep(400);
                    signalled = _instance.SignalPrimary(handover?.ToUri());
                }

                Info($"second instance: forwarded={signalled}");

                _instance.Dispose();
                _instance = null;

                if (!signalled)
                {
                    // The owning process is alive but not answering. Exiting
                    // silently would look exactly like "double-click does
                    // nothing", so say where the window went.
                    MessageBox.Show(
                        "ZDM 已经在后台运行，但没能把窗口叫出来。\n\n" +
                        "请点任务栏右下角的托盘图标（可能被折叠在 ^ 里），双击或右键选择「显示主窗口」。",
                        "ZDM 下载管理器", MessageBoxButton.OK, MessageBoxImage.Information);
                }

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
            // --autostart hides into the tray instead of popping up. A handover
            // must be visible though: the user just clicked a download and
            // needs to see what happened.
            if (!headless && (!args.Contains("--autostart") || handover is not null))
                window.Show();

            if (handover is not null)
            {
                // After Show(), so the row and the status hint are visible.
                Info($"handover: {handover.Url}");
                window.Dispatcher.BeginInvoke(() => window.HandleExternal(handover));
            }

            if (!headless)
            {
                // A second launch pokes the pipe; surface the existing window
                // and add whatever download travelled with the poke.
                _instance?.StartListener(payload =>
                {
                    void Handle()
                    {
                        var request = string.IsNullOrWhiteSpace(payload)
                            ? null
                            : ExternalRequest.Parse(payload);
                        if (request is not null) window.HandleExternal(request);
                        else window.ActivateFromAnotherInstance();
                    }

                    if (window.Dispatcher.CheckAccess())
                        Handle();
                    else
                        window.Dispatcher.BeginInvoke(Handle);
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