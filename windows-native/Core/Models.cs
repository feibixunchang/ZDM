using System.Text.Json.Serialization;

namespace Zdm.App.Core;

/// <summary>Task lifecycle, matching the states the web UI and extension expect.</summary>
public enum TaskStatus
{
    Queued,
    Downloading,
    Paused,
    Completed,
    Error,
    Canceled,
    Merging,
}

/// <summary>Serialised form of <see cref="TaskStatus"/> used by the REST API.</summary>
public static class TaskStatusNames
{
    public const string Queued = "queued";
    public const string Downloading = "downloading";
    public const string Paused = "paused";
    public const string Completed = "completed";
    public const string Error = "error";
    public const string Canceled = "canceled";
    public const string Merging = "merging";

    public static string ToApi(TaskStatus s) => s switch
    {
        TaskStatus.Queued => Queued,
        TaskStatus.Downloading => Downloading,
        TaskStatus.Paused => Paused,
        TaskStatus.Completed => Completed,
        TaskStatus.Error => Error,
        TaskStatus.Canceled => Canceled,
        TaskStatus.Merging => Merging,
        _ => Queued,
    };

    public static TaskStatus FromApi(string? s) => s switch
    {
        Downloading => TaskStatus.Downloading,
        Paused => TaskStatus.Paused,
        Completed => TaskStatus.Completed,
        Error => TaskStatus.Error,
        Canceled => TaskStatus.Canceled,
        Merging => TaskStatus.Merging,
        _ => TaskStatus.Queued,
    };

    public static string ToChinese(TaskStatus s) => s switch
    {
        TaskStatus.Queued => "排队中",
        TaskStatus.Downloading => "下载中",
        TaskStatus.Paused => "已暂停",
        TaskStatus.Completed => "已完成",
        TaskStatus.Error => "失败",
        TaskStatus.Canceled => "已取消",
        TaskStatus.Merging => "合并中",
        _ => "排队中",
    };
}

/// <summary>One byte range plus its write cursor.</summary>
public sealed class Chunk
{
    public long Start { get; set; }
    public long End { get; set; }
    public long Pos { get; set; }

    /// <summary>Connection id currently working on this chunk, or null when free.</summary>
    public int? Owner { get; set; }

    public int Attempts { get; set; }

    public long Remaining => Math.Max(0, End - Pos);

    public Chunk() { }

    public Chunk(long start, long end, long? pos = null)
    {
        Start = start;
        End = end;
        Pos = pos ?? start;
    }

    public long[] ToArray() => new[] { Start, End, Pos };

    public static Chunk FromArray(long[] a) => new(a[0], a[1], a[2]);
}

/// <summary>Where a task came from, matching the Python build's vocabulary.</summary>
public static class TaskSources
{
    public const string Manual = "manual";
    public const string Browser = "browser";
    public const string Sniffer = "sniffer";
    public const string WebUi = "webui";
    public const string Protocol = "protocol";

    /// <summary>
    /// Anything outside the known set collapses to <see cref="Manual"/>: the
    /// value reaches us over the loopback REST API from the browser
    /// extension, so it is untrusted input that ends up in a UI label.
    /// </summary>
    // The "!" matters: the switch arms mix string? and string, so the inferred
    // type is nullable and returning it from a non-nullable method is a warning
    // - which this project builds with -warnaserror.
    public static string Normalize(string? s) => s switch
    {
        Manual or Browser or Sniffer or WebUi or Protocol => s!,
        _ => Manual,
    };
}

/// <summary>Broad payload kind. Empty means "not a media file".</summary>
public static class TaskKinds
{
    public const string Video = "video";
    public const string Audio = "audio";

    public static string Normalize(string? s) => s switch
    {
        Video or Audio => s!,
        _ => "",
    };
}

/// <summary>Public snapshot of a task, shaped exactly like the Python version's JSON.</summary>
public sealed class TaskSnapshot
{
    [JsonPropertyName("id")] public string Id { get; set; } = "";
    [JsonPropertyName("url")] public string Url { get; set; } = "";
    [JsonPropertyName("final_url")] public string FinalUrl { get; set; } = "";
    [JsonPropertyName("filename")] public string Filename { get; set; } = "";
    [JsonPropertyName("directory")] public string Directory { get; set; } = "";
    [JsonPropertyName("size")] public long Size { get; set; }
    [JsonPropertyName("status")] public string Status { get; set; } = TaskStatusNames.Queued;
    [JsonPropertyName("error")] public string? Error { get; set; }
    [JsonPropertyName("downloaded")] public long Downloaded { get; set; }
    [JsonPropertyName("speed")] public long Speed { get; set; }
    [JsonPropertyName("connections")] public int Connections { get; set; }
    [JsonPropertyName("supports_range")] public bool SupportsRange { get; set; }
    [JsonPropertyName("transport")] public string Transport { get; set; } = "native";
    [JsonPropertyName("headers")] public Dictionary<string, string> Headers { get; set; } = new();
    [JsonPropertyName("proxy")] public string? Proxy { get; set; }
    [JsonPropertyName("speed_limit")] public long SpeedLimit { get; set; }
    [JsonPropertyName("created_at")] public double CreatedAt { get; set; }
    [JsonPropertyName("completed_at")] public double? CompletedAt { get; set; }
    [JsonPropertyName("eta")] public int? Eta { get; set; }
    [JsonPropertyName("progress")] public double Progress { get; set; }
    [JsonPropertyName("chunks")] public long[][] Chunks { get; set; } = Array.Empty<long[]>();
    // Provenance. Defaults keep pre-1.12.0 state files and older callers that
    // omit both fields working unchanged.
    [JsonPropertyName("source")] public string Source { get; set; } = TaskSources.Manual;
    [JsonPropertyName("kind")] public string Kind { get; set; } = "";
    [JsonPropertyName("audio_url")] public string AudioUrl { get; set; } = "";
}