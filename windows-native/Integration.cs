using System.Text;
using Microsoft.Win32;

namespace Zdm.App;

/// <summary>
/// The "start with Windows" switch.
///
/// Uses the per-user Run key, which needs no administrator rights and is the
/// same value name the installer writes, so toggling it here and choosing the
/// installer task cannot end up with two competing entries.
/// </summary>
public static class AutoStart
{
    private const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
    private const string ValueName = "ZDM";

    /// <summary>
    /// Path of the running executable. A single-file publish has no assembly
    /// location on disk, so ProcessPath is the only reliable source; the
    /// AppContext fallback covers the (theoretical) case of it being null.
    /// </summary>
    public static string ExePath =>
        Environment.ProcessPath
        ?? Path.Combine(AppContext.BaseDirectory, "Zdm.exe");

    /// <summary>--autostart starts into the tray instead of popping a window.</summary>
    public static string Command => $"\"{ExePath}\" --autostart";

    public static bool IsEnabled()
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(RunKey);
            return key?.GetValue(ValueName) is string s && s.Length > 0;
        }
        catch
        {
            return false;
        }
    }

    public static bool SetEnabled(bool on) => TrySetEnabled(on, out _);

    /// <summary>
    /// 写注册表 ≠ 真的生效：键被组策略禁用、ACL 拒绝时 SetValue 有时也不抛异常。
    /// 写完回读一次确认，并把失败原因交给调用方显示（与 Python 端
    /// autostart.enable() 返回 (ok, reason) 对齐）——否则菜单勾上了，下次开机
    /// 什么都没有，用户只能自己猜。
    /// </summary>
    public static bool TrySetEnabled(bool on, out string reason)
    {
        reason = "";
        try
        {
            using var key = Registry.CurrentUser.CreateSubKey(RunKey);
            if (key is null)
            {
                reason = "打不开 HKCU 启动项注册表键";
                return false;
            }
            if (on)
                key.SetValue(ValueName, Command);
            else
                key.DeleteValue(ValueName, throwOnMissingValue: false);
        }
        catch (Exception e)
        {
            reason = "注册表写入失败：" + e.Message;
            return false;
        }
        if (IsEnabled() != on)
        {
            reason = "写入后回读不一致（可能被组策略或安全软件拦截）";
            return false;
        }
        return true;
    }

    /// <summary>
    /// Point the Run value at the current exe when it already exists. Without
    /// this, moving or reinstalling the app leaves a stale command behind and
    /// every boot pops a "file not found" dialog.
    /// </summary>
    public static void RepairIfStale()
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(RunKey, writable: true);
            if (key?.GetValue(ValueName) is not string s) return;
            if (!string.Equals(s, Command, StringComparison.OrdinalIgnoreCase))
                key.SetValue(ValueName, Command);
        }
        catch
        {
            // best effort
        }
    }
}

/// <summary>
/// Registers the <c>zdm://</c> URL protocol under HKCU.
///
/// This is what lets a browser (or the companion extension) wake ZDM up when it
/// is not running: the extension navigates a hidden tab to
/// <c>zdm://add?url=...</c>, Windows looks the scheme up in the registry, and
/// launches the exe with the URI as its argument. No admin rights and no
/// service required — HKCU\Software\Classes is per-user and honoured by every
/// Chromium-based browser.
/// </summary>
public static class ProtocolRegistrar
{
    public const string Scheme = "zdm";
    private const string Root = @"Software\Classes\" + Scheme;

    public static string ExpectedCommand => $"\"{AutoStart.ExePath}\" \"%1\"";

    /// <summary>True when the scheme is registered and points at this executable.</summary>
    public static bool IsRegistered()
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(Root + @"\shell\open\command");
            return key?.GetValue(null) is string s
                   && s.Contains(AutoStart.ExePath, StringComparison.OrdinalIgnoreCase);
        }
        catch
        {
            return false;
        }
    }

    /// <summary>
    /// Register (or refresh) the scheme. Called on every start because the exe
    /// path changes between a portable run and an installed copy.
    /// </summary>
    public static bool Register()
    {
        try
        {
            using var root = Registry.CurrentUser.CreateSubKey(Root);
            if (root is null) return false;

            root.SetValue(null, "URL:ZDM Download Protocol");
            // An empty "URL Protocol" value is what marks the key as a scheme.
            root.SetValue("URL Protocol", "");

            using (var icon = root.CreateSubKey("DefaultIcon"))
                icon?.SetValue(null, $"\"{AutoStart.ExePath}\",0");

            using (var cmd = root.CreateSubKey(@"shell\open\command"))
                cmd?.SetValue(null, ExpectedCommand);

            return true;
        }
        catch
        {
            return false;
        }
    }

    public static void Unregister()
    {
        try
        {
            Registry.CurrentUser.DeleteSubKeyTree(Root, throwOnMissingSubKey: false);
        }
        catch
        {
            // best effort
        }
    }
}

/// <summary>
/// A download handed over from outside the app: a <c>zdm://</c> URI from the
/// browser extension, or a plain http(s) URL on the command line (drag a link
/// onto the exe, or "open with" from Explorer).
/// </summary>
public sealed class ExternalRequest
{
    public required string Url { get; init; }
    public string? Filename { get; init; }
    public string? Directory { get; init; }
    public string? Referer { get; init; }
    public int? Connections { get; init; }
    public string? Source { get; init; }
    public string? Kind { get; init; }

    /// <summary>
    /// The page's own session cookie. Video sites that require a logged-in
    /// session (YouTube, Bilibili) answer 403 without it.
    /// </summary>
    public string? Cookie { get; init; }

    /// <summary>
    /// Headers to replay with the request. Browsers refuse hotlink-protected
    /// files without a matching Referer, so it is carried across when present.
    /// </summary>
    public Dictionary<string, string> Headers()
    {
        var h = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        if (!string.IsNullOrWhiteSpace(Referer)) h["Referer"] = Referer;
        if (!string.IsNullOrWhiteSpace(Cookie)) h["Cookie"] = Cookie;
        return h;
    }

    /// <summary>
    /// Parse the first argument that looks like a handover. Command-line flags
    /// (--autostart, --selftest, ...) and anything else are ignored, so this is
    /// safe to call on the raw argument vector.
    /// </summary>
    public static ExternalRequest? FromArgs(IEnumerable<string> args)
    {
        foreach (var raw in args)
        {
            if (string.IsNullOrWhiteSpace(raw) || raw.StartsWith("--") || raw.StartsWith('-'))
                continue;

            var parsed = Parse(raw);
            if (parsed is not null) return parsed;
        }
        return null;
    }

    private static bool IsHttp(string? v) =>
        !string.IsNullOrWhiteSpace(v)
        && (v.StartsWith("http://", StringComparison.OrdinalIgnoreCase)
            || v.StartsWith("https://", StringComparison.OrdinalIgnoreCase));

    /// <summary>
    /// Parse the JSON body a browser sends over the native-messaging channel:
    /// <c>{"url":"…","filename":"…","referer":"…","connections":8,"dir":"…"}</c>.
    /// Anything malformed returns null so the caller can fall back to the
    /// command line instead of starting an empty task.
    /// </summary>
    public static ExternalRequest? FromJson(string json)
    {
        try
        {
            if (System.Text.Json.Nodes.JsonNode.Parse(json) is not System.Text.Json.Nodes.JsonObject o)
                return null;

            var url = o["url"]?.GetValue<string>();
            if (!IsHttp(url)) return null;

            int? connections = null;
            try
            {
                var n = o["connections"]?.GetValue<int>();
                if (n is not null) connections = Math.Clamp(n.Value, 1, 32);
            }
            catch
            {
                // A non-numeric value is not worth failing the whole handover.
            }

            return new ExternalRequest
            {
                Url = url!,
                Filename = o["filename"]?.ToString(),
                Directory = o["dir"]?.ToString(),
                Referer = o["referer"]?.ToString(),
                // The page session, needed by video sites that require a login.
                Cookie = o["cookie"]?.ToString(),
                Connections = connections,
                // Provenance, so a sniffer push that had to wake the client
                // still shows up as "嗅探" rather than "手动".
                Source = o["source"]?.ToString(),
                Kind = o["kind"]?.ToString(),
            };
        }
        catch
        {
            return null;
        }
    }

    public static ExternalRequest? Parse(string raw)
    {
        var s = raw.Trim().Trim('"');
        if (s.Length == 0) return null;

        // Plain web link (command line, or a link dropped onto the exe).
        if (IsHttp(s)) return new ExternalRequest { Url = s };

        if (!s.StartsWith(ProtocolRegistrar.Scheme + "://", StringComparison.OrdinalIgnoreCase))
            return null;

        if (!Uri.TryCreate(s, UriKind.Absolute, out var uri)) return null;

        // zdm://add?url=...  -> host carries the verb, query carries the rest.
        var query = ParseQuery(uri.Query);
        if (!query.TryGetValue("url", out var url) || !IsHttp(url)) return null;

        return new ExternalRequest
        {
            Url = url,
            Filename = query.GetValueOrDefault("filename"),
            Directory = query.GetValueOrDefault("dir"),
            Referer = query.GetValueOrDefault("referer") ?? query.GetValueOrDefault("ref"),
            Source = query.GetValueOrDefault("source"),
            Kind = query.GetValueOrDefault("kind"),
            Connections = int.TryParse(query.GetValueOrDefault("connections"), out var c)
                ? Math.Clamp(c, 1, 32)
                : null,
        };
    }

    private static Dictionary<string, string> ParseQuery(string query)
    {
        var map = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        var q = query.TrimStart('?');
        if (q.Length == 0) return map;

        foreach (var pair in q.Split('&', StringSplitOptions.RemoveEmptyEntries))
        {
            var i = pair.IndexOf('=');
            var key = i < 0 ? pair : pair[..i];
            var value = i < 0 ? "" : pair[(i + 1)..];
            if (key.Length == 0) continue;
            map[Uri.UnescapeDataString(key)] = Uri.UnescapeDataString(value);
        }
        return map;
    }

    /// <summary>The URI form, used when a second instance forwards to the first.</summary>
    public string ToUri()
    {
        var sb = new StringBuilder($"{ProtocolRegistrar.Scheme}://add?url=")
            .Append(Uri.EscapeDataString(Url));
        if (!string.IsNullOrWhiteSpace(Filename))
            sb.Append("&filename=").Append(Uri.EscapeDataString(Filename));
        if (!string.IsNullOrWhiteSpace(Directory))
            sb.Append("&dir=").Append(Uri.EscapeDataString(Directory));
        if (!string.IsNullOrWhiteSpace(Referer))
            sb.Append("&referer=").Append(Uri.EscapeDataString(Referer));
        if (Connections is > 0)
            sb.Append("&connections=").Append(Connections);
        // Dropped here and the forwarded instance loses the badge.
        if (!string.IsNullOrWhiteSpace(Source))
            sb.Append("&source=").Append(Uri.EscapeDataString(Source));
        if (!string.IsNullOrWhiteSpace(Kind))
            sb.Append("&kind=").Append(Uri.EscapeDataString(Kind));
        // Dropped here and the forwarded instance answers 403 on video sites.
        if (!string.IsNullOrWhiteSpace(Cookie))
            sb.Append("&cookie=").Append(Uri.EscapeDataString(Cookie));
        return sb.ToString();
    }
}
