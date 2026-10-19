namespace Zdm.App.Core;

/// <summary>
/// Splits a file into ranges and hands work to idle connections. Mirrors the
/// Python Segmenter, including the "keep halving the biggest chunk" rule that
/// gives extra throughput on fast links without a fixed connection count.
/// </summary>
public sealed class Segmenter
{
    private const long MinSplit = 1 << 20; // 1 MiB

    private readonly object _gate = new();
    private readonly List<Chunk> _chunks;

    public long Size { get; }

    public Segmenter(long size, List<Chunk>? chunks = null)
    {
        Size = size;
        _chunks = chunks is { Count: > 0 } ? chunks : new List<Chunk> { new(0, size) };
    }

    public IReadOnlyList<Chunk> Chunks
    {
        get { lock (_gate) return _chunks.ToArray(); }
    }

    /// <summary>Next range for <paramref name="owner"/>, or null when idle.</summary>
    public Chunk? GetWork(int owner)
    {
        lock (_gate)
        {
            Chunk? free = null;
            Chunk? biggest = null;

            foreach (var c in _chunks)
            {
                if (c.Pos >= c.End) continue;
                if (c.Owner is null) { free = c; break; }
                if (biggest is null || c.Remaining > biggest.Remaining)
                    biggest = c;
            }

            if (free is not null)
            {
                free.Owner = owner;
                return free;
            }

            // Everyone is busy: carve the largest chunk in half so a fast
            // connection is never idle while work remains.
            if (biggest is not null && biggest.Remaining >= MinSplit * 2)
            {
                var mid = biggest.Pos + biggest.Remaining / 2;
                var tail = new Chunk(mid, biggest.End, mid);
                biggest.End = mid;
                var idx = _chunks.IndexOf(biggest);
                _chunks.Insert(idx + 1, tail);
                tail.Owner = owner;
                return tail;
            }
            return null;
        }
    }

    public void Release(Chunk chunk)
    {
        lock (_gate) chunk.Owner = null;
    }

    public void Complete(Chunk chunk)
    {
        lock (_gate)
        {
            chunk.Pos = chunk.End;
            chunk.Owner = null;
        }
    }

    public long Downloaded()
    {
        lock (_gate)
        {
            long sum = 0;
            foreach (var c in _chunks)
                sum += Math.Min(c.Pos, c.End) - c.Start;
            return sum;
        }
    }

    public bool IsComplete()
    {
        lock (_gate)
        {
            if (Size <= 0) return false;
            foreach (var c in _chunks)
                if (c.Pos < c.End) return false;
            return true;
        }
    }

    public long[][] Snapshot()
    {
        lock (_gate)
        {
            var arr = new long[_chunks.Count][];
            for (var i = 0; i < _chunks.Count; i++)
                arr[i] = _chunks[i].ToArray();
            return arr;
        }
    }

    /// <summary>Pre-split into N ranges so each connection starts with work.</summary>
    public void Balance(int connections)
    {
        if (Size <= 0 || connections <= 1) return;
        lock (_gate)
        {
            if (_chunks.Count > 1) return;
            var step = Size / connections;
            if (step < MinSplit) return;

            var next = new List<Chunk>(connections);
            for (var i = 0; i < connections; i++)
            {
                var start = i * step;
                var end = i == connections - 1 ? Size : (i + 1) * step;
                next.Add(new Chunk(start, end));
            }
            _chunks.Clear();
            _chunks.AddRange(next);
        }
    }
}