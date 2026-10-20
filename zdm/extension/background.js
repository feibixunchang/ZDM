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
 *
 * .m4s 必须算进来：B站的 DASH 视频轨/音频轨就是 .m4s 直链，Content-Type
 * 是 application/octet-stream —— 不认它，B站一个媒体都嗅不到。
 * ------------------------------------------------------------------ */
const MEDIA_EXT =
  /\.(mp4|m4v|webm|mkv|mov|m4a|mp3|aac|flac|wav|ogg|oga|opus|wma|avi|wmv|flv|f4v|3gp|ts|m2ts|m4s)(?:$|[?#])/i;
const MEDIA_AUDIO_EXT = /\.(mp3|m4a|aac|flac|wav|ogg|oga|opus|wma)(?:$|[?#])/i;
/* B站 DASH 轨的 URL 编号规则（实测）：
     视频轨  ".../62131_da3-1-30032.m4s"   —— 30000 + 清晰度id（30006…30127）
     音频轨  ".../62131_da3-1-30216.m4s"   —— 302xx 系列（30216/30232/30250/30251/30257/30259/30280）
   所以「≥30000 就是音频」是错的：那会把所有视频轨误判成音频。
   Content-Type 两者都是 application/octet-stream，只能从编号认。 */
const BILI_AUDIO_ID = /-(\d{5})\.m4s(?:$|[?#])/;
function isBiliAudioId(id) {
  return id >= 30200 && id < 30300;
}
const MIN_MEDIA_BYTES = 64 * 1024; // 小于这个的多半是图标、埋点、预告片
const MAX_PER_TAB = 60;
const MAX_TABS = 30; // storage.session 只有 10MB，别把页签全存爆

/** tabId -> [{url, kind, size, name, from}]（内存态，SW 被回收就重来） */
const snifferByTab = new Map();

/* MV3 的 service worker 空闲 30 秒就被回收，内存态全丢。存进
   chrome.storage.session（SW 重启仍在，关浏览器才清）之后：
   - 用户先打开页面、隔一会儿再播放，媒体照样列得出来；
   - SW 重启后 webRequest 监听器会随顶层代码重新注册，内容脚本的定时
     轮询负责把它唤醒（否则休眠期间漏掉整个播放过程）。 */
const STORE_KEY = "snifferByTab";
let restored = null;

function ensureRestored() {
  if (restored) return restored;
  restored = chrome.storage.session
    .get(STORE_KEY)
    .then((d) => {
      const m = d && d[STORE_KEY];
      if (m && typeof m === "object") {
        for (const [k, v] of Object.entries(m)) {
          const id = Number(k);
          if (Number.isFinite(id) && Array.isArray(v)) snifferByTab.set(id, v);
        }
      }
    })
    .catch(() => {
      /* 存储不可用就退回纯内存，功能降级但不炸 */
    });
  return restored;
}
ensureRestored();

function persist() {
  const obj = {};
  let n = 0;
  for (const [k, v] of snifferByTab) {
    if (n++ >= MAX_TABS) break;
    obj[k] = v;
  }
  try {
    chrome.storage.session.set({ [STORE_KEY]: obj }).catch(() => {});
  } catch (_) {
    /* 忽略 */
  }
}

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
  /* B站的 DASH 轨是 .m4s（分段 MP4）。存成 .m4s 用户认不出是什么，
     播放器也不认 —— 按音/视频轨分别落成 .mp4 / .m4a。 */
  if (/\.m4s$/i.test(name)) {
    name = name.replace(/\.m4s$/i, kind === "audio" ? ".m4a" : ".mp4");
  }
  return name;
}

/** 返回 {url, kind, size, name} 或 null（不是媒体）。 */
function classifyResponse(details) {
  if (details.tabId == null || details.tabId < 0) return null;
  /* 只认最终落地的那一次响应。200 是整段，206 是 Range 分段 ——
     视频站（B站/YouTube）播放时全是分段请求，只认 200 会把它们全滤掉。 */
  if (details.statusCode !== 200 && details.statusCode !== 206) return null;
  const url = details.url || "";
  let ct = "";
  let total = 0;   // 整个资源的大小（Content-Range: bytes x-y/TOTAL）
  let partLen = 0; // 这一次响应的大小（Content-Length）
  for (const h of details.responseHeaders || []) {
    const n = (h.name || "").toLowerCase();
    if (n === "content-type") ct = (h.value || "").toLowerCase().split(";")[0].trim();
    else if (n === "content-range") {
      // 只在没有完整长度时才用 Range 的总数
      const m = /\/\s*(\d+)\s*$/.exec(h.value || "");
      if (m && !total) total = parseInt(m[1], 10) || 0;
    } else if (n === "content-length") {
      partLen = parseInt(h.value || "0", 10) || 0;
    }
  }
  // Range 响应的 Content-Length 只是这一小段的长度（实测 B站是 1024），
  // 拿它当文件大小会把它误判成「小于 64KB 的埋点」而丢掉。
  const len = total || partLen;
  let kind = null;
  if (ct.startsWith("video/")) kind = "video";
  else if (ct.startsWith("audio/")) kind = "audio";
  else if (
    (ct === "application/octet-stream" || ct === "binary/octet-stream" || ct === "") &&
    MEDIA_EXT.test(url)
  ) {
    if (MEDIA_AUDIO_EXT.test(url)) {
      kind = "audio";
    } else {
      // 不用 RegExp.$1：它属于「最后一次 test」，前面几次 test 会把它覆盖掉，
      // 读出来的是别人的捕获组。用 match 拿到本次的结果。
      const m = BILI_AUDIO_ID.exec(url);
      kind = m && isBiliAudioId(Number(m[1])) ? "audio" : "video";
    }
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
  persist();
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

/* ------------------------------------------------------------------ *
 * 站点适配器（sites.js）注入
 *
 * sites.js 必须在页面的 MAIN world 里跑：它要读页面自己的全局变量
 * （B站 __playinfo__ / YouTube ytInitialPlayerResponse），而这些变量在
 * content script 的 isolated world 里读不到。
 *
 * 老做法是在 videobtn.js 里 document.createElement('script') 动态插一段
 * <script src="chrome-extension://.../sites.js">。**B站能过，YouTube 必挂**：
 * YouTube 的 CSP script-src 不含 chrome-extension:，注入直接被浏览器拦掉，
 * 控制台报 "Unsafe attempt to load URL chrome-extension://.../sites.js
 * from frame with URL https://www.youtube/" —— 表现就是 YouTube 上点
 * 「下载此视频」弹不出任何清晰度（而 B站一切正常，极具迷惑性）。
 *
 * chrome.scripting.registerContentScripts 的 world:"MAIN" 是浏览器原生
 * 注入通道，不走页面的 <script> 元素，**页面的 CSP 管不到它**。
 * document_start 执行：videobtn.js（document_idle）起来时桥接早就绪，
 * 不用再等注入。
 *
 * world 字段要 Chrome 102+（manifest 的 minimum_chrome_version 已相应上调）；
 * 老浏览器上这个调用会失败，videobtn.js 会退回 <script src> 注入兜底
 * （B站的 CSP 放行 chrome-extension:，所以老浏览器上 B站仍然可用）。
 *
 * 动态注册的内容脚本由浏览器持久化，SW 被回收/重启都不会丢；但扩展被
 * 重新加载（chrome://extensions 点刷新）时会清空，所以这里在**每次
 * SW 启动**都注册一遍，幂等（同 ID 同定义 = 覆盖写）。
 * ------------------------------------------------------------------ */
const SITES_SCRIPT_ID = "zdm-sites-main";
const SITES_MATCHES = [
  "*://*.bilibili.com/*",
  "*://*.bilivideo.com/*",
  "*://*.youtube.com/*",
  "*://*.youtu.be/*",
];

function registerSitesScript() {
  if (!chrome.scripting || !chrome.scripting.registerContentScripts) return;
  try {
    const p = chrome.scripting.registerContentScripts([
      {
        id: SITES_SCRIPT_ID,
        matches: SITES_MATCHES,
        js: [{ file: "sites.js" }],
        world: "MAIN",
        runAt: "document_start",
        allFrames: false,
      },
    ]);
    // 同 ID 重复注册是覆盖写、不报错；这里只兜「返回了 promise 但被拒」
    // （例如老浏览器不认 world 字段）。
    if (p && typeof p.catch === "function") p.catch(() => {});
  } catch (_) {
    /* 同步抛错同样只是「这条路不通」，由 videobtn.js 兜底 */
  }
}
registerSitesScript();
chrome.runtime.onInstalled.addListener(registerSitesScript);

chrome.webRequest.onResponseStarted.addListener(
  (details) => {
    const item = classifyResponse(details);
    if (!item) return;
    if (rememberMedia(details.tabId, item)) notifyTab(details.tabId);
  },
  { urls: ["http://*/*", "https://*/*"], types: ["main_frame", "sub_frame", "xmlhttprequest", "media", "object"] },
  // 没有这一段，Chrome 根本不会把 Content-Type / Content-Length /
  // Content-Range 放进 details —— 分类只能靠 URL 扩展名兜底，大小全是
  // 「未知」，64KB 噪音过滤也一起失效（YouTube 的点击音效全混进列表）。
  // 实测 B站 CDN 的 206 响应是带着 Content-Range: bytes 0-1023/17828788 的，
  // 只是扩展没收。
  ["responseHeaders"]
);

// 页面跳转后旧列表作废，否则新页面会继承上一个页面的嗅探结果
chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.url) {
    snifferByTab.delete(tabId);
    persist();
    notifyTab(tabId, true);
  }
});
chrome.tabs.onRemoved.addListener((tabId) => {
  snifferByTab.delete(tabId);
  persist();
});

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
        audio_url: req.audio_url || undefined,
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
  if (req.audio_url) q.set("audio_url", req.audio_url);
  // Cookie 也要活过协议跳转：需要登录的视频站，没有它就是 403。
  if (req.cookie) q.set("cookie", req.cookie);
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
      // DASH 的视频轨需要配套音频，后端会合并
      audio_url: req.audio_url || undefined,
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
  // 媒体嗅探：content script 来要当前标签页已发现的媒体。
  // 先等 storage.session 恢复完再答 —— SW 刚被唤醒时恢复可能还没完成，
  // 直接读 Map 会返回空，用户就看到「嗅探到 0 个」。
  if (msg.type === "sniffer-list") {
    const tabId = sender.tab && sender.tab.id;
    ensureRestored().then(() => {
      sendResponse({
        ok: true,
        items: (tabId != null && snifferByTab.get(tabId)) || [],
      });
    });
    return true;
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
      // DASH 视频轨的配套音频轨，后端下载后用 ffmpeg 合并
      audio_url: item.audio_url,
    })
      .then((ok) => {
        sendResponse({ ok });
        if (ok) notify("已推送给 ZDM", item.filename || item.url);
      })
      .catch(() => sendResponse({ ok: false }));
    return true;
  }
});
