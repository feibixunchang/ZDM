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

$("#test").onclick = () => {
  // Go through the background page: it holds the API token. A direct fetch
  // from an options tab would now get 401.
  chrome.runtime.sendMessage({ type: "state" }, (j) => {
    const ok = j && j.ok && !chrome.runtime.lastError;
    $("#msg").textContent = ok
      ? "连接成功 ✓"
      : "连接失败：ZDM 未运行（点浏览器里的下载会自动唤醒它，也可手动打开 ZDM 一次）";
    $("#msg").style.color = ok ? "#16a34a" : "#dc2626";
  });
};
