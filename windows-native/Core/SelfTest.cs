/// <summary>
/// Drives the app through its own HTTP API from inside the process network stack.
/// </summary>
using System.Net.Http.Json;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Zdm.App.Core;

/// <summary>
/// Self-test entry: exercises the REST contract and a real segmented download.
/// Invoked with --selftest, writes a JSON report, then exits.
/// </summary>
public static class SelfTest
{
    private static string ReportPath => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        "ZDM", "selftest.json");

    public static async Task<int> RunAsync(int port)
    {
        var lines = new List<string>();
        var baseUrl = $"http://127.0.0.1:{port}";
        using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(30) };

        void Say(string s)
        {
            lines.Add(s);
            System.Diagnostics.Debug.WriteLine(s);
        }

        try
        {
            // 1) state
            var state = await client.GetFromJsonAsync<JsonObject>($"{baseUrl}/api/state");
            Say($"1) GET /api/state -> ok={state?["ok"]}");

            // 2) add a download
            // Prefer the local test server when it is up: it exercises Range handling
            // deterministically and is not subject to remote rate limits.
            var testUrl = Environment.GetEnvironmentVariable("ZDM_TEST_URL")
                          ?? "http://127.0.0.1:8899/payload.bin";
            var skipDownload = Environment.GetEnvironmentVariable("ZDM_SELFTEST_API_ONLY") == "1";
            var payload = new JsonObject
            {
                ["url"] = testUrl,
                ["filename"] = "zdm-native-selftest.bin",
                ["connections"] = 8,
            };
            var post = await client.PostAsync($"{baseUrl}/api/tasks",
                new StringContent(payload.ToJsonString(), Encoding.UTF8, "application/json"));
            var added = JsonNode.Parse(await post.Content.ReadAsStringAsync()) as JsonObject;
            var id = added?["id"]?.GetValue<string>();
            // The filename is only resolved after the server probe, so the
            // add-response still says "download"; the real name appears later.
            Say($"2) POST /api/tasks -> ok={added?["ok"]} id={id} " +
                $"(name pending probe: {added?["task"]?["filename"]})");

            if (string.IsNullOrEmpty(id)) throw new Exception("no task id returned");

            // API-only mode: skip the transfer. Used where no local source
            // server exists (CI), so the contract still gets exercised.
            if (skipDownload)
            {
                await Task.Delay(1200);
                var probe = await client.GetFromJsonAsync<JsonObject>($"{baseUrl}/api/state");
                var found = (probe?["tasks"] as JsonArray)?
                    .Any(n => n?["id"]?.GetValue<string>() == id) ?? false;
                Say($"3) api-only mode: task visible in state = {found}");
                Say(found ? "RESULT: PASS - api contract ok" : "RESULT: TASK MISSING");
                await client.DeleteAsync($"{baseUrl}/api/tasks/{id}?files=0");
                Say("END");
                try { File.WriteAllLines(ReportPath, lines); } catch { }
                return 0;
            }

            // 3) poll to completion
            var sw = System.Diagnostics.Stopwatch.StartNew();
            JsonObject? last = null;
            while (sw.Elapsed < TimeSpan.FromSeconds(90))
            {
                var st = await client.GetFromJsonAsync<JsonObject>($"{baseUrl}/api/state");
                var arr = st?["tasks"] as JsonArray;
                JsonObject? t = null;
                if (arr is not null)
                    foreach (var n in arr)
                        if (n?["id"]?.GetValue<string>() == id) t = n as JsonObject;

                if (t is not null)
                {
                    var status = t["status"]?.GetValue<string>();
                    var pct = t["progress"]?.GetValue<double>();
                    var got = t["downloaded"]?.GetValue<long>();
                    var size = t["size"]?.GetValue<long>();
                    if (last is null || last["downloaded"]?.GetValue<long>() != got)
                    {
                        Say($"   {status} {pct:F1}%  {got}/{size}  {t["speed"]} B/s");
                        last = t;
                    }
                    if (status is "completed" or "error" or "canceled")
                    {
                        if (status == "completed")
                        {
                            var dir = t["directory"]?.GetValue<string>();
                            var name = t["filename"]?.GetValue<string>();
                            var path = Path.Combine(dir!, name!);
                            var exists = File.Exists(path);
                            var actual = exists ? new FileInfo(path).Length : -1;
                            var expected = t["size"]?.GetValue<long>() ?? 0;
                            Say($"3) completed. file={exists} name={name} " +
                                $"actual={actual} expected={expected}");
                            Say(actual == expected ? "RESULT: PASS" : "RESULT: SIZE MISMATCH");
                        }
                        else
                        {
                            Say($"RESULT: FAIL status={status} error={t["error"]}");
                        }
                        break;
                    }
                }
                await Task.Delay(400);
            }

            // Poll one more time regardless: a fast download can finish between
            // two polls, and we still want to record the terminal state.
            var testSource = Path.Combine(
                Path.GetDirectoryName(Environment.ProcessPath) ?? AppContext.BaseDirectory,
                "payload.bin");
            if (!File.Exists(testSource))
                testSource = Path.Combine(AppContext.BaseDirectory, "payload.bin");

            var finalState = await client.GetFromJsonAsync<JsonObject>($"{baseUrl}/api/state");
            if (finalState?["tasks"] is JsonArray finalArr)
            {
                foreach (var n in finalArr)
                {
                    if (n?["id"]?.GetValue<string>() != id) continue;
                    var st = n["status"]?.GetValue<string>();
                    var nm = n["filename"]?.GetValue<string>();
                    var dr = n["directory"]?.GetValue<string>();
                    var p = Path.Combine(dr!, nm!);
                    var ok = File.Exists(p);
                    var sz = ok ? new FileInfo(p).Length : -1;
                    Say($"3) terminal: status={st} name={nm} file={ok} size={sz}");
                    if (ok && sz == n["size"]?.GetValue<long>())
                    {
                        // Segments write at scattered offsets, so compare bytes
                        // rather than trusting the reported total.
                        var src = File.ReadAllBytes(testSource);
                        var dst = File.ReadAllBytes(p);
                        var same = src.Length == dst.Length;
                        if (same)
                            for (var i = 0; i < src.Length; i++)
                                if (src[i] != dst[i]) { same = false; break; }
                        Say($"   byte-for-byte compare: {(same ? "IDENTICAL" : "DIFFERENT")}");
                        Say(same ? "RESULT: PASS - segmented output matches source"
                                 : "RESULT: CONTENT MISMATCH");
                    }
                    else if (ok) Say("RESULT: SIZE MISMATCH");
                    else Say("RESULT: FILE MISSING");
                }
            }

            // 4) remove the task from the list but keep the file for inspection
            var del = await client.DeleteAsync($"{baseUrl}/api/tasks/{id}?files=0");
            Say($"4) DELETE /api/tasks/{id}?files=0 -> {del.StatusCode}");
            Say("END");
        }
        catch (Exception ex)
        {
            Say($"EXCEPTION {ex.GetType().Name}: {ex.Message}");
            Say("RESULT: ERROR");
        }

        try { File.WriteAllLines(ReportPath, lines); } catch { }
        return 0;
    }
}