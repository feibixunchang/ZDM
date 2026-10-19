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

public sealed class UpdateInfo
{
    public string Tag { get; init; } = "";
    public string Url { get; init; } = "";
    public string AssetUrl { get; init; } = "";
    public bool IsNewer { get; init; }
}

/// <summary>
/// Checks GitHub for a newer release. Fails silently: an update check must
/// never interfere with downloading.
///
/// Deliberately avoids api.github.com. The unauthenticated REST endpoint is
/// capped at 60 requests per hour per IP, which a shared or NAT'd network
/// exhausts quickly and would leave the check permanently broken. Fetching the
/// release page and reading the tag out of it has no such limit.
/// </summary>
public sealed class UpdateChecker
{
    private const string ReleasesPage = "https://github.com/feibixunchang/ZDM/releases/latest";

    public async Task<UpdateInfo?> CheckAsync(CancellationToken ct = default)
    {
        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(8) };
            using var req = new HttpRequestMessage(HttpMethod.Get, ReleasesPage);
            req.Headers.TryAddWithoutValidation("User-Agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ZDM");
            req.Headers.TryAddWithoutValidation("Accept", "text/html");

            using var resp = await client.SendAsync(req, ct).ConfigureAwait(false);
            if (!resp.IsSuccessStatusCode) return null;

            var html = await resp.Content.ReadAsStringAsync(ct).ConfigureAwait(false);

            // /releases/latest redirects to /releases/tag/<tag>; the final
            // request URI carries the tag.
            var finalUrl = resp.RequestMessage?.RequestUri?.ToString() ?? ReleasesPage;
            var tag = ExtractTag(finalUrl) ?? ExtractTagFromHtml(html);
            if (tag is null) return null;

            return new UpdateInfo
            {
                Tag = tag,
                Url = ReleasesPage,
                AssetUrl = ReleasesPage,
                IsNewer = IsNewerThanCurrent(tag),
            };
        }
        catch
        {
            return null;
        }
    }

    private static string? ExtractTag(string url)
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
        if (!tag.StartsWith('v')) return false;
        return TryParse(tag[1..], out var remote)
               && TryParse(AppVersion.Current, out var local)
               && remote > local;
    }

    private static bool TryParse(string s, out Version v)
    {
        var core = s.Split('-')[0];
        return Version.TryParse(core, out v!);
    }
}