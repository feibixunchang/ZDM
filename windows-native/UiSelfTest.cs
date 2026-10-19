using System.Text.Json.Nodes;
using Zdm.App.Core;

namespace Zdm.App;

/// <summary>
/// UI-level self check: proves the row-hit resolution and the shell launch
/// actually work, which is what the context menu depends on. Run with
/// --uitest; writes %APPDATA%\ZDM\uitest.json.
/// </summary>
public static class UiSelfTest
{
    public static string ReportPath => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        "ZDM", "uitest.json");

    public static void Run(Manager manager)
    {
        var log = new List<string>();

        void Say(string s)
        {
            log.Add(s);
            System.Diagnostics.Debug.WriteLine(s);
        }

        // Always write the report, even if a step throws: a missing report is
        // indistinguishable from a crash and wastes a debugging round.
        try
        {
            // 1) is there a completed task whose file exists?
            var done = manager.All()
                .FirstOrDefault(d => d.Status == TaskStatus.Completed
                                     && File.Exists(Path.Combine(d.Directory, d.Filename)));
            if (done is null)
            {
                Say("no completed task with an existing file; skipping open test");
                Say($"tasks seen: {manager.All().Count}");
                foreach (var d in manager.All())
                    Say($"  - {d.Filename} [{TaskStatusNames.ToApi(d.Status)}]");
            }
            else
            {
                var path = Path.Combine(done.Directory, done.Filename);
                Say($"1) target file exists: {path}");
                var ok = Human.OpenFile(path);
                Say($"2) Human.OpenFile -> {(ok ? "OK" : "FAILED")}");
            }

            // 2) directory open
            var dir = manager.Config.DownloadDir;
            Say($"3) download dir exists: {Directory.Exists(dir)} ({dir})");
            var okDir = Human.OpenInExplorer(dir);
            Say($"4) Human.OpenInExplorer(dir) -> {(okDir ? "OK" : "FAILED")}");

            // 3) reveal a file
            if (done is not null)
            {
                var okSel = Human.OpenInExplorer(Path.Combine(done.Directory, done.Filename));
                Say($"5) OpenInExplorer(file) -> {(okSel ? "OK" : "FAILED")}");
            }
        }
        catch (Exception ex)
        {
            Say($"EXCEPTION {ex.GetType().Name}: {ex.Message}");
        }

        Say("END");
        try { File.WriteAllLines(ReportPath, log); } catch { }
    }
}
