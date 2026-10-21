using System.Diagnostics;
using System.Net.Http;
using System.Reflection;
using System.Text.Json;

namespace Zdm.App;

/// <summary>Single place that knows which build this is.</summary>
public static class AppVersion
{
    public static string Current { get; } = Read();

    private static string Read()
    {
        // AssemblyInformationalVersion carries the csproj <Version>, which is
        // the single value the installer and GitHub release also use.
        var asm = Assembly.GetExecutingAssembly();
        var info = asm.GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion;
        if (!string.IsNullOrWhiteSpace(info))
        {
            // Strip the "+<commit sha>" suffix the SDK appends.
            var plus = info.IndexOf('+');
            return plus > 0 ? info[..plus] : info;
        }
        return asm.GetName().Version?.ToString(3) ?? "0.0.0";
    }

    public static string Display => $"v{Current}";
}

public sealed class ReleaseAsset
{
    public string Name { get; init; } = "";
    public string Url { get; init; } = "";
    public long Size { get; init; }
}

public sealed class UpdateInfo
{
    public string Tag { get; init; } = "";
    public string Version { get; init; } = "";
    public string Url { get; init; } = "";
    public string Notes { get; init; } = "";
    /// <summary>安装包直链（能直接下载时才有值）。</summary>
    public string AssetName { get; init; } = "";
    public string AssetUrl { get; init; } = "";
    public long AssetSize { get; init; }
    public bool IsNewer { get; init; }

    /// <summary>能一键下载安装吗（没有匹配当前平台的安装包就只能开网页）。</summary>
    public bool CanInstall => !string.IsNullOrEmpty(AssetUrl);
}

/// <summary>
/// Checks GitHub for a newer release and, unlike the old version, resolves the
/// actual installer URL so the app can download and install it by itself.
///
/// Two transports, tried in order:
///   1. api.github.com …/releases/latest — carries assets[] with download URLs
///      and sizes. Unauthenticated limit is 60 requests/hour/IP, which is fine
///      for a desktop client that checks once per launch/click.
///   2. The release page + /releases/expanded_assets/&lt;tag&gt; — plain HTML with
///      no rate limit, used when the API answers 403 (shared/NAT'd networks).
/// Fails silently: an update check must never interfere with downloading.
/// </summary>
public sealed class UpdateChecker
{
    private const string Repo = "feibixunchang/ZDM";
    private const string ApiLatest = $"https://api.github.com/repos/{Repo}/releases/latest";
    private const string ReleasesPage = $"https://github.com/{Repo}/releases/latest";
    private const string UserAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ZDM";

    public async Task<UpdateInfo?> CheckAsync(CancellationToken ct = default)
    {
        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(12) };
            client.DefaultRequestHeaders.TryAddWithoutValidation("User-Agent", UserAgent);

            var viaApi = await CheckViaApiAsync(client, ct).ConfigureAwait(false);
            if (viaApi is not null) return viaApi;

            return await CheckViaHtmlAsync(client, ct).ConfigureAwait(false);
        }
        catch
        {
            return null;
        }
    }

    // ------------------------------------------------------------------ API

    private static async Task<UpdateInfo?> CheckViaApiAsync(HttpClient client, CancellationToken ct)
    {
        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Get, ApiLatest);
            req.Headers.TryAddWithoutValidation("Accept", "application/vnd.github+json");

            using var resp = await client.SendAsync(req, ct).ConfigureAwait(false);
            if (!resp.IsSuccessStatusCode) return null; // 403/404 → 走 HTML 那条

            await using var stream = await resp.Content.ReadAsStreamAsync(ct).ConfigureAwait(false);
            using var doc = await JsonDocument.ParseAsync(stream, cancellationToken: ct)
                .ConfigureAwait(false);
            var root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object) return null;

            var tag = Str(root, "tag_name");
            if (string.IsNullOrEmpty(tag)) return null;

            var assets = new List<ReleaseAsset>();
            if (root.TryGetProperty("assets", out var arr) && arr.ValueKind == JsonValueKind.Array)
            {
                foreach (var a in arr.EnumerateArray())
                {
                    var name = Str(a, "name");
                    var url = Str(a, "browser_download_url");
                    if (string.IsNullOrEmpty(name) || string.IsNullOrEmpty(url)) continue;
                    long size = 0;
                    if (a.TryGetProperty("size", out var sz) && sz.ValueKind == JsonValueKind.Number)
                        size = sz.GetInt64();
                    assets.Add(new ReleaseAsset { Name = name, Url = url, Size = size });
                }
            }

            var pick = PickAsset(assets);
            return new UpdateInfo
            {
                Tag = tag,
                Version = StripV(tag),
                Url = $"https://github.com/{Repo}/releases/tag/{tag}",
                Notes = Str(root, "body"),
                AssetName = pick?.Name ?? "",
                AssetUrl = pick?.Url ?? "",
                AssetSize = pick?.Size ?? 0,
                IsNewer = IsNewerThanCurrent(tag),
            };
        }
        catch
        {
            return null;
        }
    }

    // ----------------------------------------------------------------- HTML

    private static async Task<UpdateInfo?> CheckViaHtmlAsync(HttpClient client, CancellationToken ct)
    {
        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Get, ReleasesPage);
            req.Headers.TryAddWithoutValidation("Accept", "text/html");

            using var resp = await client.SendAsync(req, ct).ConfigureAwait(false);
            if (!resp.IsSuccessStatusCode) return null;

            var html = await resp.Content.ReadAsStringAsync(ct).ConfigureAwait(false);

            // /releases/latest redirects to /releases/tag/<tag>; the final
            // request URI carries the tag.
            var finalUrl = resp.RequestMessage?.RequestUri?.ToString() ?? ReleasesPage;
            var tag = ExtractTagFromUrl(finalUrl) ?? ExtractTagFromHtml(html);
            if (tag is null) return null;

            var assets = await FetchAssetsAsync(client, tag, ct).ConfigureAwait(false);
            var pick = PickAsset(assets);
            return new UpdateInfo
            {
                Tag = tag,
                Version = StripV(tag),
                Url = $"https://github.com/{Repo}/releases/tag/{tag}",
                AssetName = pick?.Name ?? "",
                AssetUrl = pick?.Url ?? "",
                AssetSize = pick?.Size ?? 0,
                IsNewer = IsNewerThanCurrent(tag),
            };
        }
        catch
        {
            return null;
        }
    }

    /// <summary>
    /// /releases/expanded_assets/&lt;tag&gt; returns just the asset list fragment —
    /// no login, no rate limit, and it carries the file names and sizes.
    /// </summary>
    private static async Task<List<ReleaseAsset>> FetchAssetsAsync(
        HttpClient client, string tag, CancellationToken ct)
    {
        var list = new List<ReleaseAsset>();
        try
        {
            var url = $"https://github.com/{Repo}/releases/expanded_assets/{tag}";
            using var req = new HttpRequestMessage(HttpMethod.Get, url);
            req.Headers.TryAddWithoutValidation("Accept", "text/html");

            using var resp = await client.SendAsync(req, ct).ConfigureAwait(false);
            if (!resp.IsSuccessStatusCode) return list;

            var html = await resp.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
            var marker = $"/releases/download/{tag}/";
            var i = 0;
            while ((i = html.IndexOf(marker, i, StringComparison.OrdinalIgnoreCase)) >= 0)
            {
                var tail = html[(i + marker.Length)..];
                var end = tail.IndexOfAny(new[] { '"', '<', '\'', ' ' });
                var name = end > 0 ? tail[..end] : tail;
                i += marker.Length;
                if (name.Length == 0) continue;
                var at = name.IndexOfAny(new[] { '?' });
                if (at > 0) name = name[..at];
                if (list.Exists(a => a.Name == name)) continue;
                list.Add(new ReleaseAsset
                {
                    Name = name,
                    Url = $"https://github.com/{Repo}/releases/download/{tag}/{name}",
                });
            }
        }
        catch
        {
            // 拿不到资产列表 = 没有直链，上层退回「打开下载页面」
        }
        return list;
    }

    // --------------------------------------------------------------- helpers

    /// <summary>
    /// Pick the installer for this build. The repo ships several:
    ///   ZDM-Setup-x.y.z.exe         ← 原生 WPF 客户端（就是本程序）
    ///   ZDM-PyQt5-Setup-x.y.z.exe   ← 老的 Python 客户端，不能装给本程序
    ///   ZDM-x.y.z.dmg / .deb / .tar.gz / ZDM-PyQt5-Setup.exe
    /// 选错会把 PyQt5 版覆盖到原生安装目录上，所以排除项写在前面。
    /// </summary>
    private static ReleaseAsset? PickAsset(List<ReleaseAsset> assets)
    {
        if (assets.Count == 0) return null;

        static bool IsNativeSetup(ReleaseAsset a) =>
            a.Name.EndsWith(".exe", StringComparison.OrdinalIgnoreCase)
            && a.Name.Contains("setup", StringComparison.OrdinalIgnoreCase)
            && !a.Name.Contains("pyqt", StringComparison.OrdinalIgnoreCase);

        var hit = assets.FirstOrDefault(IsNativeSetup);
        if (hit is not null) return hit;

        // 退而求其次：便携 zip（解压即用），仍然是原生构建
        hit = assets.FirstOrDefault(a =>
            a.Name.EndsWith(".zip", StringComparison.OrdinalIgnoreCase)
            && !a.Name.Contains("pyqt", StringComparison.OrdinalIgnoreCase));
        return hit;
    }

    private static string Str(JsonElement e, string name) =>
        e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String
            ? (v.GetString() ?? "")
            : "";

    private static string StripV(string tag) =>
        tag.StartsWith('v') || tag.StartsWith('V') ? tag[1..] : tag;

    private static string? ExtractTagFromUrl(string url)
    {
        const string marker = "/releases/tag/";
        var i = url.IndexOf(marker, StringComparison.OrdinalIgnoreCase);
        if (i < 0) return null;
        var tail = url[(i + marker.Length)..];
        var end = tail.IndexOfAny(new[] { '/', '?', '#' });
        var tag = end > 0 ? tail[..end] : tail;
        return tag.Length == 0 ? null : Uri.UnescapeDataString(tag);
    }

    private static string? ExtractTagFromHtml(string html)
    {
        // Fallback: the page embeds the tag in a canonical/og:url meta tag.
        const string needle = "/releases/tag/";
        var i = html.IndexOf(needle, StringComparison.OrdinalIgnoreCase);
        if (i < 0) return null;
        var tail = html[(i + needle.Length)..];
        var end = tail.IndexOfAny(new[] { '"', '<', '\'', ' ', '/' });
        var tag = end > 0 ? tail[..end] : tail;
        return tag.Length == 0 ? null : Uri.UnescapeDataString(tag);
    }

    private static bool IsNewerThanCurrent(string tag)
    {
        return TryParse(StripV(tag), out var remote)
               && TryParse(AppVersion.Current, out var local)
               && remote > local;
    }

    private static bool TryParse(string s, out Version v)
    {
        var core = s.Split('-')[0];
        return Version.TryParse(core, out v!);
    }
}

/// <summary>下载安装包并把它交给安装器 —— 让「检查更新」不用再去浏览器。</summary>
public static class UpdateInstaller
{
    private const string UserAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ZDM";

    /// <summary>
    /// Download to %TEMP%\ZDM\&lt;安装包名&gt;，边下边报进度。
    /// 先写 .part 再改名：中途断网不会留下一个「看起来完整」的坏安装包。
    /// </summary>
    public static async Task<string> DownloadAsync(
        UpdateInfo info,
        IProgress<(long Got, long Total)>? progress,
        CancellationToken ct = default)
    {
        if (!info.CanInstall) throw new InvalidOperationException("这个版本没有可下载的安装包");

        var dir = Path.Combine(Path.GetTempPath(), "ZDM");
        Directory.CreateDirectory(dir);
        var name = string.IsNullOrEmpty(info.AssetName) ? "ZDM-Setup.exe" : info.AssetName;
        var dest = Path.Combine(dir, name);
        var part = dest + ".part";
        if (File.Exists(part)) File.Delete(part);

        using var client = new HttpClient { Timeout = Timeout.InfiniteTimeSpan };
        client.DefaultRequestHeaders.TryAddWithoutValidation("User-Agent", UserAgent);

        using var resp = await client
            .GetAsync(info.AssetUrl, HttpCompletionOption.ResponseHeadersRead, ct)
            .ConfigureAwait(false);
        resp.EnsureSuccessStatusCode();

        var total = resp.Content.Headers.ContentLength ?? info.AssetSize;
        long got = 0;
        progress?.Report((0, total));

        await using (var src = await resp.Content.ReadAsStreamAsync(ct).ConfigureAwait(false))
        await using (var dst = File.Create(part))
        {
            var buf = new byte[81920];
            int n;
            while ((n = await src.ReadAsync(buf, ct).ConfigureAwait(false)) > 0)
            {
                await dst.WriteAsync(buf.AsMemory(0, n), ct).ConfigureAwait(false);
                got += n;
                progress?.Report((got, total));
            }
        }

        var size = new FileInfo(part).Length;
        // 小于 1MB 的一定不是安装包（多半是错误页/重定向），别拿它去执行
        if (size < 1024 * 1024) throw new IOException($"安装包异常小（{size} 字节）");
        if (info.AssetSize > 0 && total > 0 && size < total)
            throw new IOException("安装包下载不完整，请重试");

        if (File.Exists(dest)) File.Delete(dest);
        File.Move(part, dest);
        return dest;
    }

    /// <summary>
    /// 启动安装器后调用方应立即退出：/CLOSEAPPLICATIONS 让 Inno 能覆盖正在运行的 exe，
    /// /VERYSILENT 不弹向导（下载都做完了，再点一遍「下一步」没有意义）。
    /// </summary>
    public static bool RunInstaller(string path)
    {
        try
        {
            Process.Start(new ProcessStartInfo(path)
            {
                UseShellExecute = true,
                Arguments = "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS",
            });
            return true;
        }
        catch
        {
            return false;
        }
    }
}
