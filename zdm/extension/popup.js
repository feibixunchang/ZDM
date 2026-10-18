const $ = (s) => document.querySelector(s);
const fmt = (n) => {
  n = Number(n) || 0;
  if (n < 1024) return n + " B";
  const u = ["KB", "MB", "GB", "TB"];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
  return n.toFixed(1) + " " + u[i];
};

function send(msg) {
  return new Promise((res) => chrome.runtime.sendMessage(msg, res));
}

async function load() {
  const st = await send({ type: "state" });
  const ok = st && st.ok;
  $("#dot").className = "dot " + (ok ? "on" : "off");
  $("#empty").hidden = ok;
  $("#port").textContent = ok ? "端口 " + (st.config.port || 9000) : "未连接";
  if (!ok) { $("#tasks").innerHTML = ""; return; }

  const cfg = await new Promise((r) =>
    chrome.storage.sync.get({ intercept: true, serverUrl: "http://127.0.0.1:9000" }, r)
  );
  $("#intercept").checked = cfg.intercept;

  const active = (st.tasks || []).filter((t) =>
    ["downloading", "queued", "paused", "merging", "error"].includes(t.status)
  ).slice(0, 6);
  $("#tasks").innerHTML = active.length
    ? active.map((t) => `
      <div class="task">
        <div class="name">${t.filename}</div>
        <div class="meta">${fmt(t.downloaded)} / ${t.size ? fmt(t.size) : "?"} · ${t.status === "downloading" ? fmt(t.speed) + "/s" : t.status}</div>
        <div class="bar"><i style="width:${Math.min(100, t.progress)}%"></i></div>
      </div>`).join("")
    : `<div class="empty">暂无进行中的任务</div>`;
}

$("#intercept").onchange = async (e) => {
  await chrome.storage.sync.set({ intercept: e.target.checked });
};
$("#openUi").onclick = () => send({ type: "open-ui" });
$("#openOpts").onclick = () => chrome.runtime.openOptionsPage();

load();
setInterval(load, 1500);
