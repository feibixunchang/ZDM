const DEFAULTS = {
  serverUrl: "http://127.0.0.1:9000",
  intercept: true,
  connections: 8,
  directory: "",
  notify: true,
};
const $ = (s) => document.querySelector(s);

chrome.storage.sync.get(DEFAULTS, (cfg) => {
  $("#serverUrl").value = cfg.serverUrl;
  $("#directory").value = cfg.directory;
  $("#connections").value = cfg.connections;
  $("#intercept").checked = cfg.intercept;
  $("#notify").checked = cfg.notify;
});

$("#save").onclick = () => {
  chrome.storage.sync.set({
    serverUrl: $("#serverUrl").value.trim() || DEFAULTS.serverUrl,
    directory: $("#directory").value.trim(),
    connections: Number($("#connections").value) || 8,
    intercept: $("#intercept").checked,
    notify: $("#notify").checked,
  }, () => { $("#msg").textContent = "已保存"; });
};

$("#test").onclick = async () => {
  const base = ($("#serverUrl").value.trim() || DEFAULTS.serverUrl).replace(/\/+$/, "");
  try {
    const r = await fetch(base + "/api/state", { cache: "no-store" });
    const j = await r.json();
    $("#msg").textContent = j.ok ? "连接成功 ✓" : "连接失败";
    $("#msg").style.color = j.ok ? "#16a34a" : "#dc2626";
  } catch (e) {
    $("#msg").textContent = "连接失败：服务未启动（终端运行 ndm start --daemon）";
    $("#msg").style.color = "#dc2626";
  }
};
