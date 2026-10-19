using System.IO.Pipes;
using System.Runtime.InteropServices;

namespace Zdm.App;

/// <summary>
/// Single-instance guard.
///
/// Two mechanisms are used together because neither alone is sufficient:
/// a named Mutex decides who owns the session, and a named pipe is the channel
/// the second launch uses to ask the first one to show its window.
///
/// The Mutex alone gives "second launch exits" but then the user clicks the
/// shortcut and nothing appears, which looks broken. The pipe lets the second
/// launch surface the existing window before exiting.
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
    /// this; a secondary will send the request instead.
    /// </summary>
    public void StartListener(Action onActivate)
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
                    onActivate();
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
    /// Ask the running primary instance to show its window.
    /// Returns false when nobody is listening (e.g. the primary is still
    /// starting up and has not created the pipe yet).
    /// </summary>
    public bool SignalPrimary(int timeoutMs = 1200)
    {
        try
        {
            using var client = new NamedPipeClientStream(
                ".", _pipeName, PipeDirection.Out, PipeOptions.None);
            client.Connect(timeoutMs);
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