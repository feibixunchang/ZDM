using System.Diagnostics;
using System.Text.Json;

namespace Zdm.App.Core;

/// <summary>
/// One download task. Segmented when the server supports Range, otherwise a
/// single stream. Uses a RandomAccessFile so every connection can write at its
/// own offset without locking a shared file position.
/// </summary>
public sealed class Download
{
    /// <summary>
    /// Cancellation for the current run.
    ///
    /// Replaced rather than reset: a CancellationTokenSource that has been
    /// cancelled stays cancelled, and CancellationToken.TryReset only succeeds
    /// when nobody is still observing it — in practice a paused download could
    /// never be resumed because the old token was already dead by the time
    /// StartAsync ran again.
    /// </summary>
    private CancellationTokenSource _stop = new();

    /// <summary>Set by Cancel(); Finish() must not resurrect the task as
    /// Completed even if it briefly overwrote Status with Merging.</summary>
    private volatile bool _cancelRequested;

    /// <summary>
    /// Token for the current run. Read through a property so a swap by
    /// Resume() is picked up by workers on their next loop iteration.
    /// </summary>
    private CancellationToken CurrentToken => _stop.Token;

    /// <summary>单个任务的连接数上限。RunSegmented 按连接数直接开线程，
    /// 没有上限时一个请求就能开出几千条连接。和 Python 端
    /// zdm/core.py 的 MAX_CONNECTIONS、设置项区间保持同一个数字。</summary>
    public const int MaxConnections = 32;

    public string Id { get; }
    public string Url { get; set; }
    public string FinalUrl { get; set; }
    public string Directory { get; set; }
    public string Filename { get; set; }
    public int Connections { get; set; }
    public Dictionary<string, string> Headers { get; set; }
    public string? Proxy { get; set; }
    public int MaxRetries { get; set; }
    public TimeSpan Timeout { get; set; }
    public long SpeedLimit { get; set; }
    /// <summary>Who asked for this task (manual/browser/sniffer/webui/protocol).</summary>
    public string Source { get; set; } = TaskSources.Manual;
    /// <summary>"video"/"audio", or empty for an ordinary file download.</summary>
    public string Kind { get; set; } = "";
    /// <summary>
    /// The matching audio track for a DASH video stream. When set, the
    /// audio is downloaded after the video and muxed in with ffmpeg.
    /// </summary>
    public string AudioUrl { get; set; } = "";

    public long Size { get; set; }
    public TaskStatus Status { get; set; } = TaskStatus.Queued;
    /// <summary>True once the .part has been moved to TargetPath. Lets the
    /// manager delete the file of a task cancelled during merging without
    /// ever touching an unrelated pre-existing file.</summary>
    public bool FileReady { get; set; }
    public string? Error { get; set; }
    public bool SupportsRange { get; set; }
    public double CreatedAt { get; set; } = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;
    public double? CompletedAt { get; set; }
    public int Retries { get; set; }

    public Segmenter? Segments { get; set; }
    public SpeedMeter Meter { get; } = new();
    public RateLimiter Limiter { get; }
    public RateLimiter? GlobalLimiter { get; set; }

    private List<CancellationTokenSource> _workers = new();
    private readonly object _workerGate = new();

    public event Action<string, Download>? OnEvent;

    public Download(
        string id,
        string url,
        string directory,
        string? filename = null,
        int connections = 8,
        Dictionary<string, string>? headers = null,
        string? proxy = null,
        int maxRetries = 5,
        double timeoutSeconds = 30,
        long speedLimit = 0,
        string? source = null,
        string? kind = null,
        string? audioUrl = null)
    {
        Id = id;
        Url = url;
        FinalUrl = url;
        Directory = directory;
        Filename = string.IsNullOrWhiteSpace(filename) ? "download" : filename;
        Connections = Math.Clamp(connections, 1, MaxConnections);
        Headers = headers ?? new Dictionary<string, string>();
        Proxy = proxy;
        MaxRetries = Math.Max(0, maxRetries);
        Timeout = TimeSpan.FromSeconds(timeoutSeconds);
        SpeedLimit = speedLimit;
        Limiter = new RateLimiter(speedLimit);
        Source = TaskSources.Normalize(source);
        Kind = TaskKinds.Normalize(kind);
        AudioUrl = audioUrl ?? "";
    }

    public string PartPath => Path.Combine(Directory, Filename + ".part");
    public string MetaPath => PartPath + ".zdm";
    public string TargetPath => Path.Combine(Directory, Filename);

    public long Downloaded => Segments?.Downloaded() ?? Meter.Total;

    public double Progress => Size <= 0 ? 0 : Math.Round(Downloaded / (double)Size * 100, 2);

    public int? Eta
    {
        get
        {
            var sp = Meter.Speed();
            if (sp <= 0 || Size <= 0) return null;
            return (int)((Size - Downloaded) / sp);
        }
    }

    public bool IsActive => Status == TaskStatus.Downloading;

    /// <summary>Current speed in bytes/s, cached for the UI polling loop.</summary>
    public long SpeedForDisplay { get; private set; }

    /// <summary>Recompute the volatile fields the UI reads every tick.</summary>
    public void RefreshMetrics()
    {
        SpeedForDisplay = (long)Math.Round(Meter.Speed());
    }

    private void Emit(string evt)
    {
        try
        {
            OnEvent?.Invoke(evt, this);
        }
        catch
        {
            // observers must never break the download
        }
    }

    public TaskSnapshot Snapshot()
    {
        return new TaskSnapshot
        {
            Id = Id,
            Url = Url,
            FinalUrl = FinalUrl,
            Filename = Filename,
            Directory = Directory,
            Size = Size,
            Status = TaskStatusNames.ToApi(Status),
            Error = Error,
            Downloaded = Downloaded,
            Speed = (long)Math.Round(Meter.Speed()),
            Connections = Connections,
            SupportsRange = SupportsRange,
            Transport = "native",
            Headers = Headers,
            Proxy = Proxy,
            SpeedLimit = SpeedLimit,
            CreatedAt = CreatedAt,
            CompletedAt = CompletedAt,
            Eta = Eta,
            Progress = Progress,
            Chunks = Segments?.Snapshot() ?? Array.Empty<long[]>(),
            Source = TaskSources.Normalize(Source),
            Kind = TaskKinds.Normalize(Kind),
        };
    }

    // ---------------- metadata ----------------

    private object MetaPayload() => new
    {
        url = Url,
        final_url = FinalUrl,
        filename = Filename,
        size = Size,
        connections = Connections,
        supports_range = SupportsRange,
        transport = "native",
        headers = Headers,
        proxy = Proxy,
        speed_limit = SpeedLimit,
        source = Source,
        kind = Kind,
        audio_url = AudioUrl,
        chunks = Segments?.Snapshot() ?? Array.Empty<long[]>(),
    };

    private void SaveMeta()
    {
        if (Segments is null) return;
        try
        {
            var tmp = MetaPath + ".tmp";
            File.WriteAllText(tmp, JsonSerializer.Serialize(MetaPayload()));
            File.Move(tmp, MetaPath, overwrite: true);
        }
        catch
        {
            // metadata is an optimisation for resume; losing it is not fatal
        }
    }

    public void LoadMeta(JsonElement data)
    {
        Url = Text(data, "url") ?? Url;
        FinalUrl = Text(data, "final_url") ?? Url;
        Filename = Text(data, "filename") ?? Filename;
        // Numbers and flags go through the read-path helpers: state.json and
        // .zdm-meta can be hand-edited, and GetInt64()/GetBoolean() throw on a
        // bad value — the caller's catch then turns that into a task quietly
        // missing from the history. Same reason record_int() exists in Python.
        Size = Math.Max(0, RecordNumbers.Long(data, "size", Size));
        Connections = Math.Clamp(RecordNumbers.Int(data, "connections", Connections), 1, MaxConnections);
        SupportsRange = Flag(data, "supports_range");
        SpeedLimit = Math.Max(0, RecordNumbers.Long(data, "speed_limit", SpeedLimit));
        Proxy = Text(data, "proxy");
        // Metadata written before 1.12.0 has neither key: keep the defaults.
        if (data.TryGetProperty("source", out var so) && so.ValueKind == JsonValueKind.String)
            Source = TaskSources.Normalize(so.GetString());
        if (data.TryGetProperty("kind", out var kd) && kd.ValueKind == JsonValueKind.String)
            Kind = TaskKinds.Normalize(kd.GetString());
        if (data.TryGetProperty("audio_url", out var au) && au.ValueKind == JsonValueKind.String)
            AudioUrl = au.GetString() ?? "";

        if (data.TryGetProperty("headers", out var hd) && hd.ValueKind == JsonValueKind.Object)
        {
            Headers.Clear();
            foreach (var p in hd.EnumerateObject())
                if (p.Value.ValueKind == JsonValueKind.String)
                    Headers[p.Name] = p.Value.GetString() ?? "";
        }

        Limiter.Rate = SpeedLimit;

        if (data.TryGetProperty("chunks", out var ch) && ch.ValueKind == JsonValueKind.Array && Size > 0)
        {
            var list = new List<Chunk>();
            foreach (var item in ch.EnumerateArray())
            {
                if (item.ValueKind != JsonValueKind.Array) continue;
                var parts = item.EnumerateArray().ToArray();
                if (parts.Length < 3) continue;
                if (!RecordNumbers.Value(parts[0], out var start) ||
                    !RecordNumbers.Value(parts[1], out var end) ||
                    !RecordNumbers.Value(parts[2], out var pos))
                    continue;
                // A chunk outside the file length makes a connection write past
                // EOF (or report negative remaining); skip it and re-fetch that
                // range instead of trusting a corrupted resume point.
                if (start < 0 || end > Size || start >= end) continue;
                list.Add(new Chunk((long)start, (long)end, (long)Math.Min(Math.Max(pos, start), end)));
            }
            // The surviving chunks must still tile the whole file. Dropping a
            // damaged one otherwise leaves a hole that resume never refills:
            // the task reports "completed" with bytes missing from the middle.
            // Without full coverage, forget the resume point and re-segment.
            long covered = 0;
            foreach (var c in list.OrderBy(x => x.Start))
            {
                if (c.Start != covered) break;
                covered = c.End;
            }
            if (covered == Size)
                Segments = new Segmenter(Size, list);
        }
    }

    /// <summary>Optional string field: null when absent or holding another type.</summary>
    private static string? Text(JsonElement data, string name) =>
        data.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;

    private static bool Flag(JsonElement data, string name) =>
        data.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.True;

    // ---------------- lifecycle ----------------

    private async Task PrepareAsync()
    {
        System.IO.Directory.CreateDirectory(Directory);

        if (File.Exists(MetaPath))
        {
            try
            {
                using var doc = JsonDocument.Parse(await File.ReadAllTextAsync(MetaPath));
                LoadMeta(doc.RootElement.Clone());
            }
            catch
            {
                // corrupt metadata: start over
            }
        }

        if (Segments is null)
        {
            var probe = await Transport.Instance.ProbeAsync(
                FinalUrl, Headers, Timeout, Proxy, CurrentToken);

            FinalUrl = probe.FinalUrl;
            SupportsRange = probe.SupportsRange;
            Size = probe.Size;
            if (Size <= 0)
                SupportsRange = false;

            if (Filename == "download")
                Filename = Filenames.FromHeaders(probe.Headers, FinalUrl);
            Filename = Filenames.UniqueIn(Directory, Filename);
            Segments = new Segmenter(Size);
        }
        else if (Size > 0 && !File.Exists(PartPath))
        {
            // metadata survived but the partial data is gone
            Segments = new Segmenter(Size);
        }

        if (Size > 0 && SupportsRange)
            Segments!.Balance(Connections);

        // Pre-allocate so segments can be written at arbitrary offsets.
        if (Size > 0 && !File.Exists(PartPath))
        {
            var fs = new FileStream(PartPath, FileMode.Create, FileAccess.Write, FileShare.ReadWrite);
            fs.SetLength(Size);
            fs.Dispose();
        }
    }

    public async Task StartAsync()
    {
        try
        {
            await PrepareAsync();
        }
        catch (OperationCanceledException)
        {
            Status = TaskStatus.Paused;
            SaveMeta();
            Emit("paused");
            return;
        }
        catch (Exception ex)
        {
            Status = TaskStatus.Error;
            Error = ex.Message;
            Emit("error");
            return;
        }

        Status = TaskStatus.Downloading;
        // No TryReset here: the CTS is swapped for a fresh one by Resume().
        // PrepareAsync() above already consumed the token, so resetting it at
        // this point was both too late and unreliable.
        var token = CurrentToken;
        Meter.Reset();
        Emit("started");

        if (Size > 0 && SupportsRange)
            await RunSegmentedAsync(token);
        else
            await RunSingleAsync(token);

        // Worker tasks have all returned by now, so nothing else can advance
        // the byte counters: decide the outcome once, here.
        if (_stop.IsCancellationRequested)
        {
            // A pause that landed after the final byte still has to finish.
            if (Segments is not null && Segments.IsComplete() && Size > 0)
            {
                Finish();
                return;
            }
            SaveMeta();
            if (Status == TaskStatus.Downloading)
                Status = TaskStatus.Paused;
            Emit("paused");
            return;
        }

        if (Status == TaskStatus.Error)
        {
            SaveMeta();
            Emit("error");
            return;
        }

        if (Segments is not null && Segments.IsComplete())
        {
            Finish();
            return;
        }

        // Workers exited with bytes still missing: retry with backoff.
        while (Retries < MaxRetries && Status != TaskStatus.Error)
        {
            Retries++;
            await Task.Delay(TimeSpan.FromSeconds(Math.Min(1 << Retries, 8)));

            foreach (var c in Segments?.Chunks ?? Array.Empty<Chunk>())
                c.Attempts = 0;

            if (Size > 0 && SupportsRange)
                await RunSegmentedAsync(CurrentToken);
            else
                await RunSingleAsync(CurrentToken);

            if (Segments is not null && Segments.IsComplete())
            {
                Finish();
                return;
            }
            if (Status == TaskStatus.Error || _stop.IsCancellationRequested)
                break;
        }

        SaveMeta();
        if (Status != TaskStatus.Error)
        {
            Status = TaskStatus.Error;
            Error ??= "下载停滞";
        }
        Emit("error");
    }

    private void SetWorkers(List<CancellationTokenSource> workers)
    {
        lock (_workerGate)
        {
            foreach (var w in _workers) w.Dispose();
            _workers = workers;
        }
    }

    private async Task RunSegmentedAsync(CancellationToken runToken)
    {
        // Use the run token directly. Creating a private CTS here meant
        // Pause() cancelled a different token, so the workers never saw it and
        // the download kept going while the UI claimed it was paused.
        var tasks = new List<Task>();
        for (var i = 0; i < Connections; i++)
        {
            var wid = i;
            tasks.Add(Task.Run(() => WorkerAsync(wid, runToken), CancellationToken.None));
        }
        SetWorkers(new List<CancellationTokenSource>());
        await Task.WhenAll(tasks);
    }

    private async Task RunSingleAsync(CancellationToken runToken)
    {
        SetWorkers(new List<CancellationTokenSource>());
        await Task.Run(() => WorkerSingleAsync(runToken), CancellationToken.None);
    }

    private async Task WorkerAsync(int wid, CancellationToken ct)
    {
        var segments = Segments;
        if (segments is null) return;

        while (!ct.IsCancellationRequested)
        {
            var chunk = segments.GetWork(wid);
            if (chunk is null) return;

            try
            {
                if (await DownloadRangeAsync(chunk, ct))
                    segments.Complete(chunk);
                else
                {
                    // interrupted by pause/cancel: keep the real progress
                    segments.Release(chunk);
                    return;
                }
            }
            catch (OperationCanceledException)
            {
                segments.Release(chunk);
                return;
            }
            catch (Exception ex)
            {
                segments.Release(chunk);
                chunk.Attempts++;
                if (ct.IsCancellationRequested) return;
                if (chunk.Attempts > MaxRetries)
                {
                    Status = TaskStatus.Error;
                    Error = ex.Message;
                    return;
                }
                // Honour cancellation while backing off: the old unbounded
                // Task.Delay kept a worker sleeping for up to 10s after the
                // user paused, and WaitIdleAsync's 12s budget was shaved
                // razor-thin by it.
                try { await Task.Delay(Math.Min(1 << chunk.Attempts, 10) * 1000, ct); }
                catch (OperationCanceledException) { return; }
            }
        }
    }

    /// <summary>Fill one segment; true only when the whole range landed.</summary>
    private async Task<bool> DownloadRangeAsync(Chunk chunk, CancellationToken ct)
    {
        var url = string.IsNullOrEmpty(FinalUrl) ? Url : FinalUrl;
        var offset = chunk.Pos;

        await using var file = new RandomAccessFile(PartPath);

        while (offset < chunk.End && !ct.IsCancellationRequested)
        {
            await foreach (var buf in Transport.Instance.StreamAsync(
                               url, offset, chunk.End, Headers, Timeout, Proxy, ct))
            {
                if (ct.IsCancellationRequested)
                    return false;

                // The chunk may have been split while this response was in
                // flight, so never write past the current boundary.
                var room = chunk.End - offset;
                if (room <= 0)
                    break;
                var slice = buf.Length > room
                    ? buf.AsMemory(0, (int)room)
                    : buf.AsMemory();

                GlobalLimiter?.Consume(slice.Length);
                Limiter.Consume(slice.Length);

                var written = await file.WriteAtAsync(slice, offset, ct);
                offset += written;
                chunk.Pos = Math.Min(offset, chunk.End);
                Meter.Add(written);
            }
        }

        return chunk.Pos >= chunk.End;
    }

    /// <summary>Streamed download for servers without Range support.</summary>
    private async Task WorkerSingleAsync(CancellationToken ct)
    {
        var url = string.IsNullOrEmpty(FinalUrl) ? Url : FinalUrl;
        var offset = 0L;
        var expected = Size;

        try
        {
            await using (var fs = new FileStream(
                             PartPath, FileMode.Create, FileAccess.Write,
                             FileShare.ReadWrite | FileShare.Delete))
            {
                await foreach (var buf in Transport.Instance.StreamAsync(
                                   url, 0, null, Headers, Timeout, Proxy, ct))
                {
                    if (ct.IsCancellationRequested)
                    {
                        Size = offset;
                        Segments = new Segmenter(offset, new List<Chunk> { new(0, offset, offset) });
                        return;
                    }
                    GlobalLimiter?.Consume(buf.Length);
                    Limiter.Consume(buf.Length);
                    await fs.WriteAsync(buf, ct);
                    offset += buf.Length;
                    Meter.Add(buf.Length);
                }
            }

            if (expected > 0 && offset != expected)
                throw new HttpFailureException($"响应不完整：{offset}/{expected} 字节");
            if (offset == 0)
                throw new HttpFailureException("响应内容为空");

            Size = offset;
            Segments = new Segmenter(offset, new List<Chunk> { new(0, offset, offset) });
        }
        catch (OperationCanceledException)
        {
            // paused mid-stream
        }
        catch (Exception ex)
        {
            if (!ct.IsCancellationRequested)
            {
                Status = TaskStatus.Error;
                Error = ex.Message;
            }
        }
    }

    private void Finish()
    {
        // Cancel/restart wins over everything. A pause that landed after the
        // final byte is different: StartAsync calls Finish() on purpose so the
        // completed data still becomes a real file, so do NOT test _stop here.
        if (_cancelRequested || Status == TaskStatus.Canceled)
        {
            Status = TaskStatus.Canceled;
            return;
        }
        Status = TaskStatus.Merging;
        Emit("progress");

        if (Size > 0 && File.Exists(PartPath))
        {
            var actual = new FileInfo(PartPath).Length;
            if (actual != Size)
            {
                Status = TaskStatus.Error;
                Error = $"文件大小不符：实际 {actual}，应为 {Size}";
                Emit("error");
                return;
            }
        }

        try
        {
            File.Move(PartPath, TargetPath, overwrite: true);
            FileReady = true;
        }
        catch (Exception ex)
        {
            Status = TaskStatus.Error;
            Error = $"无法保存文件：{ex.Message}";
            Emit("error");
            return;
        }

        try
        {
            if (File.Exists(MetaPath)) File.Delete(MetaPath);
        }
        catch
        {
            // best effort
        }

        // Cancelled after the move: do not start a mux that will be killed.
        if (_cancelRequested || Status == TaskStatus.Canceled)
        {
            Status = TaskStatus.Canceled;
            return;
        }

        // A DASH video track is silent until the audio track is muxed in.
        if (!string.IsNullOrWhiteSpace(AudioUrl) && !MuxAudio())
            Error = "音频轨合并失败（缺少 ffmpeg 或音频下载失败），视频已保存但无声音";

        if (_cancelRequested || Status == TaskStatus.Canceled)
        {
            Status = TaskStatus.Canceled;
            return;
        }
        Status = TaskStatus.Completed;
        CompletedAt = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;
        Emit("completed");
    }

    /// <summary>
    /// Download the matching audio track and mux it into the video with ffmpeg.
    /// On failure the video file is left untouched (silent but playable).
    /// </summary>
    private bool MuxAudio()
    {
        if (_cancelRequested || Status == TaskStatus.Canceled) return false;
        var ffmpeg = FindFfmpeg();
        if (ffmpeg is null) return false;
        if (!File.Exists(TargetPath)) return false;

        var tmpDir = Path.Combine(Path.GetTempPath(), "zdmmux-" + Guid.NewGuid().ToString("N")[..8]);
        var audioPath = Path.Combine(tmpDir, "audio.bin");
        var outPath = TargetPath + ".merged.mp4";
        try
        {
            System.IO.Directory.CreateDirectory(tmpDir);
            using (var client = new HttpClient())
            {
                if (Headers.Count > 0)
                    foreach (var kv in Headers)
                        client.DefaultRequestHeaders.TryAddWithoutValidation(kv.Key, kv.Value);
                // B站 / YouTube 的 CDN 没有 Referer 直接 403。扩展推送时
                // 任务头里一定带着页面地址，走不到下面这段；这是给
                // zdm:// 协议、桌面端粘贴这类不带 Referer 的入口兜底。
                if (!Headers.Any(k => string.Equals(k.Key, "Referer", StringComparison.OrdinalIgnoreCase)))
                {
                    var audioHost = (Uri.TryCreate(AudioUrl, UriKind.Absolute, out var au) ? au.Host : "") ?? "";
                    if (audioHost.Contains("bilivideo") || audioHost.Contains("bilibili"))
                        client.DefaultRequestHeaders.TryAddWithoutValidation("Referer", "https://www.bilibili.com/");
                    else if (audioHost.Contains("googlevideo") || audioHost.Contains("youtube"))
                        client.DefaultRequestHeaders.TryAddWithoutValidation("Referer", "https://www.youtube.com/");
                }
                client.Timeout = Timeout;
                using var resp = client.GetAsync(AudioUrl).GetAwaiter().GetResult();
                if (!resp.IsSuccessStatusCode) return false;
                using var fs = File.Create(audioPath);
                resp.Content.CopyToAsync(fs).GetAwaiter().GetResult();
            }

            // Cancelled while the audio track was downloading: do not start
            // an ffmpeg that would be killed on its first poll.
            if (_cancelRequested || Status == TaskStatus.Canceled) return false;

            var psi = new ProcessStartInfo(ffmpeg)
            {
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            foreach (var a in new[] { "-y", "-loglevel", "error", "-i", TargetPath, "-i", audioPath,
                                      "-c", "copy", "-map", "0:v:0", "-map", "1:a:0", outPath })
                psi.ArgumentList.Add(a);
            using (var proc = Process.Start(psi))
            {
                if (proc is null) return false;
                // Poll instead of one long WaitForExit: 取消/重下要能立刻
                // 杀掉 ffmpeg，否则任务会卡在「合并中」直到它自己跑完。
                var deadline = DateTime.UtcNow.AddSeconds(600);
                while (!proc.HasExited && DateTime.UtcNow < deadline)
                {
                    // 只看显式取消：停在最后几个字节的暂停仍要走完收尾，
                    // 否则音频轨会被误杀、任务变成「已完成但无声」。
                    if (_cancelRequested || Status == TaskStatus.Canceled)
                    {
                        try { proc.Kill(entireProcessTree: true); } catch { }
                        return false;
                    }
                    proc.WaitForExit(200);
                }
                if (!proc.HasExited)
                {
                    try { proc.Kill(entireProcessTree: true); } catch { }
                    return false;
                }
                if (proc.ExitCode != 0 || !File.Exists(outPath)) return false;
            }
            File.Move(outPath, TargetPath, overwrite: true);
            return true;
        }
        catch
        {
            return false;
        }
        finally
        {
            try { if (System.IO.Directory.Exists(tmpDir)) System.IO.Directory.Delete(tmpDir, true); } catch { }
        }
    }

    /// <summary>Locate ffmpeg on PATH or next to the app.</summary>
    private static string? FindFfmpeg()
    {
        var exe = OperatingSystem.IsWindows() ? "ffmpeg.exe" : "ffmpeg";
        var pathVar = Environment.GetEnvironmentVariable("PATH") ?? "";
        foreach (var dir in pathVar.Split(Path.PathSeparator))
        {
            if (string.IsNullOrWhiteSpace(dir)) continue;
            var candidate = Path.Combine(dir, exe);
            if (File.Exists(candidate)) return candidate;
        }
        var local = Path.Combine(AppContext.BaseDirectory, exe);
        return File.Exists(local) ? local : null;
    }

    // ---------------- control ----------------

    public void Pause()
    {
        if (Status is TaskStatus.Downloading or TaskStatus.Queued)
        {
            _stop.Cancel();
            if (Status == TaskStatus.Downloading)
                Status = TaskStatus.Paused;
        }
    }

    public void Resume()
    {
        if (Status is TaskStatus.Paused or TaskStatus.Error or TaskStatus.Canceled)
        {
            Error = null;
            // A cancelled CTS can never be revived, so swap in a fresh one.
            // Without this the resumed run starts already cancelled and exits
            // immediately, which is what made "继续" appear broken.
            var fresh = new CancellationTokenSource();
            var old = _stop;
            _stop = fresh;
            try { old.Dispose(); } catch { /* best effort */ }
            // A resumed run has not written TargetPath yet. Carrying the flag
            // over from a merge that was cancelled earlier would let a later
            // cancel delete a file this run never produced.
            FileReady = false;
            _cancelRequested = false;
            Status = TaskStatus.Queued;
        }
    }

    public void Cancel(bool removeFiles = true)
    {
        _cancelRequested = true;
        _stop.Cancel();
        Status = TaskStatus.Canceled;
        if (removeFiles)
        {
            foreach (var p in new[] { PartPath, MetaPath })
            {
                try
                {
                    if (File.Exists(p)) File.Delete(p);
                }
                catch
                {
                    // best effort
                }
            }
        }
        Emit("canceled");
    }
}

/// <summary>Random-access writer so parallel segments never fight over a file position.</summary>
internal sealed class RandomAccessFile : IAsyncDisposable
{
    private readonly FileStream _stream;

    public RandomAccessFile(string path)
    {
        _stream = new FileStream(
            path,
            FileMode.OpenOrCreate,
            FileAccess.ReadWrite,
            // FileShare.Delete is required for cancel/delete to work while
            // workers still hold the file: without it DeleteFile fails with a
            // sharing violation and the .part is left behind on disk.
            FileShare.ReadWrite | FileShare.Delete,
            bufferSize: 1,
            FileOptions.Asynchronous | FileOptions.RandomAccess);
    }

    public async Task<int> WriteAtAsync(ReadOnlyMemory<byte> data, long offset, CancellationToken ct)
    {
        ct.ThrowIfCancellationRequested();
        // FileStream exposes positional writes through the base Stream API.
        // _stream is private to this wrapper, so seeking here is safe even
        // though several segments share the same underlying file.
        _stream.Seek(offset, SeekOrigin.Begin);
        await _stream.WriteAsync(data, ct).ConfigureAwait(false);
        return data.Length;
    }

    public ValueTask DisposeAsync()
    {
        _stream.Flush();
        _stream.Dispose();
        return ValueTask.CompletedTask;
    }
}