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
    public const string Version = "1.0.0";

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

    /// <summary>
    /// Extension ids already whitelisted for native messaging. Seeded from the
    /// manifest on disk so the first requests do not rewrite the file.
    /// </summary>
    private static readonly HashSet<string> KnownExtensionIds =
        new(NativeMessagingRegistrar.AllowedOrigins(), StringComparer.Ordinal);

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
            if (extId.Length == 32 && KnownExtensionIds.Add(extId))
                NativeMessagingRegistrar.Register(extId);

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

        // ---------------- api ----------------
        app.MapGet("/api", () => Results.Json(new JsonObject
        {
            ["ok"] = true,
            ["version"] = Version,
        }));

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
                    if (p.Value != null)
                        headers[p.Key] = p.Value.GetValue<string>();

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
                var d = manager.Add(
                    url,
                    directory: body?["dir"]?.GetValue<string>() ?? body?["directory"]?.GetValue<string>(),
                    filename: body?["filename"]?.GetValue<string>() ?? body?["name"]?.GetValue<string>(),
                    connections: body?["connections"]?.GetValue<int?>(),
                    headers: headers,
                    proxy: body?["proxy"]?.GetValue<string>(),
                    speedLimit: body?["speed_limit"]?.GetValue<long?>(),
                    startNow: body?["paused"]?.GetValue<bool?>() != true);

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

        app.MapDelete("/api/tasks/{id}", (HttpContext ctx, string id) =>
        {
            // ?files=0 keeps the partial data while dropping the task.
            var keepFiles = !(ctx.Request.Query.ContainsKey("files")
                              && ctx.Request.Query["files"] == "0");
            var ok = manager.RemoveAsync(id, keepFiles).GetAwaiter().GetResult();
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
            Human.OpenInExplorer(body?["path"]?.GetValue<string>() ?? manager.Config.DownloadDir);
            return Results.Json(new JsonObject { ["ok"] = true });
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

        if (!path.StartsWith(root, StringComparison.OrdinalIgnoreCase) || !File.Exists(path))
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