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
  // 这个开关必须在「连不上」之前就读出来：早期版本把这段放在
  // `if (!ok) return` 之后，客户端没开时复选框永远显示成未勾选，
  // 看起来像「接管没打开」。
  const cfg = await new Promise((r) =>
    chrome.storage.sync.get({ intercept: true, serverUrl: "http://127.0.0.1:9000" }, r)
  );
  $("#intercept").checked = !!cfg.intercept;

  const st = await send({ type: "state" });
  const ok = st && st.ok;
  $("#dot").className = "dot " + (ok ? "on" : "off");
  $("#empty").hidden = ok;
  $("#port").textContent = ok ? "端口 " + (st.config.port || 9000) : "未连接";
  if (!ok) { $("#tasks").innerHTML = ""; }

  // 上次唤醒失败的原因（Chrome 的原话），排查时唯一有用的线索。
  chrome.storage.local.get({ lastNativeError: null }, (r) => {
    const e = r.lastNativeError;
    $("#diagRow").hidden = !e;
    if (e) {
      const when = new Date(e.at).toLocaleTimeString();
      $("#diag").textContent =
        `唤醒失败（${when}）：${e.msg}\n扩展 ID ${chrome.runtime.id}`;
    }
  });

  if (!ok) return;

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
$("#wake").onclick = async (e) => {
  const b = e.currentTarget;
  b.disabled = true;
  b.textContent = "正在启动…";
  await send({ type: "wake" });
  setTimeout(() => {
    b.disabled = false;
    b.textContent = "启动 ZDM";
    load();
  }, 2500);
};
$("#openUi").onclick = () => send({ type: "open-ui" });
$("#openOpts").onclick = () => chrome.runtime.openOptionsPage();

load();
setInterval(load, 1500);
