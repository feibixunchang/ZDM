/* ZDM Connector - background service worker */

const DEFAULTS = {
  serverUrl: "http://127.0.0.1:9000",
  intercept: true,
  connections: 8,
  directory: "",
  notify: true,
};

/* How long to wait for ZDM to finish starting after we wake it up. A cold
   start of the WPF client on a slow disk can take several seconds. */
const WAKE_TIMEOUT_MS = 20000;

function getCfg() {
  return new Promise((res) => chrome.storage.sync.get(DEFAULTS, res));
}

function baseUrl(cfg) {
  return (cfg.serverUrl || DEFAULTS.serverUrl).replace(/\/+$/, "");
}

async function api(path, body, method = "POST") {
  const cfg = await getCfg();
  const r = await fetch(baseUrl(cfg) + "/api/" + path.replace(/^\/+/, ""), {
    method,
    headers: {
      "Content-Type": "application/json",
      // Lets the client whitelist this extension id for native messaging even
      // if it was reloaded under a different id.
      "X-ZDM-Extension": chrome.runtime.id,
    },
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

/* ------------------------------------------------------------------ *
 * 唤醒 ZDM
 *
 * 首选「原生消息」(native messaging)：chrome.runtime.connectNative 会让
 * Chrome 直接按注册表里的 host 清单启动 Zdm.exe，不需要用户确认。
 *
 * 为什么不用 zdm:// 协议作为主路径：Chrome 只在「有用户手势」的页面上才
 * 允许跳转外部协议，而扩展自己开的隐藏标签页没有手势，跳转会被静默拦掉
 * —— 之前点了下载 ZDM 毫无反应就是这个原因。协议只作为兜底。
 * ------------------------------------------------------------------ */
const HOST_NAME = "com.zdm.launcher";

function launchViaNative(req) {
  return new Promise((resolve) => {
    let port;
    try {
      port = chrome.runtime.connectNative(HOST_NAME);
    } catch (e) {
      resolve(false);
      return;
    }

    let settled = false;
    const finish = (ok) => {
      if (settled) return;
      settled = true;
      try {
        port.disconnect();
      } catch (_) {
        /* already gone */
      }
      resolve(ok);
    };

    port.onMessage.addListener((m) => {
      if (m && m.ok) chrome.storage.local.remove("lastNativeError");
      finish(!!(m && m.ok));
    });
    port.onDisconnect.addListener(() => {
      // Reading lastError is what suppresses Chrome's console warning — and it
      // is the only place the real reason ("host not found", "forbidden")
      // surfaces, so keep it for the popup.
      const err = chrome.runtime.lastError;
      if (err && err.message) {
        chrome.storage.local.set({ lastNativeError: { msg: err.message, at: Date.now() } });
      }
      finish(false);
    });

    try {
      port.postMessage({
        url: req.url,
        filename: req.filename || undefined,
        referer: req.referer || undefined,
        connections: req.connections || undefined,
        dir: req.dir || undefined,
      });
    } catch (e) {
      finish(false);
    }

    // A host that never answers must not stall the handover forever.
    setTimeout(() => finish(false), 5000);
  });
}

function buildProtocolUrl(req) {
  const q = new URLSearchParams();
  q.set("url", req.url);
  if (req.filename) q.set("filename", req.filename);
  if (req.referer) q.set("referer", req.referer);
  if (req.connections) q.set("connections", String(req.connections));
  if (req.dir) q.set("dir", req.dir);
  return "zdm://add?" + q.toString();
}

async function launchZdm(req) {
  if (await launchViaNative(req)) return true;

  // 兜底：没有注册原生消息（或用户用的是不支持它的浏览器）时，
  // 开一个隐藏的扩展页，由它把地址栏跳到 zdm://。
  const target = req && req.url ? buildProtocolUrl(req) : "zdm://open";
  const launcher =
    chrome.runtime.getURL("launch.html") + "?u=" + encodeURIComponent(target);
  try {
    const tab = await chrome.tabs.create({ url: launcher, active: false });
    if (tab && tab.id != null) {
      setTimeout(() => {
        chrome.tabs.remove(tab.id).catch(() => {});
      }, 5000);
    }
    return true;
  } catch (e) {
    console.warn("[ZDM] 无法唤醒客户端:", e);
    return false;
  }
}

/* 轮询直到本机 API 有响应，说明 ZDM 已经起来了。 */
async function waitForServer(cfg, timeoutMs) {
  const base = baseUrl(cfg);
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const r = await fetch(base + "/api/state", { cache: "no-store" });
      if (r.ok) return true;
    } catch (_) {
      /* 还没起来，继续等 */
    }
    await new Promise((r) => setTimeout(r, 600));
  }
  return false;
}

/* 确认任务真的进了 ZDM 的队列。协议启动有可能失败（比如注册表被清理、
 * 首次运行被拦截），这时不能盲目取消浏览器自己的下载。 */
async function taskExists(url) {
  try {
    const s = await api("state", null, "GET");
    const items = (s && s.tasks) || [];
    return items.some((t) => t.url === url);
  } catch (_) {
    return false;
  }
}

/**
 * 把一次下载交给 ZDM，必要时先把客户端拉起来。
 * 返回 true 表示 ZDM 已经接手，浏览器这边可以取消了。
 */
async function handOff(req) {
  const cfg = await getCfg();

  const post = () =>
    api("tasks", {
      url: req.url,
      filename: req.filename,
      connections: req.connections || cfg.connections,
      dir: req.dir || cfg.directory || undefined,
      headers: req.referer ? { Referer: req.referer } : {},
    });

  // 1. 客户端已在运行：直接 POST。
  try {
    const res = await post();
    if (res && res.ok) return true;
  } catch (_) {
    /* 服务没起，走下面的唤醒流程 */
  }

  // 2. 没在运行：唤醒它。原生消息可能因为「扩展还没重新加载」而失败，
  //    这时立刻降级到协议跳转。唤醒是否成功不在这里判断 —— 以 HTTP
  //    服务能否连上为准，否则一次误报就会把下载白白放回浏览器。
  await launchZdm(req);

  // 3. 等它起来。
  if (!(await waitForServer(cfg, WAKE_TIMEOUT_MS))) {
    notify("ZDM 没有启动", "请在开始菜单里打开 ZDM 下载管理器，或在设置中重新注册浏览器接管。");
    return false;
  }

  // 4. 正常情况下客户端已经从启动参数里取走了这条下载；
  //    如果队列里没有，就再补一次 API 调用，避免两边都空手。
  if (await taskExists(req.url)) return true;

  try {
    const res = await post();
    return !!(res && res.ok);
  } catch (_) {
    return false;
  }
}

/* ---- take over browser downloads ---- */
chrome.downloads.onCreated.addListener(async (item) => {
  const cfg = await getCfg();
  if (!cfg.intercept) return;
  const url = item.url || "";
  if (!/^https?:\/\//i.test(url)) return; // blob:, data:, file:, chrome-extension:
  if (item.byExtensionId === chrome.runtime.id) return;

  const req = {
    url,
    filename: item.filename ? item.filename.split(/[\\/]/).pop() : undefined,
    referer: item.referrer || undefined,
  };

  // 先把浏览器这边的下载按住：唤醒 ZDM 要几秒，不按的话文件可能已经
  // 下完一半，白跑流量。这里用 pause 而不是 cancel —— 交接失败时必须
  // 能恢复，cancel 是不可逆的。
  let held = false;
  try {
    await chrome.downloads.pause(item.id);
    held = true;
  } catch (e) {
    /* 还没开始或已经结束，无需处理 */
  }

  let ok = false;
  try {
    ok = await handOff(req);
  } catch (e) {
    console.warn("[ZDM] handoff failed:", e);
  }

  if (!ok) {
    if (held) await chrome.downloads.resume(item.id).catch(() => {});
    return; // 交给浏览器自己下，别把用户的东西弄丢
  }

  try {
    await chrome.downloads.cancel(item.id);
    await chrome.downloads.erase({ id: item.id });
  } catch (e) {
    /* 已经结束 */
  }
  notify("已交给 ZDM", item.filename || url);
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
  const url = info.linkUrl || info.srcUrl || info.pageUrl;
  if (!url) return;
  const ok = await handOff({
    url,
    referer: tab && tab.url ? tab.url : undefined,
  });
  notify(ok ? "已交给 ZDM" : "ZDM 添加失败", ok ? url : "请在设置里重新注册浏览器接管");
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
  if (msg.type === "wake") {
    launchZdm({}).then((ok) => sendResponse({ ok }));
    return true;
  }
  if (msg.type === "open-ui") {
    getCfg().then((cfg) => {
      chrome.tabs.create({ url: baseUrl(cfg) + "/" });
    });
    return true;
  }
});
