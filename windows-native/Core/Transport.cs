using System.Net;
using System.Net.Http.Headers;

namespace Zdm.App.Core;

public sealed class ProbeResult
{
    public string FinalUrl { get; init; } = "";
    public int Status { get; init; }
    public Dictionary<string, string> Headers { get; init; } = new(StringComparer.OrdinalIgnoreCase);
    public long Size { get; init; }
    public bool SupportsRange { get; init; }
}

/// <summary>
/// HTTP transport built on HttpClient with HTTP/2 enabled, which is what most
/// CDNs require; the Python version needed a curl fallback for exactly that.
/// </summary>
public sealed class Transport : IDisposable
{
    private static readonly Lazy<Transport> Shared = new(() => new Transport());

    public static Transport Instance => Shared.Value;

    private readonly HttpClient _client;

    public Transport()
    {
        var handler = new SocketsHttpHandler
        {
            // HTTP/2 by default, plain HTTP/1.1 fallback, no forced downgrade
            EnableMultipleHttp2Connections = true,
            PooledConnectionIdleTimeout = TimeSpan.FromSeconds(30),
            ConnectTimeout = TimeSpan.FromSeconds(30),
            AutomaticDecompression = DecompressionMethods.None, // Range math needs raw bytes
            AllowAutoRedirect = false, // handled manually so Range survives redirects
        };

        _client = new HttpClient(handler) { Timeout = Timeout.InfiniteTimeSpan };
        _client.DefaultRequestHeaders.TryAddWithoutValidation("User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            + "Chrome/131.0.0.0 Safari/537.36");
        _client.DefaultRequestHeaders.TryAddWithoutValidation("Accept", "*/*");
        // identity encoding: never let the server gzip, offsets must be exact
        _client.DefaultRequestHeaders.TryAddWithoutValidation("Accept-Encoding", "identity");
    }

    private const int MaxRedirects = 8;

    private HttpRequestMessage BuildRequest(
        string url,
        IReadOnlyDictionary<string, string> headers,
        long? rangeStart, long? rangeEnd)
    {
        var req = new HttpRequestMessage(HttpMethod.Get, url);
        if (rangeStart is { } start)
        {
            var range = rangeEnd.HasValue
                ? $"bytes={start}-{rangeEnd.Value - 1}"
                : $"bytes={start}-";
            req.Headers.TryAddWithoutValidation("Range", range);
        }
        foreach (var (k, v) in headers)
            req.Headers.TryAddWithoutValidation(k, v);
        return req;
    }

    private static Dictionary<string, string> ToDictionary(HttpResponseMessage resp)
    {
        var map = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (var h in resp.Headers)
            map[h.Key] = string.Join(", ", h.Value);
        foreach (var h in resp.Content.Headers)
            map[h.Key] = string.Join(", ", h.Value);
        return map;
    }

    /// <summary>Resolve final URL, size and Range support with a 1-byte probe.</summary>
    public async Task<ProbeResult> ProbeAsync(
        string url,
        IReadOnlyDictionary<string, string> headers,
        TimeSpan timeout,
        string? proxy,
        CancellationToken ct)
    {
        var current = url;
        using var timeoutCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeoutCts.CancelAfter(timeout);

        for (var hop = 0; hop <= MaxRedirects; hop++)
        {
            using var req = BuildRequest(current, headers, 0, 1);
            using var resp = await _client.SendAsync(
                req, HttpCompletionOption.ResponseHeadersRead, timeoutCts.Token);

            var status = (int)resp.StatusCode;
            if (status is 301 or 302 or 303 or 307 or 308)
            {
                var loc = resp.Headers.Location;
                current = loc is null
                    ? current
                    : new Uri(new Uri(current), loc).ToString();
                continue;
            }

            var map = ToDictionary(resp);
            var (size, supports) = ParseSize(map, status);

            // If the byte-range probe gave us a size that cannot be right
            // (0, or suspiciously tiny), confirm with a HEAD request. Some
            // proxies answer the probe with a 1-byte 200/206 body and no
            // Content-Range, which would otherwise poison the whole task.
            if (size <= 1)
            {
                var head = await ProbeWithHeadAsync(current, headers, timeoutCts.Token)
                    .ConfigureAwait(false);
                if (head is not null)
                {
                    size = head.Value.Size;
                    supports = supports || head.Value.Supports;
                }
            }

            return new ProbeResult
            {
                FinalUrl = current,
                Status = status,
                Headers = map,
                Size = size,
                SupportsRange = supports,
            };
        }

        throw new HttpFailureException("重定向次数过多");
    }

    /// <summary>HEAD fallback used when a range probe yields no usable length.</summary>
    private async Task<(long Size, bool Supports)?> ProbeWithHeadAsync(
        string url, IReadOnlyDictionary<string, string> headers, CancellationToken ct)
    {
        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Head, url);
            foreach (var (k, v) in headers)
                req.Headers.TryAddWithoutValidation(k, v);
            using var resp = await _client.SendAsync(req, ct).ConfigureAwait(false);
            if (!resp.IsSuccessStatusCode)
                return null;

            var map = ToDictionary(resp);
            long len = 0;
            if (map.TryGetValue("Content-Length", out var cl) && long.TryParse(cl, out var headLen))
                len = headLen;
            var supports = map.TryGetValue("Accept-Ranges", out var ar)
                           && ar.Equals("bytes", StringComparison.OrdinalIgnoreCase);
            return (len, supports);
        }
        catch
        {
            return null;
        }
    }

    private static (long Size, bool Supports) ParseSize(
        Dictionary<string, string> headers, int status)
    {
        // A 206 reply carries the authoritative total in Content-Range. Never
        // fall back to Content-Length in that case: for a "bytes=0-0" probe
        // Content-Length is 1 (the single byte we asked for), which would make
        // the task think the whole file is 1 byte.
        var supports =
            headers.TryGetValue("Accept-Ranges", out var ar) &&
            ar.Equals("bytes", StringComparison.OrdinalIgnoreCase)
            || status == 206;

        long total = 0;
        var haveRangeTotal = false;
        if (headers.TryGetValue("Content-Range", out var cr))
        {
            var slash = cr.LastIndexOf('/');
            if (slash >= 0 && long.TryParse(cr[(slash + 1)..].Trim(), out var parsed))
            {
                total = parsed;
                haveRangeTotal = true;
            }
        }

        if (!haveRangeTotal &&
            headers.TryGetValue("Content-Length", out var cl) &&
            long.TryParse(cl, out var len))
            total = len;

        // "bytes */N" means unknown length.
        if (!haveRangeTotal && status == 206)
            total = 0;

        return (total, supports);
    }

    /// <summary>Stream a byte range. Yields chunks as they arrive.</summary>
    public async IAsyncEnumerable<byte[]> StreamAsync(
        string url,
        long start,
        long? end,
        IReadOnlyDictionary<string, string> headers,
        TimeSpan timeout,
        string? proxy,
        [System.Runtime.CompilerServices.EnumeratorCancellation] CancellationToken ct)
    {
        var current = url;
        // Per-read deadline, not a total-stream one: a 2 GB file over a slow
        // link legitimately takes hours, and the old CancelAfter(timeout)
        // aborted every such transfer after 30s no matter how healthy the
        // connection was. Python's http.client timeout is per socket read.
        using var timeoutCts = CancellationTokenSource.CreateLinkedTokenSource(ct);

        for (var hop = 0; hop <= MaxRedirects; hop++)
        {
            HttpResponseMessage? resp = null;
            try
            {
                using var req = BuildRequest(current, headers, start, end);
                // The client timeout is infinite (streaming reads must not be
                // bounded), so give the connect + response-headers phase its
                // own deadline. Once the body starts flowing only the per-read
                // deadline applies.
                using var sendCts = CancellationTokenSource.CreateLinkedTokenSource(timeoutCts.Token);
                sendCts.CancelAfter(timeout);
                resp = await _client.SendAsync(
                    req, HttpCompletionOption.ResponseHeadersRead, sendCts.Token);

                var status = (int)resp.StatusCode;
                if (status is 301 or 302 or 303 or 307 or 308)
                {
                    var loc = resp.Headers.Location;
                    current = loc is null
                        ? current
                        : new Uri(new Uri(current), loc).ToString();
                    continue;
                }
                if (status is not (200 or 206))
                    throw new HttpFailureException($"HTTP {status}");

                // Asked for a range but got the whole body: every worker would
                // then overwrite the file from its own offset and the final
                // size check would still pass (same total length) - silent
                // corruption. Python's transport treats this as an error too.
                if (status == 200 && end.HasValue)
                    throw new HttpFailureException("server ignored Range request (200)");

                var bytes = 64 * 1024;
                await using var stream = await resp.Content.ReadAsStreamAsync(timeoutCts.Token);
                var buffer = new byte[bytes];
                while (true)
                {
                    using var readCts = CancellationTokenSource.CreateLinkedTokenSource(timeoutCts.Token);
                    readCts.CancelAfter(timeout);
                    var read = await stream.ReadAsync(buffer.AsMemory(0, bytes), readCts.Token);
                    if (read <= 0)
                        yield break;
                    var chunk = new byte[read];
                    Array.Copy(buffer, chunk, read);
                    yield return chunk;
                }
            }
            finally
            {
                resp?.Dispose();
            }
        }

        throw new HttpFailureException("重定向次数过多");
    }

    public void Dispose()
    {
        _client.Dispose();
        GC.SuppressFinalize(this);
    }
}

/// <summary>Token-bucket limiter shared across a task (and optionally globally).</summary>
public sealed class RateLimiter
{
    private readonly object _gate = new();
    private long _rate;
    private double _tokens;
    private long _lastTicks;

    public RateLimiter(long bytesPerSecond)
    {
        _rate = bytesPerSecond;
        _tokens = bytesPerSecond;
        _lastTicks = DateTime.UtcNow.Ticks;
        Capacity = Math.Max(bytesPerSecond, 1);
    }

    private long Capacity { get; set; }

    public long Rate
    {
        get { lock (_gate) return _rate; }
        set
        {
            lock (_gate)
            {
                _rate = value;
                Capacity = Math.Max(value, 1);
                _tokens = Math.Min(_tokens, Capacity);
            }
        }
    }

    /// <summary>Block until <paramref name="count"/> bytes may be sent.</summary>
    public void Consume(long count)
    {
        long rate;
        lock (_gate) rate = _rate;
        if (rate <= 0) return;

        var remaining = count;
        while (remaining > 0)
        {
            long take;
            lock (_gate)
            {
                var now = DateTime.UtcNow.Ticks;
                _tokens += (now - _lastTicks) / (double)TimeSpan.TicksPerSecond * rate;
                _lastTicks = now;
                if (_tokens > Capacity)
                    _tokens = Capacity;
                take = (long)Math.Min(remaining, Math.Max(0, _tokens));
                _tokens -= take;
            }
            if (take <= 0)
            {
                Thread.Sleep(50);
                continue;
            }
            remaining -= take;
        }
    }
}

/// <summary>Rolling speed estimate over a five second window.</summary>
/// <summary>
/// Rolling throughput estimate.
///
/// The divisor has to be the time actually covered by the samples, which is
/// bounded by the window. Dividing by "now minus oldest sample" instead
/// inflates the divisor once the oldest sample nears the window edge while the
/// numerator still only holds part of the data — that reported 3 MB/s as
/// roughly 170 KB/s.
/// </summary>
public sealed class SpeedMeter
{
    private readonly object _gate = new();
    private readonly Queue<(double Time, long Bytes)> _samples = new();
    private readonly double _window;

    public long Total;

    public SpeedMeter(double window = 5.0) => _window = window;

    public void Add(long n)
    {
        if (n <= 0) return;
        lock (_gate)
        {
            Total += n;
            var now = Now();
            _samples.Enqueue((now, n));
            while (_samples.Count > 0 && now - _samples.Peek().Time > _window)
                _samples.Dequeue();
        }
    }

    private static double Now()
        => DateTime.UtcNow.Ticks / (double)TimeSpan.TicksPerSecond;

    public double Speed()
    {
        lock (_gate)
        {
            if (_samples.Count == 0) return 0;

            long sum = 0;
            foreach (var s in _samples) sum += s.Bytes;
            if (sum <= 0) return 0;

            var now = Now();
            var oldest = _samples.Peek().Time;
            var newest = _samples.Last().Time;

            // The divisor must be the time the samples actually cover, but the
            // queue only spans that time once enough wall clock has passed. Right
            // after a burst the samples are dense, so using their own span
            // over-reports; using (now - oldest) under-reports once the oldest
            // sample nears the window edge. Blend both and take the smaller
            // result, which is the conservative answer in both regimes.
            var observed = newest - oldest;
            var sinceOldest = now - oldest;

            double divisor;
            if (observed < 0.05)
            {
                // A single sample or an instantaneous burst: no trustworthy
                // span yet, so treat the sample as if it took the minimum time.
                divisor = 0.05;
            }
            else if (sinceOldest >= _window - 0.05)
            {
                // The window is full: observed span is the honest divisor.
                divisor = observed;
            }
            else
            {
                // Still filling the window; the true average is bounded by both.
                divisor = Math.Max(observed, Math.Min(sinceOldest, _window));
            }

            return sum / Math.Min(divisor, _window);
        }
    }

    public void Reset()
    {
        lock (_gate)
        {
            _samples.Clear();
            Total = 0;
        }
    }
}