using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Text;

namespace Zdm.App;

/// <summary>
/// Single-instance guard.
///
/// Two mechanisms are used together because neither alone is sufficient:
/// a named Mutex decides who owns the session, and a named pipe is the channel
/// the second launch uses to hand over work.
///
/// The Mutex alone gives "second launch exits" but then the user clicks the
/// shortcut and nothing appears, which looks broken. The pipe lets the second
/// launch surface the existing window before exiting — and, when the second
/// launch was a <c>zdm://</c> handover from the browser, it carries the URI
/// along so the running instance adds the download instead of dropping it.
/// </summary>
public sealed class SingleInstance : IDisposable
{
    private readonly Mutex _mutex;
    private readonly string _pipeName;
    private CancellationTokenSource? _listenerCts;
    private Thread? _listenerThread;

    public bool IsPrimaryInstance { get; }

    public SingleInstance(string key = "ZDM.DownloadManager")
    {
        _pipeName = key + ".pipe";
        _mutex = new Mutex(initiallyOwned: true, name: @"Local\" + key, out var createdNew);
        IsPrimaryInstance = createdNew;
    }

    /// <summary>
    /// Start listening for activation requests. Only the primary instance runs
    /// this; a secondary will send the request instead. The callback receives
    /// the forwarded payload (a <c>zdm://</c> URI), or null for a plain
    /// "show your window" poke.
    /// </summary>
    public void StartListener(Action<string?> onActivate)
    {
        if (!IsPrimaryInstance) return;

        _listenerCts = new CancellationTokenSource();
        var token = _listenerCts.Token;

        _listenerThread = new Thread(() =>
        {
            while (!token.IsCancellationRequested)
            {
                try
                {
                    using var server = new NamedPipeServerStream(
                        _pipeName, PipeDirection.In, maxNumberOfServerInstances: 1,
                        PipeTransmissionMode.Byte, PipeOptions.Asynchronous);
                    server.WaitForConnectionAsync(token).GetAwaiter().GetResult();
                    onActivate(ReadPayload(server));
                }
                catch (OperationCanceledException)
                {
                    return;
                }
                catch
                {
                    // A transient failure must not kill the listener; the tray
                    // icon always remains usable.
                    Thread.Sleep(500);
                }
            }
        })
        {
            IsBackground = true,
            Name = "ZDM single-instance listener",
        };
        _listenerThread.Start();
    }

    /// <summary>
    /// Read what the secondary wrote. Bounded by a timeout: a client that
    /// connects and lingers would otherwise block the listener forever.
    /// </summary>
    private static string? ReadPayload(NamedPipeServerStream server)
    {
        try
        {
            var buffer = new byte[4096];
            var read = Task.Run(() => server.Read(buffer, 0, buffer.Length));
            if (!read.Wait(1500)) return null;
            var n = read.Result;
            return n > 0 ? Encoding.UTF8.GetString(buffer, 0, n) : null;
        }
        catch
        {
            return null;
        }
    }

    /// <summary>
    /// Ask the running primary instance to show its window, optionally handing
    /// it a download request.
    /// Returns false when nobody is listening (e.g. the primary is still
    /// starting up and has not created the pipe yet).
    /// </summary>
    public bool SignalPrimary(string? payload = null, int timeoutMs = 1200)
    {
        try
        {
            using var client = new NamedPipeClientStream(
                ".", _pipeName, PipeDirection.Out, PipeOptions.None);
            client.Connect(timeoutMs);

            if (!string.IsNullOrEmpty(payload))
            {
                var bytes = Encoding.UTF8.GetBytes(payload);
                client.Write(bytes, 0, bytes.Length);
            }

            // Flush before the using block closes the pipe: a pipe write is
            // buffered on the client side until the handle is released.
            client.Flush();
            return true;
        }
        catch
        {
            return false;
        }
    }

    public void Dispose()
    {
        _listenerCts?.Cancel();
        _listenerThread?.Join(1500);
        _listenerCts?.Dispose();
        _mutex.Dispose();
    }
}