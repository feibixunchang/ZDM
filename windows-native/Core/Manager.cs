using System.Globalization;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace Zdm.App.Core;

/// <summary>
/// Persisted settings. Property names are pinned to snake_case because the web
/// UI and the browser extension read these exact keys from the REST API.
/// </summary>
public sealed class AppConfig
{
    [JsonPropertyName("download_dir")] public string DownloadDir { get; set; } = AppPaths.DefaultDownloadDir();
    [JsonPropertyName("connections")] public int Connections { get; set; } = 8;
    [JsonPropertyName("max_active")] public int MaxActive { get; set; } = 3;
    [JsonPropertyName("speed_limit")] public long SpeedLimit { get; set; }
    [JsonPropertyName("proxy")] public string Proxy { get; set; } = "";
    [JsonPropertyName("max_retries")] public int MaxRetries { get; set; } = 5;
    [JsonPropertyName("timeout")] public double Timeout { get; set; } = 30;
    [JsonPropertyName("port")] public int Port { get; set; } = 9000;
    [JsonPropertyName("auto_resume")] public bool AutoResume { get; set; } = true;
    [JsonPropertyName("intercept_browser")] public bool InterceptBrowser { get; set; }
    // The Python server exposes these two; the web UI and extension read them
    // from /api/config, so a Windows client that dropped them answered with a
    // different shape for the same endpoint.
    [JsonPropertyName("auto_show_gui")] public bool AutoShowGui { get; set; } = true;
    [JsonPropertyName("check_updates")] public bool CheckUpdates { get; set; } = true;
    [JsonPropertyName("history_limit")] public int HistoryLimit { get; set; } = 200;

    public AppConfig Clone() => (AppConfig)MemberwiseClone();
}

public static class AppPaths
{
    public static string DefaultDownloadDir()
    {
        var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        foreach (var name in new[] { "Downloads", "下载" })
        {
            var p = Path.Combine(home, name);
            if (Directory.Exists(p)) return p;
        }
        return Path.Combine(home, "Downloads");
    }
}

/// <summary>
/// Owns every task: the queue, the scheduler, persistence and the event fan-out
/// that feeds both the WPF UI and the web/extension API.
/// </summary>
public sealed class Manager
{
    private readonly object _gate = new();
    private readonly Dictionary<string, Download> _tasks = new();
    private readonly Dictionary<string, Task> _workers = new();
    private readonly List<ChannelSink> _listeners = new();
    private readonly CancellationTokenSource _shutdown = new();
    private readonly string _stateFile;

    public AppConfig Config { get; private set; }
    public RateLimiter GlobalLimiter { get; }

    public event Action? Changed;

    public Manager(string? stateFile = null)
    {
        ConfigDir = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "ZDM");
        Directory.CreateDirectory(ConfigDir);
        _stateFile = stateFile ?? Path.Combine(ConfigDir, "state.json");

        Config = LoadConfig(_stateFile);
        GlobalLimiter = new RateLimiter(Config.SpeedLimit);
        LoadState();

        _ = Task.Run(SchedulerAsync);
    }

    public string ConfigDir { get; }

    // ---------------- events ----------------

    public sealed class ChannelSink
    {
        public readonly Queue<System.Text.Json.Nodes.JsonObject> Queue = new();
        public readonly SemaphoreSlim Signal = new(0, 256);
        public bool Closed;
    }

    public ChannelSink Subscribe()
    {
        var sink = new ChannelSink();
        lock (_gate) _listeners.Add(sink);
        return sink;
    }

    public void Unsubscribe(ChannelSink sink)
    {
        lock (_gate) _listeners.Remove(sink);
        sink.Closed = true;
        sink.Signal.Release();
    }

    private void Emit(string evt, Download? d)
    {
        var payload = new JsonObject
        {
            ["event"] = evt,
            ["id"] = d?.Id,
            ["task"] = d is null ? null : JsonSerializer.SerializeToNode(d.Snapshot()),
            ["ts"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0,
        };

        lock (_gate)
        {
            foreach (var sink in _listeners)
            {
                if (sink.Closed) continue;
                if (sink.Queue.Count > 64) continue; // slow consumer: skip, snapshot will catch it up
                sink.Queue.Enqueue((System.Text.Json.Nodes.JsonObject)payload.DeepClone());
                sink.Signal.Release();
            }
        }

        if (evt is "completed" or "error" or "canceled" or "paused" or "started" or "added")
            SaveState();

        Changed?.Invoke();
    }

    // ---------------- tasks ----------------

    public Download Add(
        string url,
        string? directory = null,
        string? filename = null,
        int? connections = null,
        Dictionary<string, string>? headers = null,
        string? proxy = null,
        long? speedLimit = null,
        bool startNow = true,
        string? source = null,
        string? kind = null,
        string? audioUrl = null)
    {
        if (string.IsNullOrWhiteSpace(url) ||
            !(url.StartsWith("http://") || url.StartsWith("https://")))
            throw new ArgumentException("仅支持 http/https 链接");

        if (!string.IsNullOrWhiteSpace(filename))
            filename = Filenames.Sanitize(filename.Trim());

        var d = new Download(
            id: Guid.NewGuid().ToString("N")[..12],
            url: url,
            directory: directory ?? Config.DownloadDir,
            filename: filename,
            connections: connections ?? Config.Connections,
            headers: headers ?? new Dictionary<string, string>(),
            proxy: proxy ?? (string.IsNullOrEmpty(Config.Proxy) ? null : Config.Proxy),
            maxRetries: Config.MaxRetries,
            timeoutSeconds: Config.Timeout,
            speedLimit: speedLimit ?? 0,
            source: source,
            kind: kind,
            audioUrl: audioUrl)
        {
            Status = startNow ? TaskStatus.Queued : TaskStatus.Paused,
        };

        d.OnEvent += (evt, task) => Emit(evt, task);
        d.GlobalLimiter = GlobalLimiter;

        lock (_gate) _tasks[d.Id] = d;
        Emit("added", d);
        return d;
    }

    public Download? Get(string id)
    {
        lock (_gate) return _tasks.GetValueOrDefault(id);
    }

    public List<Download> All()
    {
        lock (_gate) return _tasks.Values.ToList();
    }

    public TaskSnapshot[] List()
    {
        var items = All();
        items.Sort((a, b) => b.CreatedAt.CompareTo(a.CreatedAt));
        return items.Select(t => t.Snapshot()).ToArray();
    }

    private async Task WaitIdleAsync(Download d, double timeoutSeconds = 12)
    {
        Task? worker;
        lock (_gate) _workers.TryGetValue(d.Id, out worker);
        if (worker is null) return;
        var done = await Task.WhenAny(worker, Task.Delay(TimeSpan.FromSeconds(timeoutSeconds)));
        if (done == worker)
        {
            try { await worker; } catch { /* task already reported its own error */ }
        }
    }

    public async Task<bool> PauseAsync(string id)
    {
        var d = Get(id);
        if (d is null) return false;

        // Download.Pause() flips the status synchronously and cancels the
        // token; the workers then wind down on their own. Broadcasting before
        // waiting keeps the UI responsive instead of blocking up to 12s on a
        // slow connection to notice the cancellation.
        d.Pause();
        Emit("paused", d);

        await WaitIdleAsync(d);
        return true;
    }

    public async Task<bool> ResumeAsync(string id)
    {
        var d = Get(id);
        if (d is null) return false;
        await WaitIdleAsync(d);
        d.Resume();
        Emit("resumed", d);
        return true;
    }

    public async Task<bool> RestartAsync(string id)
    {
        var d = Get(id);
        if (d is null) return false;
        // 合并中/已完成的任务已经写出目标文件；不先删掉它，重下会落成
        //「文件名(1).mp4」，用户会以为旧文件没删干净（与 Python 端一致）。
        var was = d.Status;
        var produced = was == TaskStatus.Completed
            || (was == TaskStatus.Merging && d.FileReady);
        d.Cancel(removeFiles: true);
        await WaitIdleAsync(d);
        produced = produced || (was == TaskStatus.Merging && d.FileReady);
        if (produced) TryDelete(d.TargetPath);
        // Cancel() leaves the CancellationTokenSource in the cancelled state.
        // Resume() swaps in a fresh one - without it the next probe is
        // cancelled immediately and the "restarted" task lands in paused
        // instead of downloading (Python's start() does _stop.clear()).
        d.Resume();
        d.Segments = null;
        d.Meter.Reset();
        d.Retries = 0;
        d.Error = null;
        d.FileReady = false;
        d.Status = TaskStatus.Queued;
        Emit("restarted", d);
        return true;
    }

    public async Task<bool> CancelAsync(string id, bool removeFiles = true)
    {
        var d = Get(id);
        if (d is null) return false;
        var was = d.Status;
        var produced = was == TaskStatus.Merging && d.FileReady;
        d.Cancel(removeFiles);
        await WaitIdleAsync(d);
        produced = produced || (was == TaskStatus.Merging && d.FileReady);
        if (removeFiles && produced) TryDelete(d.TargetPath);
        return true;
    }

    public async Task<bool> RemoveAsync(string id, bool removeFiles = true)
    {
        var d = Get(id);
        if (d is null) return false;
        var was = d.Status;
        var produced = was == TaskStatus.Completed
            || (was == TaskStatus.Merging && d.FileReady);
        if (was is TaskStatus.Downloading or TaskStatus.Queued or TaskStatus.Merging)
        {
            d.Cancel(removeFiles);
            await WaitIdleAsync(d);
            produced = produced || (was == TaskStatus.Merging && d.FileReady);
            // Cancel() tried to delete the .part while workers still had the
            // file open (FileShare without Delete), so it failed silently.
            // The workers are gone now - try again.
            if (removeFiles) DeleteTaskFiles(d, produced);
        }
        else if (removeFiles)
        {
            // A completed download owns TargetPath; deleting only the .part
            // left the finished file behind (Python fixed this in v1.11.1).
            DeleteTaskFiles(d, produced);
        }
        lock (_gate) _tasks.Remove(id);
        Emit("removed", d);
        SaveState();
        return true;
    }

    private static void TryDelete(string path)
    {
        try { if (File.Exists(path)) File.Delete(path); } catch { /* best effort */ }
    }

    private static void DeleteTaskFiles(Download d, bool includeTarget = false)
    {
        var paths = new List<string> { d.PartPath, d.MetaPath };
        if (includeTarget || d.Status == TaskStatus.Completed)
            paths.Add(d.TargetPath);
        foreach (var p in paths) TryDelete(p);
    }

    public async Task PauseAllAsync()
    {
        foreach (var d in All().Where(x => x.Status is TaskStatus.Downloading or TaskStatus.Queued))
            await PauseAsync(d.Id);
    }

    public async Task ResumeAllAsync()
    {
        foreach (var d in All().Where(x => x.Status is TaskStatus.Paused or TaskStatus.Error))
            await ResumeAsync(d.Id);
    }

    public int ClearFinished()
    {
        var n = 0;
        foreach (var d in All().Where(x =>
                     x.Status is TaskStatus.Completed or TaskStatus.Canceled or TaskStatus.Error))
        {
            lock (_gate) _tasks.Remove(d.Id);
            n++;
        }
        SaveState();
        return n;
    }

    // ---------------- scheduling ----------------

    private async Task SchedulerAsync()
    {
        while (!_shutdown.IsCancellationRequested)
        {
            try
            {
                Tick();
            }
            catch
            {
                // never let scheduling die
            }
            await Task.Delay(500);
        }
    }

    private void Tick()
    {
        List<string> finished;
        lock (_gate)
        {
            finished = _workers.Where(kv => kv.Value.IsCompleted).Select(kv => kv.Key).ToList();
            foreach (var id in finished)
                _workers.Remove(id);
        }

        // 读取路径已经夹过一遍，这里再兜一次：调度器唯一的并发闸门，
        // 越界值会让队列永久卡死（-5）或一次开几百个线程（500）。
        var maxActive = Math.Clamp(Config.MaxActive, 1, 32);
        List<Download> candidates;
        lock (_gate)
        {
            var running = _tasks.Values.Count(t => t.IsActive);
            if (running >= maxActive) return;
            candidates = _tasks.Values
                .Where(t => t.Status == TaskStatus.Queued && !_workers.ContainsKey(t.Id))
                .OrderBy(t => t.CreatedAt)
                .ToList();
            var slots = maxActive - running;

            foreach (var d in candidates.Take(slots))
            {
                d.Status = TaskStatus.Downloading;
                _workers[d.Id] = Task.Run(() => RunTaskAsync(d));
            }
        }
    }

    private async Task RunTaskAsync(Download d)
    {
        try
        {
            await d.StartAsync();
        }
        catch (Exception ex)
        {
            d.Status = TaskStatus.Error;
            d.Error = ex.Message;
            Emit("error", d);
        }
        finally
        {
            lock (_gate) _workers.Remove(d.Id);
            Changed?.Invoke();
        }
    }

    // ---------------- config & persistence ----------------

    public static AppConfig LoadConfig(string stateFile)
    {
        var cfg = new AppConfig();
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(stateFile));
            if (doc.RootElement.TryGetProperty("config", out var c))
                ApplyTo(cfg, c);
        }
        catch
        {
            // defaults are fine
        }
        return cfg;
    }

    private static void ApplyTo(AppConfig cfg, JsonElement c) => ApplyTo(cfg, c, false);

    /// <summary>
    /// strict=false（读 state.json）：一处写坏的值留着其它项继续生效；
    /// strict=true（POST /api/config）：换算失败要报给用户，而不是回一份
    /// 原样配置让人以为「改了没生效」。
    /// </summary>
    private static void ApplyTo(AppConfig cfg, JsonElement c, bool strict)
    {
        foreach (var p in c.EnumerateObject())
        {
            // 和 Python 端一致：null 表示「这一项没给」，直接跳过而不是报错。
            if (p.Value.ValueKind is JsonValueKind.Null or JsonValueKind.Undefined) continue;
            try
            {
                Assign(cfg, p.Name, p.Value);
            }
            catch (FormatException) when (!strict)
            {
                // 旧版本或手工改过的 state 文件：数字越界夹回边界（max_active
                // 存成 -5 会让调度器永远不给任务开工，存成 500 会一口气开几百
                // 个线程），其余坏值直接忽略，让其它项照常恢复。
                Heal(cfg, p.Name, p.Value);
            }
        }
    }

    /// <summary>
    /// Read-path fallback: out-of-range numbers are clamped into
    /// <see cref="ConfigRanges"/> instead of thrown away, mirroring
    /// heal_config() in the Python manager. Port is the exception — a stored
    /// port outside 1~65535 means the file is broken, so fall back to the
    /// default the desktop client probes instead of binding a port nobody
    /// can find.
    /// </summary>
    private static void Heal(AppConfig cfg, string name, JsonElement v)
    {
        if (!ConfigRanges.TryGetValue(name, out var range)) return;
        double raw;
        if (v.ValueKind == JsonValueKind.Number) raw = v.GetDouble();
        else if (v.ValueKind == JsonValueKind.String &&
                 double.TryParse(v.GetString(), NumberStyles.Float, CultureInfo.InvariantCulture, out var text))
            raw = text;
        else return;
        if (name == "port")
        {
            cfg.Port = new AppConfig().Port;
            return;
        }
        var clamped = Math.Clamp(raw, range.Lo, range.Hi);
        switch (name)
        {
            case "connections": cfg.Connections = (int)Math.Round(clamped); break;
            case "max_active": cfg.MaxActive = (int)Math.Round(clamped); break;
            case "max_retries": cfg.MaxRetries = (int)Math.Round(clamped); break;
            case "history_limit": cfg.HistoryLimit = (int)Math.Round(clamped); break;
            case "speed_limit": cfg.SpeedLimit = (long)Math.Round(clamped); break;
            case "timeout": cfg.Timeout = clamped; break;
        }
    }

    // 数值项的合法区间，和 Python 端 zdm/manager.py 的 CONFIG_RANGES 保持一致。
    private static readonly Dictionary<string, (double Lo, double Hi)> ConfigRanges = new()
    {
        ["connections"] = (1, 32),
        ["max_active"] = (1, 32),
        ["max_retries"] = (0, 50),
        ["speed_limit"] = (0, double.PositiveInfinity),
        ["port"] = (1, 65535),
        ["history_limit"] = (1, 100000),
        ["timeout"] = (1, 3600),
    };

    private static void Assign(AppConfig cfg, string name, JsonElement v)
    {
        switch (name)
        {
            case "download_dir": cfg.DownloadDir = Dir(v, name); break;
            case "proxy": cfg.Proxy = Text(v, name); break;
            case "connections": cfg.Connections = Int(v, name); break;
            case "max_active": cfg.MaxActive = Int(v, name); break;
            case "max_retries": cfg.MaxRetries = Int(v, name); break;
            case "port": cfg.Port = Int(v, name); break;
            case "history_limit": cfg.HistoryLimit = Int(v, name); break;
            case "speed_limit": cfg.SpeedLimit = Long(v, name); break;
            case "timeout": cfg.Timeout = Num(v, name); break;
            case "auto_resume": cfg.AutoResume = Flag(v, name); break;
            case "intercept_browser": cfg.InterceptBrowser = Flag(v, name); break;
            case "auto_show_gui": cfg.AutoShowGui = Flag(v, name); break;
            case "check_updates": cfg.CheckUpdates = Flag(v, name); break;
        }
    }

    private static string Text(JsonElement v, string name) =>
        v.ValueKind == JsonValueKind.String
            ? v.GetString() ?? ""
            : throw new FormatException($"{name} 需要字符串（收到 {Raw(v)}）");

    private static string Dir(JsonElement v, string name)
    {
        // 存一个空目录进去，后面每个任务的路径都是 ""/<file>，界面只会显示
        // 「保存失败」，看不出是自己把目录框删空了。
        var text = Text(v, name).Trim();
        if (text.Length == 0)
            throw new FormatException("下载目录不能为空");
        return text;
    }

    private static double Num(JsonElement v, string name)
    {
        // "16" 也算：curl / Python CLI 把数字写成字符串是常见用法，
        // 之前这类值被静默丢掉，接口却照样回 ok。
        double value;
        if (v.ValueKind == JsonValueKind.Number) value = v.GetDouble();
        else if (v.ValueKind == JsonValueKind.String &&
                 double.TryParse(v.GetString(), NumberStyles.Float, CultureInfo.InvariantCulture, out var text))
            value = text;
        else throw new FormatException($"{name} 需要数字（收到 {Raw(v)}）");
        if (ConfigRanges.TryGetValue(name, out var range) && (value < range.Lo || value > range.Hi))
            throw new FormatException($"{name} 需要在 {RangeText(range)} 之间（收到 {Raw(v)}）");
        return value;
    }

    private static string RangeText((double Lo, double Hi) range) =>
        double.IsPositiveInfinity(range.Hi) ? $">= {range.Lo}" : $"{range.Lo}~{range.Hi}";

    private static int Int(JsonElement v, string name) => (int)Math.Round(Num(v, name));

    private static long Long(JsonElement v, string name) => (long)Math.Round(Num(v, name));

    private static bool Flag(JsonElement v, string name)
    {
        if (v.ValueKind == JsonValueKind.True) return true;
        if (v.ValueKind == JsonValueKind.False) return false;
        if (v.ValueKind == JsonValueKind.Number) return v.GetDouble() != 0;
        var text = (v.ValueKind == JsonValueKind.String ? v.GetString() : Raw(v))?
            .Trim().ToLowerInvariant();
        return text switch
        {
            // bool("off") == true 是最坏的读法：开关被写成反的。
            "0" or "false" or "no" or "off" or "" => false,
            "1" or "true" or "yes" or "on" => true,
            _ => throw new FormatException($"{name} 需要 true/false（收到 {Raw(v)}）"),
        };
    }

    private static string Raw(JsonElement v)
    {
        var text = v.GetRawText();
        return text.Length > 40 ? text[..40] + "…" : text;
    }

    public AppConfig SetConfig(JsonNode patch)
    {
        lock (_gate)
        {
            var scratch = Config.Clone();
            using var doc = JsonDocument.Parse(patch.ToJsonString());
            // 换算失败会在 Config = scratch 之前抛出：不会出现前几项已生效、
            // 后一项报错的半套配置。目录也先建好，写一个没权限的路径不该让
            // 配置停在半新半旧。
            ApplyTo(scratch, doc.RootElement, true);
            if (!string.IsNullOrWhiteSpace(scratch.DownloadDir))
            {
                try
                {
                    System.IO.Directory.CreateDirectory(scratch.DownloadDir);
                }
                catch (Exception e)
                {
                    throw new FormatException($"下载目录无法创建：{scratch.DownloadDir}（{e.Message}）");
                }
            }
            Config = scratch;
        }
        GlobalLimiter.Rate = Config.SpeedLimit;
        Emit("config", null);
        SaveState();
        return Config;
    }

    /// <summary>Serializes writes: Emit() can be called from several threads
    /// and two concurrent saves used to fight over the same .tmp file.</summary>
    private readonly object _saveGate = new();

    public void SaveState()
    {
        lock (_saveGate)
        {
            try
            {
                var tasks = new JsonArray();
                var finished = new List<Download>();
                var keep = new HashSet<string>();

                lock (_gate)
                {
                    var limit = Math.Max(1, Config.HistoryLimit);
                    // 失败/取消也算历史。只裁剪 completed 会让错误任务在
                    // state.json 里无限增长，重启越来越慢（与 Python 端一致）。
                    foreach (var d in _tasks.Values
                                 .Where(x => x.Status is TaskStatus.Completed
                                     or TaskStatus.Error or TaskStatus.Canceled)
                                 .OrderByDescending(x => x.CompletedAt ?? x.CreatedAt))
                    {
                        if (keep.Count >= limit) break;
                        keep.Add(d.Id);
                        finished.Add(d);
                    }

                    foreach (var d in _tasks.Values)
                    {
                        if ((d.Status is TaskStatus.Completed or TaskStatus.Error
                                 or TaskStatus.Canceled)
                            && !keep.Contains(d.Id))
                            continue;
                        tasks.Add(JsonSerializer.SerializeToNode(d.Snapshot()));
                    }
                }

                var root = new JsonObject
                {
                    ["config"] = JsonSerializer.SerializeToNode(Config),
                    ["tasks"] = tasks,
                };

                Directory.CreateDirectory(Path.GetDirectoryName(_stateFile)!);
                var tmp = _stateFile + ".tmp";
                File.WriteAllText(tmp, root.ToJsonString());
                File.Move(tmp, _stateFile, overwrite: true);
            }
            catch
            {
                // persistence is best effort
            }
        }
    }

    private void LoadState()
    {
        if (!File.Exists(_stateFile)) return;
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(_stateFile));
            if (doc.RootElement.TryGetProperty("tasks", out var tasks) &&
                tasks.ValueKind == JsonValueKind.Array)
            {
                foreach (var t in tasks.EnumerateArray())
                    RestoreTask(t);
            }
        }
        catch
        {
            // corrupt state: start clean
        }
    }

    /// <summary>
    /// Read an optional string property. Returns null when the key is absent or
    /// holds a non-string: <c>JsonElement.GetString()</c> throws on a number,
    /// and RestoreTask runs from the constructor, so one malformed entry would
    /// otherwise stop the app from starting at all.
    /// </summary>
    private static string? Str(JsonElement t, string name) =>
        t.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String
            ? v.GetString()
            : null;

    private void RestoreTask(JsonElement t)
    {
        var id = t.TryGetProperty("id", out var i) ? i.GetString() : null;
        var url = t.TryGetProperty("url", out var u) ? u.GetString() : null;
        if (string.IsNullOrEmpty(id) || string.IsNullOrEmpty(url)) return;

        var prevStatus = t.TryGetProperty("status", out var s) ? s.GetString() : "queued";

        if (prevStatus == TaskStatusNames.Completed)
        {
            var done = new Download(id, url,
                t.TryGetProperty("directory", out var dd) ? dd.GetString() ?? Config.DownloadDir : Config.DownloadDir,
                t.TryGetProperty("filename", out var fn) ? fn.GetString() : null,
                t.TryGetProperty("connections", out var cn) ? cn.GetInt32() : 1,
                // ValueKind guard: a hand-edited state.json with "source": 5
                // would otherwise throw here and take the whole startup down.
                source: Str(t, "source"),
                kind: Str(t, "kind"));
            done.Size = t.TryGetProperty("size", out var sz) ? sz.GetInt64() : 0;
            done.Status = TaskStatus.Completed;
            done.CompletedAt = t.TryGetProperty("completed_at", out var ca) && ca.ValueKind != JsonValueKind.Null
                ? ca.GetDouble() : DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;
            done.CreatedAt = t.TryGetProperty("created_at", out var ca2) ? ca2.GetDouble() : done.CreatedAt;
            done.GlobalLimiter = GlobalLimiter;
            if (done.Size > 0)
                done.Segments = new Segmenter(done.Size, new List<Chunk> { new(0, done.Size, done.Size) });
            lock (_gate) _tasks[id] = done;
            return;
        }

        var d = new Download(
            id, url,
            t.TryGetProperty("directory", out var dd2) ? dd2.GetString() ?? Config.DownloadDir : Config.DownloadDir,
            t.TryGetProperty("filename", out var fn2) ? fn2.GetString() : null,
            t.TryGetProperty("connections", out var cn2) ? cn2.GetInt32() : Config.Connections,
            timeoutSeconds: Config.Timeout,
            speedLimit: Config.SpeedLimit,
            source: Str(t, "source"),
            kind: Str(t, "kind"),
            audioUrl: Str(t, "audio_url"));

        d.OnEvent += (evt, task) => Emit(evt, task);
        d.GlobalLimiter = GlobalLimiter;
        d.CreatedAt = t.TryGetProperty("created_at", out var c3) ? c3.GetDouble() : d.CreatedAt;
        try
        {
            d.LoadMeta(t);
        }
        catch
        {
            // ignore a damaged entry
        }

        d.Status = prevStatus switch
        {
            TaskStatusNames.Completed => TaskStatus.Completed,
            _ => Config.AutoResume ? TaskStatus.Queued : TaskStatus.Paused,
        };
        lock (_gate) _tasks[id] = d;
    }

    public void Shutdown()
    {
        _shutdown.Cancel();
        foreach (var d in All().Where(x => x.IsActive))
            d.Pause();
        SaveState();
    }
}