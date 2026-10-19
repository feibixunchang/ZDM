using System.Diagnostics;
using System.Net;
using System.Text;

namespace Zdm.App.Core;

public sealed class HttpFailureException : Exception
{
    public HttpFailureException(string message) : base(message) { }
}

public static class Filenames
{
    private static readonly char[] Illegal = "<>:\"/\\|?*".ToCharArray();

    /// <summary>
    /// Strip separators and characters Windows rejects, matching the Python
    /// version's sanitize_filename so existing tasks keep the same names.
    /// </summary>
    public static string Sanitize(string? name, int maxLen = 180)
    {
        if (string.IsNullOrWhiteSpace(name))
            return "download";

        var sb = new StringBuilder(name.Length);
        foreach (var c in name)
        {
            if (c == 0 || c < ' ' || Array.IndexOf(Illegal, c) >= 0)
                sb.Append('_');
            else
                sb.Append(c);
        }
        var cleaned = sb.ToString().Trim().Trim('.').Trim();
        if (cleaned.Length == 0)
            cleaned = "download";

        // Reserved device names (CON, PRN, COM1...) must be prefixed.
        var stem = cleaned.Split('.')[0].ToUpperInvariant();
        if (Reserved.Contains(stem))
            cleaned = "_" + cleaned;

        return cleaned.Length > maxLen ? cleaned[..maxLen] : cleaned;
    }

    private static readonly HashSet<string> Reserved = BuildReserved();

    private static HashSet<string> BuildReserved()
    {
        var set = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
            { "CON", "PRN", "AUX", "NUL" };
        for (var i = 1; i <= 9; i++)
        {
            set.Add($"COM{i}");
            set.Add($"LPT{i}");
        }
        return set;
    }

    /// <summary>Resolve a download filename from Content-Disposition, else the URL path.</summary>
    public static string FromHeaders(IReadOnlyDictionary<string, string> headers, string url)
    {
        foreach (var key in new[] { "content-disposition", "Content-Disposition" })
        {
            if (headers.TryGetValue(key, out var cd) && !string.IsNullOrWhiteSpace(cd))
            {
                var name = ParseContentDispositionFilename(cd);
                if (!string.IsNullOrWhiteSpace(name))
                    return Sanitize(name);
            }
        }

        if (Uri.TryCreate(url, UriKind.Absolute, out var uri))
        {
            var baseName = Uri.UnescapeDataString(uri.Segments.LastOrDefault()?.TrimEnd('/') ?? "");
            if (!string.IsNullOrWhiteSpace(baseName))
                return Sanitize(baseName);
        }
        return "download";
    }

    private static string? ParseContentDispositionFilename(string cd)
    {
        // RFC 5987 form: filename*=UTF-8''name
        var star = cd.IndexOf("filename*=", StringComparison.OrdinalIgnoreCase);
        if (star >= 0)
        {
            var rest = cd[(star + 10)..].Split(';')[0].Trim().Trim('"');
            var parts = rest.Split("''", 2);
            var raw = parts.Length > 1 ? parts[1] : parts[0];
            return CleanName(raw);
        }

        var idx = cd.IndexOf("filename=", StringComparison.OrdinalIgnoreCase);
        if (idx >= 0)
        {
            var raw = cd[(idx + 9)..].Split(';')[0].Trim().Trim('"');
            return CleanName(raw);
        }
        return null;
    }

    private static string? CleanName(string raw)
    {
        if (string.IsNullOrWhiteSpace(raw))
            return null;
        try
        {
            raw = Uri.UnescapeDataString(raw);
        }
        catch
        {
            // keep the raw value if it is not valid percent-encoding
        }
        raw = raw.Trim().Trim('"');
        raw = raw.Split('/').Last().Split('\\').Last();
        return string.IsNullOrWhiteSpace(raw) ? null : raw;
    }

    /// <summary>Avoid clobbering an existing file by appending (1), (2), ...</summary>
    public static string UniqueIn(string directory, string filename)
    {
        string PathFor(string n) => Path.Combine(directory, n);

        if (!File.Exists(PathFor(filename)) && !File.Exists(PathFor(filename + ".part")))
            return filename;

        var idx = filename.LastIndexOf('.');
        var stem = idx > 0 ? filename[..idx] : filename;
        var ext = idx > 0 ? filename[idx..] : "";

        for (var i = 1; i < 1000; i++)
        {
            var candidate = $"{stem}({i}){ext}";
            if (!File.Exists(PathFor(candidate)) && !File.Exists(PathFor(candidate) + ".part"))
                return candidate;
        }
        return $"{stem}-{DateTimeOffset.UtcNow.ToUnixTimeSeconds()}{ext}";
    }
}

public static class Human
{
    private static readonly string[] Units = { "B", "KB", "MB", "GB", "TB", "PB" };

    public static string Size(long? bytes)
    {
        double v = bytes ?? 0;
        if (v < 1024) return $"{(int)v} B";
        for (var i = 0; i < Units.Length; i++)
        {
            v /= 1024;
            if (v < 1024) return $"{v:0.0} {Units[i]}";
        }
        return $"{v:0.0} PB";
    }

    public static string Speed(long bytesPerSecond)
    {
        if (bytesPerSecond <= 0) return "--";
        return $"{Size(bytesPerSecond)}/s";
    }

    public static string Eta(long? seconds)
    {
        if (seconds is null or <= 0) return "";
        var s = (long)seconds;
        var h = s / 3600;
        var m = s % 3600 / 60;
        var sec = s % 60;
        return h > 0 ? $"{h}时{m}分" : m > 0 ? $"{m}分{sec}秒" : $"{sec}秒";
    }

    public static long ParseSize(string? text)
    {
        if (string.IsNullOrWhiteSpace(text)) return 0;
        var t = text.Trim().ToUpperInvariant();
        double mult = 1;
        foreach (var (suffix, m) in new[] { ("KB", 1024d), ("MB", 1024d * 1024), ("GB", 1024d * 1024 * 1024) })
        {
            if (t.EndsWith(suffix))
            {
                mult = m;
                t = t[..^suffix.Length].Trim();
                break;
            }
        }
        return double.TryParse(t, out var v) ? (long)(v * mult) : 0;
    }

    // ShellExecute verbs and constants.
    private const int SW_SHOWNORMAL = 1;
    private const int SW_RESTORE = 9;

    [System.Runtime.InteropServices.DllImport("shell32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr ShellExecuteW(IntPtr hwnd, string? lpOperation, string lpFile,
        string? lpParameters, string? lpDirectory, int nShowCmd);

    /// <summary>Open a file with whatever program the user has associated with it.</summary>
    public static bool OpenFile(string path)
    {
        if (string.IsNullOrWhiteSpace(path) || !File.Exists(path)) return false;
        return ShellExecuteW(IntPtr.Zero, "open", path, null, null, SW_SHOWNORMAL) != IntPtr.Zero;
    }

    /// <summary>Reveal a file in Explorer, or open the folder when it does not exist.</summary>
    public static bool OpenInExplorer(string path)
    {
        if (string.IsNullOrWhiteSpace(path)) return false;

        try
        {
            if (Directory.Exists(path))
                // "explore" on the folder itself: opens it in a new window.
                return ShellExecuteW(IntPtr.Zero, "explore", path, null, null, SW_SHOWNORMAL)
                       != IntPtr.Zero;

            if (File.Exists(path))
            {
                // /select needs the path without quotes around the whole argument;
                // ShellExecute adds its own quoting and stray quotes make
                // explorer treat the argument as a program to run.
                var dir = Path.GetDirectoryName(path) ?? string.Empty;
                return ShellExecuteW(IntPtr.Zero, "open", dir, $"/select,{path}", null, SW_SHOWNORMAL)
                       != IntPtr.Zero;
            }

            var parent = Path.GetDirectoryName(path);
            return !string.IsNullOrEmpty(parent) && Directory.Exists(parent)
                && OpenInExplorer(parent);
        }
        catch (Exception ex)
        {
            Debug.WriteLine($"[open] {ex.GetType().Name}: {ex.Message}");
            return false;
        }
    }

    /// <summary>Bring an already-open Explorer window to the front.</summary>
    public static void RestoreExplorer(string path)
        => ShellExecuteW(IntPtr.Zero, "open", path, null, null, SW_RESTORE);
}