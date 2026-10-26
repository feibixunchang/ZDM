using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Zdm.App.Core;

/// <summary>
/// Local HTTP service on 127.0.0.1 speaking the exact REST + SSE contract the
/// Python build exposed, so the browser extension and web UI keep working
/// unchanged.
///
/// Kestrel is used instead of HttpListener because HttpListener's HTTP.sys
/// registration does not survive single-file publishing: Start() returned
/// success but nothing actually bound to the port.
/// </summary>
public sealed class ApiServer : IDisposable
{
    /// <summary>Reported by GET /api. Kept in sync with the assembly version
    /// (it used to be a hard-coded "1.0.0" that nothing updated).</summary>
    public static string Version =>
        typeof(ApiServer).Assembly.GetName().Version?.ToString(3) ?? "0.0.0";

    private static readonly Dictionary<string, string> Mime = new(StringComparer.OrdinalIgnoreCase)
    {
        [".html"] = "text/html; charset=utf-8",
        [".js"] = "application/javascript; charset=utf-8",
        [".css"] = "text/css; charset=utf-8",
        [".svg"] = "image/svg+xml",
        [".png"] = "image/png",
        [".ico"] = "image/x-icon",
        [".json"] = "application/json; charset=utf-8",
    };

    private readonly Manager _manager;
    private readonly string _webRoot;
    private readonly int _port;
    private WebApplication? _app;

    /// <summary>Extension ids already whitelisted for native messaging. Seeded from the
    /// manifest on disk so the first requests do not rewrite the file.</summary>
    private static readonly HashSet<string> KnownExtensionIds =
        new(NativeMessagingRegistrar.AllowedOrigins(), StringComparer.Ordinal);

    /// <summary>Kestrel calls the middleware concurrently; HashSet.Add is not
    /// thread-safe and two racing first-requests could both rewrite the
    /// manifest.</summary>
    private static readonly object ExtensionGate = new();

    public string Url { get; private set; } = "";

    public ApiServer(Manager manager, int port = 9000)
    {
        _manager = manager;
        _port = port;
        _webRoot = ResolveWebRoot();
    }

    private static string ResolveWebRoot()
    {
        var dir = Path.GetDirectoryName(Environment.ProcessPath) ?? AppContext.BaseDirectory;
        return Path.Combine(dir, "web");
    }

    public void Start()
    {
        var builder = WebApplication.CreateSlimBuilder();
        builder.Logging.ClearProviders();
        builder.WebHost.ConfigureKestrel(k =>
        {
            // Loopback only: never expose the control API to the network.
            k.ListenLocalhost(_port);
            k.Limits.MaxRequestBodySize = 8 * 1024 * 1024;
        });

        var manager = _manager;
        var webRoot = _webRoot;

        var app = builder.Build();

        app.Use(async (ctx, next) =>
        {
            var headers = ctx.Response.Headers;
            headers.AccessControlAllowOrigin = "*";
            headers.AccessControlAllowMethods = "GET,POST,PUT,DELETE,OPTIONS";
            headers.AccessControlAllowHeaders = "Content-Type,X-ZDM-Token,X-ZDM-Extension";

            // Self-healing browser registration. The extension sends its own
            // id with every call; if it is not yet whitelisted in the native
            // messaging manifest (a reloaded or re-installed extension gets a
            // new id), it is added here so the next wake-up works without the
            // user touching anything.
            var extId = ctx.Request.Headers["X-ZDM-Extension"].ToString();
            string? toRegister = null;
            lock (ExtensionGate)
            {
                if (extId.Length == 32 && KnownExtensionIds.Add(extId))
                    toRegister = extId;
            }
            if (toRegister != null)
                NativeMessagingRegistrar.Register(toRegister);

            if (ctx.Request.Method == HttpMethods.Options)
            {
                ctx.Response.StatusCode = 204;
                return;
            }
            await next();
        });

        // ---------------- static web UI ----------------
        app.MapFallback(async ctx =>
        {
            var rel = ctx.Request.Path.Value?.Trim('/') ?? "";
            if (string.IsNullOrEmpty(rel))
                rel = "index.html";
            await ServeStaticAsync(ctx, webRoot, rel);
        });

        // ---------------- events (SSE) ----------------
        app.MapGet("/events", async ctx => { await SseAsync(ctx, manager); });
        // The web UI connects to /api/events (the Python server accepts both
        // spellings); without this alias the browser UI got a 404 here and
        // never received a single live update.
        app.MapGet("/api/events", async ctx => { await SseAsync(ctx, manager); });

        // ---------------- api ----------------
        app.MapGet("/api", () => Results.Json(new JsonObject
        {
            ["ok"] = true,
            ["version"] = Version,
        }));

        // ---------------- extension presence ping ----------------
        // The extension pings this every minute, unauthenticated (same as the
        // Python build). It compares `ext_version` — the version of the
        // extension folder the installer dropped on disk — with its own
        // running manifest version and reloads itself when they differ. That
        // is the whole extension-update story: an installer update replaces
        // {app}\extension, the next ping sees the new version, the extension
        // reloads from disk. No browser restart, no app-version bump needed.
        // (This route was missing entirely before: the ping fell through to
        // the static fallback, the JSON parse failed, and the auto-reload —
        // and the "extension connected" presence — never worked here.)
        app.MapGet("/api/extension/ping", () =>
        {
            string extVersion = "";
            try
            {
                var manifest = Path.Combine(AppContext.BaseDirectory, "extension", "manifest.json");
                if (File.Exists(manifest))
                {
                    using var doc = JsonDocument.Parse(File.ReadAllText(manifest));
                    if (doc.RootElement.TryGetProperty("version", out var v))
                        extVersion = v.GetString() ?? "";
                }
            }
            catch
            {
                // Broken/unreadable manifest — report the app version so the
                // extension at least sees a stable value instead of crashing.
            }
            if (string.IsNullOrEmpty(extVersion)) extVersion = Version;
            return Results.Json(new JsonObject
            {
                ["ok"] = true,
                ["version"] = extVersion,
                ["ext_version"] = extVersion,
                ["app_version"] = Version,
            });
        });

        app.MapGet("/api/state", () =>
        {
            var arr = new JsonArray(manager.List()
                .Select(t => JsonSerializer.SerializeToNode(t)).ToArray());
            return Results.Json(new JsonObject
            {
                ["ok"] = true,
                ["config"] = JsonSerializer.SerializeToNode(manager.Config),
                ["tasks"] = arr,
            });
        });

        app.MapGet("/api/tasks", () =>
        {
            var arr = new JsonArray(manager.List()
                .Select(t => JsonSerializer.SerializeToNode(t)).ToArray());
            return Results.Json(new JsonObject { ["ok"] = true, ["tasks"] = arr });
        });

        app.MapGet("/api/config", () => Results.Json(new JsonObject
        {
            ["ok"] = true,
            ["config"] = JsonSerializer.SerializeToNode(manager.Config),
        }));

        app.MapPost("/api/tasks", async Task<IResult> (HttpContext ctx) =>
        {
            var body = await ReadJsonAsync(ctx);
            var url = body?["url"]?.GetValue<string>()?.Trim();
            if (string.IsNullOrEmpty(url))
                return Fail(400, "url required");

            var headers = new Dictionary<string, string>();
            if (body?["headers"] is JsonObject h)
                foreach (var p in h)
                    // A number/bool in the headers object used to throw
                    // InvalidOperationException out of GetValue<string>(),
                    // which the ArgumentException-only catch turned into a
                    // bare 500. Skip anything that is not a string instead.
                    if (p.Value is JsonValue v && v.GetValueKind() == JsonValueKind.String)
                        headers[p.Key] = v.GetValue<string>()!;

            void Alias(string src, string dst)
            {
                var v = body?[src]?.GetValue<string>();
                if (!string.IsNullOrEmpty(v)) headers[dst] = v;
            }
            Alias("referer", "Referer");
            Alias("referrer", "Referer");
            Alias("cookie", "Cookie");
            Alias("useragent", "User-Agent");
            Alias("user_agent", "User-Agent");

            try
            {
                // Only accept a JSON *string*: GetValue<string>() throws on a
                // number or a bool, and that exception is not an
                // ArgumentException, so it would escape as a bare 500.
                string? Str(string key)
                {
                    var v = body?[key];
                    return v is JsonValue j && j.GetValueKind() == JsonValueKind.String
                        ? j.GetValue<string>()
                        : null;
                }

                var d = manager.Add(
                    url,
                    directory: body?["dir"]?.GetValue<string>() ?? body?["directory"]?.GetValue<string>(),
                    filename: body?["filename"]?.GetValue<string>() ?? body?["name"]?.GetValue<string>(),
                    connections: body?["connections"]?.GetValue<int?>(),
                    headers: headers,
                    proxy: body?["proxy"]?.GetValue<string>(),
                    speedLimit: body?["speed_limit"]?.GetValue<long?>(),
                    startNow: body?["paused"]?.GetValue<bool?>() != true,
                    // Provenance the browser extension sends with every hand-off.
                    // Download normalises anything unexpected to "manual".
                    source: Str("source"),
                    kind: Str("kind"),
                    // DASH video tracks need the matching audio to be muxed in.
                    audioUrl: Str("audio_url"));

                return Results.Json(new JsonObject
                {
                    ["ok"] = true,
                    ["id"] = d.Id,
                    ["task"] = JsonSerializer.SerializeToNode(d.Snapshot()),
                });
            }
            catch (ArgumentException ex)
            {
                return Fail(400, ex.Message);
            }
        });

        app.MapPost("/api/tasks/{id}/{action}", async Task<IResult> (HttpContext ctx, string id, string action) =>
        {
            var d = manager.Get(id);
            if (d is null)
                return Fail(404, "task not found");

            switch (action)
            {
                case "pause":
                    await manager.PauseAsync(id);
                    break;
                case "resume":
                    await manager.ResumeAsync(id);
                    break;
                case "restart":
                    await manager.RestartAsync(id);
                    break;
                case "cancel":
                {
                    var body = await ReadJsonAsync(ctx);
                    await manager.CancelAsync(id, body?["files"]?.GetValue<bool?>() ?? true);
                    break;
                }
                case "open":
                    // Parity with the Python server: a finished task opens the
                    // file itself (a .dmg starts the installer); an unfinished
                    // one has no file yet, so it reveals the directory.
                    if (d.Status == TaskStatus.Completed)
                        Human.OpenFile(d.TargetPath);
                    else
                        Human.OpenInExplorer(d.Directory);
                    break;
                case "reveal":
                    Human.OpenInExplorer(d.Status == TaskStatus.Completed ? d.TargetPath : d.Directory);
                    break;
                default:
                    return Fail(400, $"unknown action {action}");
            }

            return Results.Json(new JsonObject
            {
                ["ok"] = true,
                ["task"] = JsonSerializer.SerializeToNode(d.Snapshot()),
            });
        });

        app.MapDelete("/api/tasks/{id}", async Task<IResult> (HttpContext ctx, string id) =>
        {
            // ?files=0 keeps the partial data while dropping the task.
            var removeFiles = !(ctx.Request.Query.ContainsKey("files")
                                && ctx.Request.Query["files"] == "0");
            // Async, not GetAwaiter().GetResult(): RemoveAsync waits for the
            // workers to wind down (up to 12s), and blocking a Kestrel thread
            // per delete is how the thread pool gets exhausted.
            var ok = await manager.RemoveAsync(id, removeFiles);
            return Results.Json(new JsonObject { ["ok"] = ok });
        });

        app.MapPost("/api/action", async Task<IResult> (HttpContext ctx) =>
        {
            var body = await ReadJsonAsync(ctx);
            switch (body?["action"]?.GetValue<string>())
            {
                case "pause_all":
                    await manager.PauseAllAsync();
                    break;
                case "resume_all":
                    await manager.ResumeAllAsync();
                    break;
                case "clear_finished":
                    manager.ClearFinished();
                    break;
                case "open_folder":
                    Human.OpenInExplorer(body?["path"]?.GetValue<string>() ?? manager.Config.DownloadDir);
                    break;
                case "shutdown":
                    _ = Task.Run(async () =>
                    {
                        await Task.Delay(600);
                        await ShutdownAsync();
                    });
                    break;
                default:
                    return Fail(400, $"unknown action {body?["action"]?.GetValue<string>()}");
            }
            return Results.Json(new JsonObject { ["ok"] = true });
        });

        app.MapPost("/api/config", async Task<IResult> (HttpContext ctx) =>
        {
            var body = await ReadJsonAsync(ctx);
            if (body is null)
                return Fail(400, "no config supplied");
            var cfg = manager.SetConfig(body);
            return Results.Json(new JsonObject
            {
                ["ok"] = true,
                ["config"] = JsonSerializer.SerializeToNode(cfg),
            });
        });

        app.MapPost("/api/open", async Task<IResult> (HttpContext ctx) =>
        {
            var body = await ReadJsonAsync(ctx);
            var path = body?["path"]?.GetValue<string>() ?? manager.Config.DownloadDir;
            // mode=file opens the file itself, mode=reveal selects it in
            // Explorer - the Python server accepts the same modes.
            var mode = body?["mode"]?.GetValue<string>()?.ToLowerInvariant() ?? "dir";
            var ok = mode switch
            {
                "file" => Human.OpenFile(path),
                "reveal" => Human.OpenInExplorer(path),
                _ => Human.OpenInExplorer(path),
            };
            return Results.Json(new JsonObject { ["ok"] = ok });
        });

        try
        {
            // StartAsync, not Start: Kestrel binds asynchronously and calling
            // the blocking Start() from the UI thread deadlocks against the
            // dispatcher, which makes the whole app die at launch.
            app.StartAsync().GetAwaiter().GetResult();
        }
        catch (Exception ex)
        {
            File.AppendAllText(LogPath, $"[{DateTime.Now:HH:mm:ss}] start failed: {ex}\n");
            return;
        }

        _app = app;
        Url = $"http://127.0.0.1:{_port}/";
        File.AppendAllText(LogPath, $"[{DateTime.Now:HH:mm:ss}] listening on {_port}\n");

        _ = SelfCheckAsync();
    }

    /// <summary>
    /// Probe our own endpoint once at startup and record the result. Everything
    /// else in this app shares the process, so this is the only verification
    /// that does not depend on the caller's network stack.
    /// </summary>
    private async Task SelfCheckAsync()
    {
        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(5) };
            var body = await client.GetStringAsync(Url + "api/state");
            File.AppendAllText(LogPath,
                $"[{DateTime.Now:HH:mm:ss}] selfcheck OK, state bytes={body.Length}\n");
        }
        catch (Exception ex)
        {
            File.AppendAllText(LogPath,
                $"[{DateTime.Now:HH:mm:ss}] selfcheck FAILED: {ex.GetType().Name}: {ex.Message}\n");
        }
    }

    private static string LogPath => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        "ZDM", "zdm-native.log");

    private static IResult Fail(int code, string message)
        => Results.Json(new JsonObject { ["ok"] = false, ["error"] = message },
            statusCode: code);

    private static async Task<JsonObject?> ReadJsonAsync(HttpContext ctx)
    {
        try
        {
            using var reader = new StreamReader(ctx.Request.Body, Encoding.UTF8);
            var text = await reader.ReadToEndAsync();
            if (string.IsNullOrWhiteSpace(text)) return null;
            return JsonNode.Parse(text) as JsonObject;
        }
        catch
        {
            return null;
        }
    }

    private static async Task ServeStaticAsync(HttpContext ctx, string webRoot, string rel)
    {
        var root = Path.GetFullPath(webRoot);
        var path = Path.GetFullPath(Path.Combine(root, rel));

        // Compare against root + separator: a bare StartsWith(root) also
        // matches a sibling directory like "<webRoot>2" and would let
        // /web2/... read files outside the intended tree.
        var boundary = root.EndsWith(Path.DirectorySeparatorChar)
            ? root
            : root + Path.DirectorySeparatorChar;
        if (!path.StartsWith(boundary, StringComparison.OrdinalIgnoreCase) || !File.Exists(path))
        {
            ctx.Response.StatusCode = 404;
            await ctx.Response.WriteAsJsonAsync(new JsonObject { ["ok"] = false, ["error"] = "not found" });
            return;
        }

        var ext = Path.GetExtension(path);
        ctx.Response.ContentType = Mime.GetValueOrDefault(ext, "application/octet-stream");
        ctx.Response.Headers.CacheControl = "no-store";
        await ctx.Response.SendFileAsync(path);
    }

    private static async Task SseAsync(HttpContext ctx, Manager manager)
    {
        ctx.Response.ContentType = "text/event-stream; charset=utf-8";
        ctx.Response.Headers.CacheControl = "no-cache";
        ctx.Response.Headers["X-Accel-Buffering"] = "no";

        var sink = manager.Subscribe();
        var lastPing = DateTimeOffset.UtcNow;

        try
        {
            while (!ctx.RequestAborted.IsCancellationRequested)
            {
                if (await sink.Signal.WaitAsync(TimeSpan.FromMilliseconds(500), ctx.RequestAborted))
                {
                    while (sink.Queue.TryDequeue(out var payload))
                    {
                        await ctx.Response.WriteAsync($"event: task\ndata: {payload.ToJsonString()}\n\n",
                            ctx.RequestAborted);
                    }
                    await ctx.Response.Body.FlushAsync(ctx.RequestAborted);
                    lastPing = DateTimeOffset.UtcNow;
                    continue;
                }

                if (DateTimeOffset.UtcNow - lastPing >= TimeSpan.FromSeconds(1))
                {
                    var snap = new JsonObject
                    {
                        ["event"] = "snapshot",
                        ["tasks"] = new JsonArray(manager.List()
                            .Select(t => JsonSerializer.SerializeToNode(t)).ToArray()),
                        ["config"] = JsonSerializer.SerializeToNode(manager.Config),
                    };
                    await ctx.Response.WriteAsync($"event: snapshot\ndata: {snap.ToJsonString()}\n\n",
                        ctx.RequestAborted);
                    await ctx.Response.Body.FlushAsync(ctx.RequestAborted);
                    lastPing = DateTimeOffset.UtcNow;
                }
            }
        }
        catch
        {
            // client disconnected
        }
        finally
        {
            manager.Unsubscribe(sink);
        }
    }

    private async Task ShutdownAsync()
    {
        if (_app is null) return;
        try { await _app.StopAsync(TimeSpan.FromSeconds(1)); } catch { /* ignore */ }
    }

    public void Dispose()
    {
        try { _app?.StopAsync(TimeSpan.FromSeconds(1)).GetAwaiter().GetResult(); } catch { }
        if (_app is IDisposable disposable)
        {
            try { disposable.Dispose(); } catch { }
        }
    }
}