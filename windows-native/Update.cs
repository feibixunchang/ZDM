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
/// Checks the GitHub releases API for a newer tag. Fails silently: an update
/// check must never interfere with downloading.
/// </summary>
public sealed class UpdateChecker
{
    private const string Repo = "feibixunchang/ZDM";

    public async Task<UpdateInfo?> CheckAsync(CancellationToken ct = default)
    {
        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(6) };
            // Latest release, excluding drafts and prereleases.
            var url = $"https://api.github.com/repos/{Repo}/releases/latest";
            using var req = new HttpRequestMessage(HttpMethod.Get, url);
            req.Headers.TryAddWithoutValidation("User-Agent", "ZDM");
            req.Headers.TryAddWithoutValidation("Accept", "application/vnd.github+json");

            using var resp = await client.SendAsync(req, ct).ConfigureAwait(false);
            if (!resp.IsSuccessStatusCode) return null;

            var json = await resp.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;

            var tag = root.TryGetProperty("tag_name", out var t) ? t.GetString() ?? "" : "";
            if (tag.Length == 0) return null;

            var html = root.TryGetProperty("html_url", out var h) ? h.GetString() ?? "" : "";
            var asset = "";
            if (root.TryGetProperty("assets", out var assets) &&
                assets.ValueKind == JsonValueKind.Array)
            {
                foreach (var a in assets.EnumerateArray())
                {
                    var name = a.TryGetProperty("name", out var n) ? n.GetString() ?? "" : "";
                    if (name.EndsWith(".exe", StringComparison.OrdinalIgnoreCase))
                    {
                        asset = a.TryGetProperty("browser_download_url", out var u)
                            ? u.GetString() ?? "" : "";
                        break;
                    }
                }
            }

            return new UpdateInfo
            {
                Tag = tag,
                Url = html,
                AssetUrl = asset,
                IsNewer = IsNewerThanCurrent(tag),
            };
        }
        catch
        {
            return null;
        }
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