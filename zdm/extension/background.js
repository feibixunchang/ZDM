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

/* ------------------------------------------------------------------ *
 * 媒体嗅探（v1.12.0）
 *
 * 判据放在 service worker 而不是 content script：只有 webRequest 拿得到
 * 响应的 Content-Type / Content-Length，而这两个头才是「这是不是一个
 * 可下载的媒体文件」的唯一可靠证据（页面里的 <video> 元素既可能是
 * blob: 也可能只是个播放器外壳）。
 *
 * 范围说明：这一版只认「直链媒体」（服务器直接吐字节流的 mp4/mp3/…）。
 * HLS(.m3u8) / DASH(.mpd) 是分片协议，需要的是「拉取+拼接+混流」引擎，
 * 属于下一版的范围，所以这里刻意不把它们算进来——列出一堆点不动
 * 的条目比不列更糟。
 * ------------------------------------------------------------------ */
const MEDIA_EXT =
  /\.(mp4|m4v|webm|mkv|mov|m4a|mp3|aac|flac|wav|ogg|oga|opus|wma|avi|wmv|flv|f4v|3gp|ts|m2ts)(?:$|[?#])/i;
const MEDIA_AUDIO_EXT = /\.(mp3|m4a|aac|flac|wav|ogg|oga|opus|wma)(?:$|[?#])/i;
const MIN_MEDIA_BYTES = 64 * 1024; // 小于这个的多半是图标、埋点、预告片
const MAX_PER_TAB = 60;

/** tabId -> [{url, kind, size, name, from}]（内存态，SW 被回收就重来） */
const snifferByTab = new Map();

function nameFromUrl(url, kind) {
  let name = "";
  try {
    name = decodeURIComponent(new URL(url).pathname.split("/").filter(Boolean).pop() || "");
  } catch (_) {
    /* ignore */
  }
  name = name.replace(/[\\/:*?"<>|]/g, "_").slice(0, 120);
  if (!name || !/\.[a-z0-9]{2,5}$/i.test(name)) {
    name = (name || "media") + (kind === "audio" ? ".mp3" : ".mp4");
  }
  return name;
}

/** 返回 {url, kind, size, name} 或 null（不是媒体）。 */
function classifyResponse(details) {
  if (details.tabId == null || details.tabId < 0) return null;
  if (details.statusCode !== 200) return null; // 每个重定向跳都会触发一次，只认最终 200
  const url = details.url || "";
  let ct = "";
  let len = 0;
  for (const h of details.responseHeaders || []) {
    const n = (h.name || "").toLowerCase();
    if (n === "content-type") ct = (h.value || "").toLowerCase().split(";")[0].trim();
    else if (n === "content-length") len = parseInt(h.value || "0", 10) || 0;
    else if (n === "content-range" && !len) {
      const m = /\/\s*(\d+)\s*$/.exec(h.value || "");
      if (m) len = parseInt(m[1], 10) || 0;
    }
  }
  let kind = null;
  if (ct.startsWith("video/")) kind = "video";
  else if (ct.startsWith("audio/")) kind = "audio";
  else if (
    (ct === "application/octet-stream" || ct === "binary/octet-stream" || ct === "") &&
    MEDIA_EXT.test(url)
  ) {
    kind = MEDIA_AUDIO_EXT.test(url) ? "audio" : "video";
  }
  if (!kind) return null;
  if (len && len < MIN_MEDIA_BYTES) return null;
  return { url, kind, size: len, name: nameFromUrl(url, kind), from: "net" };
}

function rememberMedia(tabId, item) {
  let list = snifferByTab.get(tabId);
  if (!list) {
    list = [];
    snifferByTab.set(tabId, list);
  }
  if (list.some((x) => x.url === item.url)) return false;
  list.push(item);
  if (list.length > MAX_PER_TAB) list.shift();
  return true;
}

async function notifyTab(tabId, reset) {
  const items = snifferByTab.get(tabId) || [];
  try {
    // reset=true 表示「换了页面，旧结果作废」，content script 收到后要
    // 清空而不是合并，否则新页面会继承上一个页面的媒体。
    await chrome.tabs.sendMessage(tabId, { type: "sniffer-update", items, reset: !!reset });
  } catch (_) {
    /* 页面没有注入 content script（chrome://、应用店、已关闭的标签） */
  }
}

chrome.webRequest.onResponseStarted.addListener(
  (details) => {
    const item = classifyResponse(details);
    if (!item) return;
    if (rememberMedia(details.tabId, item)) notifyTab(details.tabId);
  },
  { urls: ["http://*/*", "https://*/*"], types: ["main_frame", "sub_frame", "xmlhttprequest", "media", "object"] }
);

// 页面跳转后旧列表作废，否则新页面会继承上一个页面的嗅探结果
chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.url) {
    snifferByTab.delete(tabId);
    notifyTab(tabId, true);
  }
});
chrome.tabs.onRemoved.addListener((tabId) => snifferByTab.delete(tabId));

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
        cookie: req.cookie || undefined,
        connections: req.connections || undefined,
        dir: req.dir || undefined,
        source: req.source || undefined,
        kind: req.kind || undefined,
      });
    } catch (e) {
      finish(false);
    }

    // A host that never answers must not stall the handover forever. The host
    // itself may legitimately take ~20s to bring a cold client up, so give it
    // more than the old 5s (which expired on every cold start and always fell
    // through to the zdm:// escape hatch).
    setTimeout(() => finish(false), 15000);
  });
}

function buildProtocolUrl(req) {
  const q = new URLSearchParams();
  q.set("url", req.url);
  if (req.filename) q.set("filename", req.filename);
  if (req.referer) q.set("referer", req.referer);
  if (req.connections) q.set("connections", String(req.connections));
  if (req.dir) q.set("dir", req.dir);
  // 来源标记要能活着走到队列里：协议兜底是冷启动路径，客户端此时
  // 只认启动参数，丢掉 source 会让「浏览器接管」显示成「手动添加」。
  if (req.source) q.set("source", req.source);
  if (req.kind) q.set("kind", req.kind);
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
 * 首次运行被拦截），这时不能盲目取消浏览器自己的下载。
 *
 * ``since`` 只认这次交接之后新出现的任务：ZDM 会把最多 200 条历史完成任务
 * 恢复进列表，只比 URL 会把"上周下过的同一个地址"误判成本次已入列 ——
 * 浏览器下载被取消，新任务却从未创建，用户静默丢东西。 */
async function taskExists(url, since) {
  try {
    const s = await api("state", null, "GET");
    const items = (s && s.tasks) || [];
    return items.some(
      (t) =>
        t.url === url &&
        (since == null || (t.created_at || 0) >= since)
    );
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
  const since = Date.now() / 1000;

  const headers = {};
  if (req.referer) headers.Referer = req.referer;
  if (req.cookie) headers.Cookie = req.cookie;

  const post = () =>
    api("tasks", {
      url: req.url,
      filename: req.filename,
      connections: req.connections || cfg.connections,
      dir: req.dir || cfg.directory || undefined,
      headers,
      source: req.source || "browser",
      kind: req.kind || undefined,
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

  // 4. 正常情况下客户端已经从启动参数里取走了这条下载；只有确认队列里
  //    没有 *本次* 的新任务时才补一次 POST。早先版本在这里无条件补 POST，
  //    而 Windows 原生宿主是"先回 ok、任务随后才入列"，于是同一个地址
  //    会被加两遍。
  if (await taskExists(req.url, since)) return true;

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
    // The wake-up path must carry the user's options too, otherwise a cold
    // start silently falls back to the client's defaults (8 connections,
    // default directory) while a warm POST honoured them.
    connections: cfg.connections,
    dir: cfg.directory || undefined,
    source: "browser",
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
    source: "browser",
  });
  notify(ok ? "已交给 ZDM" : "ZDM 添加失败", ok ? url : "请在设置里重新注册浏览器接管");
});

/* ---- popup helpers ---- */
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
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
  // 媒体嗅探：content script 来要当前标签页已发现的媒体
  if (msg.type === "sniffer-list") {
    const tabId = sender.tab && sender.tab.id;
    sendResponse({ ok: true, items: (tabId != null && snifferByTab.get(tabId)) || [] });
    return false;
  }
  // 一键推送：走的是和「接管浏览器下载」完全相同的 handOff()，
  // 因此冷启动唤醒、原生消息、协议兜底的行为三者一致。
  if (msg.type === "sniffer-push") {
    const item = msg.item || {};
    handOff({
      url: item.url,
      filename: item.filename,
      referer: item.referer,
      cookie: item.cookie,
      source: item.source || "sniffer",
      kind: item.kind || "video",
    })
      .then((ok) => {
        sendResponse({ ok });
        if (ok) notify("已推送给 ZDM", item.filename || item.url);
      })
      .catch(() => sendResponse({ ok: false }));
    return true;
  }
});
