using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using Microsoft.Win32;

namespace Zdm.App;

/// <summary>
/// Native-messaging mode.
///
/// A browser extension cannot start a local program on its own, and the
/// <c>zdm://</c> protocol route it used before is silently blocked: Chrome only
/// launches an external protocol handler from a page that has a user gesture,
/// and a hidden tab we open ourselves never has one. That is why clicking a
/// download with ZDM closed did nothing at all.
///
/// Native messaging has no such restriction. The extension calls
/// <c>chrome.runtime.connectNative</c>, Chrome reads the host manifest out of
/// HKCU, and starts THIS executable with stdin/stdout wired to pipes. We read
/// one length-prefixed JSON message, hand it to the real window through the
/// <c>zdm://</c> shell association, answer, and exit — a host process lives in
/// Chrome's job object and is killed when the port closes.
///
/// Chrome passes no arguments to the host, so the mode is detected from the
/// stdin handle: a console-less GUI app started by Explorer never has a pipe
/// there, while a host started by Chrome always does.
/// </summary>
public static class NativeHost
{
    private const int StdInputHandle = -10;
    private const uint FileTypePipe = 2;

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr GetStdHandle(int nStdHandle);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint GetFileType(IntPtr hFile);

    private const uint Th32csSnapProcess = 0x00000002;

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr CreateToolhelp32Snapshot(uint dwFlags, uint th32ProcessId);

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern bool Process32FirstW(IntPtr hSnapshot, ref PROCESSENTRY32W entry);

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern bool Process32NextW(IntPtr hSnapshot, ref PROCESSENTRY32W entry);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr hObject);

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct PROCESSENTRY32W
    {
        public uint dwSize;
        public uint cntUsage;
        public uint th32ProcessID;
        public IntPtr th32DefaultHeapID;
        public uint th32ModuleID;
        public uint cntThreads;
        public uint th32ParentProcessID;
        public int pcPriClassBase;
        public uint dwFlags;

        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)]
        public string szExeFile;
    }

    /// <summary>Raw GetFileType of the stdin handle, for the startup log.</summary>
    public static string StdinType()
    {
        try
        {
            var h = GetStdHandle(StdInputHandle);
            return $"handle=0x{h.ToInt64():X} type={GetFileType(h)}";
        }
        catch (Exception ex)
        {
            return "error " + ex.Message;
        }
    }

    /// <summary>True when stdin is a pipe, i.e. a browser started us as a host.</summary>
    private static bool StdinIsPipe()
    {
        try
        {
            var h = GetStdHandle(StdInputHandle);
            if (h == IntPtr.Zero || h == new IntPtr(-1)) return false;
            return GetFileType(h) == FileTypePipe;
        }
        catch
        {
            return false;
        }
    }

    private static readonly string[] BrowserExes =
    {
        "chrome", "msedge", "brave", "chromium", "vivaldi", "opera", "thorium",
    };

    /// <summary>Name of the nearest ancestor, for the startup log.</summary>
    public static string AncestorChain()
    {
        var chain = new List<string>();
        var map = ProcessMap();
        var pid = (uint)Environment.ProcessId;
        for (var i = 0; i < 4; i++)
        {
            if (!map.TryGetValue(pid, out var entry)) break;
            chain.Add($"{entry.Name}({pid})");
            pid = entry.Parent;
            if (pid == 0) break;
        }
        return string.Join(" < ", chain);
    }

    private static bool AncestorIsBrowser()
    {
        var map = ProcessMap();
        var pid = (uint)Environment.ProcessId;
        for (var i = 0; i < 4; i++)
        {
            if (!map.TryGetValue(pid, out var entry)) return false;
            var name = entry.Name.ToLowerInvariant();
            if (BrowserExes.Any(b => name == b || name == b + ".exe")) return true;
            pid = entry.Parent;
            if (pid == 0) return false;
        }
        return false;
    }

    private static Dictionary<uint, (uint Parent, string Name)> ProcessMap()
    {
        var map = new Dictionary<uint, (uint, string)>();
        var snapshot = CreateToolhelp32Snapshot(Th32csSnapProcess, 0);
        if (snapshot == IntPtr.Zero || snapshot == new IntPtr(-1)) return map;

        try
        {
            var entry = new PROCESSENTRY32W { dwSize = (uint)Marshal.SizeOf<PROCESSENTRY32W>() };
            if (!Process32FirstW(snapshot, ref entry)) return map;

            do
            {
                map[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile ?? "");
                entry.dwSize = (uint)Marshal.SizeOf<PROCESSENTRY32W>();
            }
            while (Process32NextW(snapshot, ref entry));
        }
        catch
        {
            // An empty map simply means "no browser ancestor detected".
        }
        finally
        {
            CloseHandle(snapshot);
        }
        return map;
    }

    /// <summary>
    /// Decide whether this process was started by a browser as its messaging
    /// host.
    ///
    /// Chrome does NOT start a host with an empty command line — it passes the
    /// caller's origin, plus a few switches:
    /// <c>chrome-extension://&lt;id&gt;/ --parent-window=0</c>. That origin
    /// argument is the reliable marker. A redirected launch with no arguments
    /// at all is accepted too, but only when stdin is a pipe or a browser is an
    /// ancestor, so a plain desktop double-click never ends up here.
    ///
    /// A <c>zdm://</c> handover always carries the URI as an argument, which is
    /// what stops the launched window from re-entering host mode.
    /// </summary>
    public static bool LooksLikeHost(IEnumerable<string> args)
    {
        var list = args.Skip(1).Where(a => a.Length > 0).ToList();

        if (list.Any(a => a.StartsWith("chrome-extension://", StringComparison.OrdinalIgnoreCase)))
            return true;

        return list.Count == 0 && (StdinIsPipe() || AncestorIsBrowser());
    }

    /// <summary>
    /// Read the pending message as raw JSON text. Returns null when nothing
    /// usable arrived; the caller decides what it means.
    /// </summary>
    public static string? ReadRawMessage()
    {
        try
        {
            var stdin = Console.OpenStandardInput();
            var header = new byte[4];
            if (ReadFully(stdin, header, 3000) != 4) return null;

            var length = BitConverter.ToInt32(header, 0);
            if (length <= 0 || length > 1 << 20) return null;

            var body = new byte[length];
            if (ReadFully(stdin, body, 5000) != length) return null;

            return Encoding.UTF8.GetString(body).TrimEnd('\0');
        }
        catch
        {
            return null;
        }
    }

    /// <summary>
    /// Hand the request to the real UI process through the shell.
    ///
    /// This deliberately does NOT start the app as our child: Chrome puts a
    /// native-messaging host into a job object that is killed when the port
    /// closes, and a plain child process would die with us — taking the
    /// download with it. Going through ShellExecute makes Explorer the parent,
    /// so the app outlives this short-lived forwarder.
    /// </summary>
    public static bool LaunchViaShell(string uri)
    {
        // ShellExecute resolves the zdm:// association and starts the UI
        // process. It is deliberately not started as a plain child process:
        // Chrome may terminate the host (with anything it spawned) once the
        // port closes, and the shell launch keeps the download independent of
        // this short-lived forwarder.
        //
        // explorer.exe as a trampoline is NOT usable here: explorer only
        // accepts a bare scheme and silently drops any URI that carries a
        // query string, which is every real handover.
        try
        {
            var psi = new System.Diagnostics.ProcessStartInfo(uri) { UseShellExecute = true };
            System.Diagnostics.Process.Start(psi)?.Dispose();
            return true;
        }
        catch
        {
            return false;
        }
    }

    /// <summary>
    /// Blocking read with a deadline. A pipe that never delivers must not hang
    /// startup: after the timeout the app simply boots as a normal window.
    /// </summary>
    private static int ReadFully(Stream stream, byte[] buffer, int timeoutMs)
    {
        var task = Task.Run(() =>
        {
            var offset = 0;
            while (offset < buffer.Length)
            {
                var n = stream.Read(buffer, offset, buffer.Length - offset);
                if (n <= 0) break;
                offset += n;
            }
            return offset;
        });

        return task.Wait(timeoutMs) ? task.Result : 0;
    }

    public static void Reply(object payload)
    {
        try
        {
            var bytes = Encoding.UTF8.GetBytes(JsonSerializer.Serialize(payload));
            var stdout = Console.OpenStandardOutput();
            stdout.Write(BitConverter.GetBytes(bytes.Length), 0, 4);
            stdout.Write(bytes, 0, bytes.Length);
            stdout.Flush();
        }
        catch
        {
            // Chrome closing the pipe early is normal.
        }
    }
}

/// <summary>
/// Writes the native-messaging host manifest and points the browsers at it.
///
/// Everything lives under HKCU / %APPDATA%, so no administrator rights are
/// needed — the same constraint the protocol registration already works under.
/// Chrome, Edge and friends each have their own registry path for hosts.
/// </summary>
public static class NativeMessagingRegistrar
{
    public const string HostName = "com.zdm.launcher";

    /// <summary>
    /// Derived from the "key" embedded in the extension manifest, so it is the
    /// same on every machine and the host manifest can be written up front.
    /// </summary>
    public const string DefaultExtensionId = "mpodbnemeeghjpllgphpemhoogddfhjg";

    private static readonly string[] HostRoots =
    {
        @"Software\Google\Chrome\NativeMessagingHosts\",
        @"Software\Microsoft\Edge\NativeMessagingHosts\",
        @"Software\Chromium\NativeMessagingHosts\",
        @"Software\BraveSoftware\Brave-Browser\NativeMessagingHosts\",
    };

    public static string ManifestPath => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        "ZDM", HostName + ".json");

    /// <summary>Extension ids currently allowed to talk to the host.</summary>
    public static List<string> AllowedOrigins()
    {
        var ids = new List<string> { DefaultExtensionId };
        try
        {
            if (File.Exists(ManifestPath))
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(ManifestPath));
                if (doc.RootElement.TryGetProperty("allowed_origins", out var arr))
                {
                    foreach (var item in arr.EnumerateArray())
                    {
                        var s = item.GetString();
                        const string prefix = "chrome-extension://";
                        if (s is not null && s.StartsWith(prefix, StringComparison.Ordinal))
                        {
                            var id = s[prefix.Length..].TrimEnd('/');
                            if (id.Length == 32 && !ids.Contains(id)) ids.Add(id);
                        }
                    }
                }
            }
        }
        catch
        {
            // A corrupt manifest is simply rewritten below.
        }
        return ids;
    }

    public static bool IsRegistered()
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey
                (@"Software\Google\Chrome\NativeMessagingHosts\" + HostName);
            var value = key?.GetValue(null) as string;
            return !string.IsNullOrEmpty(value)
                   && File.Exists(ManifestPath)
                   && string.Equals(value, ManifestPath, StringComparison.OrdinalIgnoreCase);
        }
        catch
        {
            return false;
        }
    }

    /// <summary>
    /// Write the manifest and register it. Called on every start, because the
    /// exe path differs between a portable run and an installed copy.
    /// </summary>
    public static bool Register(string? additionalExtensionId = null)
    {
        try
        {
            var ids = AllowedOrigins();
            if (!string.IsNullOrWhiteSpace(additionalExtensionId)
                && additionalExtensionId.Length == 32
                && !ids.Contains(additionalExtensionId))
            {
                ids.Add(additionalExtensionId);
            }

            var manifest = new
            {
                name = HostName,
                description = "ZDM 下载管理器启动器",
                path = AutoStart.ExePath,
                type = "stdio",
                allowed_origins = ids.Select(id => $"chrome-extension://{id}/").ToArray(),
            };

            Directory.CreateDirectory(Path.GetDirectoryName(ManifestPath)!);
            // UTF-8 without BOM: Chrome refuses a manifest that starts with one.
            File.WriteAllText(ManifestPath, JsonSerializer.Serialize(manifest,
                new JsonSerializerOptions { WriteIndented = true }),
                new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));

            foreach (var root in HostRoots)
            {
                try
                {
                    using var key = Registry.CurrentUser.CreateSubKey(root + HostName);
                    key?.SetValue(null, ManifestPath);
                }
                catch
                {
                    // A browser that is not installed has no reason to accept it.
                }
            }
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
            if (File.Exists(ManifestPath)) File.Delete(ManifestPath);
        }
        catch
        {
            // best effort
        }

        foreach (var root in HostRoots)
        {
            try
            {
                Registry.CurrentUser.DeleteSubKeyTree(root + HostName, throwOnMissingSubKey: false);
            }
            catch
            {
                // best effort
            }
        }
    }
}
