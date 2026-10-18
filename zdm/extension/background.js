/* ZDM Connector - background service worker */

const DEFAULTS = {
  serverUrl: "http://127.0.0.1:9000",
  intercept: true,
  connections: 8,
  directory: "",
  notify: true,
};

function getCfg() {
  return new Promise((res) => chrome.storage.sync.get(DEFAULTS, res));
}

async function api(path, body, method = "POST") {
  const cfg = await getCfg();
  const base = (cfg.serverUrl || DEFAULTS.serverUrl).replace(/\/+$/, "");
  const r = await fetch(base + "/api/" + path.replace(/^\/+/, ""), {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  return r.json();
}

function notify(title, message) {
  getCfg().then((cfg) => {
    if (!cfg.notify) return;
    chrome.notifications.create({
      type: "basic",
      iconUrl: "icons/icon48.png",
      title,
      message,
    });
  });
}

/* ---- take over browser downloads ---- */
chrome.downloads.onCreated.addListener(async (item) => {
  const cfg = await getCfg();
  if (!cfg.intercept) return;
  const url = item.url || "";
  if (!/^https?:\/\//i.test(url)) return; // blob:, data:, file:, chrome-extension:

  try {
    const res = await api("tasks", {
      url,
      filename: item.filename ? item.filename.split(/[\\/]/).pop() : undefined,
      connections: cfg.connections,
      dir: cfg.directory || undefined,
      headers: item.referrer ? { Referer: item.referrer } : {},
    });
    if (!res.ok) throw new Error(res.error || "rejected");
    await chrome.downloads.cancel(item.id);
    await chrome.downloads.erase({ id: item.id });
    notify("已交给 ZDM", item.filename || url);
  } catch (e) {
    // service unreachable -> let the browser finish the download itself
    console.warn("[ZDM] handoff failed:", e);
  }
});

/* ---- context menus ---- */
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: "zdm-download",
      title: "使用 ZDM 下载",
      contexts: ["link", "video", "audio", "image"],
    });
    chrome.contextMenus.create({
      id: "zdm-page",
      title: "使用 ZDM 下载本页资源",
      contexts: ["page"],
    });
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const cfg = await getCfg();
  const url = info.linkUrl || info.srcUrl || info.pageUrl;
  if (!url) return;
  try {
    const res = await api("tasks", {
      url,
      connections: cfg.connections,
      dir: cfg.directory || undefined,
      headers: tab && tab.url ? { Referer: tab.url } : {},
    });
    notify(res.ok ? "已交给 ZDM" : "ZDM 添加失败", res.ok ? url : res.error || "");
  } catch (e) {
    notify("ZDM 服务未启动", "请先在终端运行: zdm start --daemon");
  }
});

/* ---- popup helpers ---- */
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === "state") {
    api("state", null, "GET").then(sendResponse).catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (msg.type === "task-action") {
    api(`tasks/${msg.id}/${msg.action}`, {}).then(sendResponse).catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (msg.type === "open-ui") {
    getCfg().then((cfg) => {
      chrome.tabs.create({ url: (cfg.serverUrl || DEFAULTS.serverUrl).replace(/\/+$/, "") + "/" });
    });
    return true;
  }
});
