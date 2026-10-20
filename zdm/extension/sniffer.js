/* ZDM Connector - 媒体嗅探浮动面板（content script）
 *
 * 职责边界：
 *   - 本文件只负责「页面里的那一颗浮动按钮 + 媒体列表面板」；
 *   - 「哪些请求算媒体」的判断在 background.js（webRequest 看得到
 *     Content-Type / Content-Length，content script 看不到响应头）；
 *   - 「推送下载」复用 background.js 的 handOff()，因此冷启动唤醒、
 *     原生消息、协议兜底三端行为与接管浏览器下载完全一致。
 *
 * 为什么用 Shadow DOM：站点自己的 CSS（尤其 * {} 通配符和全局 z-index）
 * 会轻易把浮动按钮打回原形，隔离后我们只受自己这段样式约束。
 */

(() => {
  if (window.__zdmSnifferLoaded) return;
  window.__zdmSnifferLoaded = true;

  const MAX_ROWS = 60;
  let items = [];
  let panelOpen = false;
  let host = null;
  let shadow = null;
  let fab = null;
  let badge = null;
  let listEl = null;
  let titleEl = null;
  let lastDomScan = 0;

  const esc = (s) =>
    String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );

  const fmtSize = (n) => {
    n = Number(n) || 0;
    if (!n) return "大小未知";
    if (n < 1024) return n + " B";
    const u = ["KB", "MB", "GB", "TB"];
    let i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
    return n.toFixed(1) + " " + u[i];
  };

  const send = (msg) =>
    new Promise((res) => {
      try {
        chrome.runtime.sendMessage(msg, (r) => {
          void chrome.runtime.lastError; // 抑制 "receiving end does not exist"
          res(r);
        });
      } catch (_) {
        res(null);
      }
    });

  /* 从 URL 猜一个像样的文件名：取最后一段，去掉查询串，补常见扩展名。 */
  function guessName(url, kind) {
    let name = "";
    try {
      const u = new URL(url);
      name = decodeURIComponent(u.pathname.split("/").filter(Boolean).pop() || "");
    } catch (_) {
      name = "";
    }
    name = name.replace(/[\\/:*?"<>|]/g, "_").slice(0, 120);
    if (!name || !/\.[a-z0-9]{2,5}$/i.test(name)) {
      name = (name || "media") + (kind === "audio" ? ".mp3" : ".mp4");
    }
    return name;
  }

  /* 只把同站（含子域）的 cookie 带过去：媒体常在 CDN 域名上，
     把第一方 cookie 无差别发给任意第三方是没必要的泄露。 */
  function sameSite(mediaUrl, pageUrl) {
    try {
      const a = new URL(mediaUrl).hostname.toLowerCase();
      const b = new URL(pageUrl).hostname.toLowerCase();
      return a === b || a.endsWith("." + b) || b.endsWith("." + a);
    } catch (_) {
      return false;
    }
  }

  /* 页面里已经躺着的 <video>/<audio>/<source>：webRequest 只能看到
     「加载之后」的响应，首屏直链媒体靠这一步补上。 */
  function scanDom() {
    const out = [];
    const page = location.href;
    const nodes = document.querySelectorAll(
      'video[src],audio[src],video>source[src],audio>source[src],video[data-src],audio[data-src]'
    );
    for (const el of nodes) {
      const raw = el.getAttribute("src") || el.getAttribute("data-src") || "";
      if (!raw || raw.startsWith("blob:") || raw.startsWith("data:")) continue;
      let abs;
      try {
        abs = new URL(raw, page).href;
      } catch (_) {
        continue;
      }
      if (!/^https?:/i.test(abs)) continue;
      const tag = el.tagName.toLowerCase();
      const type = (el.getAttribute("type") || "").toLowerCase();
      out.push({
        url: abs,
        kind: tag === "audio" || type.startsWith("audio/") ? "audio" : "video",
        size: 0,
        name: guessName(abs, tag === "audio" ? "audio" : "video"),
        from: "dom",
      });
    }
    return out;
  }

  /* DOM 扫描节流：MutationObserver 可能在 1 秒内触发几十次，
     视频站的媒体响应也能连着来一串，每次都全量扫 DOM 太浪费。
     超过 800ms 才真扫一次，其余时候返回 null（表示「这次没扫」）。 */
  function scanDomThrottled(force) {
    const now = Date.now();
    if (!force && now - lastDomScan <= 800) return null;
    lastDomScan = now;
    return scanDom();
  }

  function merge(network, dom) {
    const seen = new Set();
    const out = [];
    // background 的网络嗅探结果优先（带 Content-Type / 大小），
    // DOM 扫描的结果补充在后面（首屏直链媒体在 webRequest 之前就加载完了）。
    for (const it of (network || []).concat(dom || []).concat(items)) {
      if (!it || !it.url || seen.has(it.url)) continue;
      seen.add(it.url);
      out.push(it);
      if (out.length >= MAX_ROWS) break;
    }
    items = out;
  }

  function build() {
    host = document.createElement("div");
    host.id = "zdm-sniffer-host";
    // 固定定位但挂在 body 上：avoid position:fixed 与站点 transform/contain
    // 的包含块陷阱（那颗按钮会跟着某个容器一起被裁掉）。
    host.style.cssText =
      "all:initial;position:fixed;right:18px;bottom:18px;z-index:2147483647;";
    shadow = host.attachShadow({ mode: "open" });
    shadow.innerHTML = `
      <style>
        :host{all:initial}
        .fab{all:initial;display:flex;align-items:center;gap:6px;height:40px;
          padding:0 14px 0 12px;border-radius:999px;cursor:pointer;
          background:#2563eb;color:#fff;font:600 13px/1 -apple-system,BlinkMacSystemFont,
          "Segoe UI","Noto Sans CJK SC",sans-serif;border:none;
          box-shadow:0 4px 14px rgba(37,99,235,.4);transition:.15s;font-family:inherit}
        .fab:hover{background:#1d4fd8}
        .fab svg{width:17px;height:17px;flex:none}
        .badge{all:initial;min-width:18px;height:18px;padding:0 5px;border-radius:999px;
          background:#fff;color:#2563eb;font:700 11px/18px -apple-system,sans-serif;
          text-align:center;font-family:inherit}
        .fab[data-empty="1"]{background:#475569;box-shadow:0 4px 14px rgba(15,23,42,.28)}
        .fab[data-empty="1"] .badge{background:transparent;color:#fff}
        .panel{all:initial;display:block;position:absolute;right:0;bottom:48px;width:336px;
          max-height:min(62vh,460px);overflow:hidden;border-radius:12px;background:#fff;
          border:1px solid #e3e8ef;box-shadow:0 18px 50px rgba(15,23,42,.22);
          font:13px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans CJK SC",sans-serif;
          color:#1f2937;font-family:inherit}
        .panel[hidden]{display:none}
        .hd{display:flex;align-items:center;gap:8px;padding:11px 13px;
          border-bottom:1px solid #eef1f5;font-weight:700;font-size:13px}
        .hd .dot{width:7px;height:7px;border-radius:50%;background:#2563eb;flex:none}
        .hd .x{margin-left:auto;all:initial;cursor:pointer;color:#94a3b8;font-size:16px;
          line-height:1;padding:2px 4px;border-radius:6px;font-family:inherit}
        .hd .x:hover{background:#f1f5f9;color:#475569}
        .list{max-height:min(46vh,340px);overflow-y:auto;margin:0;padding:0;list-style:none}
        .row{display:flex;align-items:center;gap:9px;padding:9px 13px;
          border-bottom:1px solid #f4f6f9}
        .row:last-child{border-bottom:none}
        .ico{all:initial;width:30px;height:30px;border-radius:8px;background:#eff4ff;
          color:#2563eb;display:flex;align-items:center;justify-content:center;flex:none}
        .ico svg{width:16px;height:16px}
        .meta{all:initial;min-width:0;flex:1;display:block;font-family:inherit}
        .nm{all:initial;display:block;font-weight:600;font-size:12.5px;color:#1f2937;
          white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-family:inherit}
        .sub{all:initial;display:block;font-size:11px;color:#6b7280;margin-top:1px;
          white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-family:inherit}
        .go{all:initial;flex:none;cursor:pointer;border:none;border-radius:7px;
          background:#2563eb;color:#fff;font:600 12px/1 -apple-system,sans-serif;
          padding:7px 11px;font-family:inherit}
        .go:hover{background:#1d4fd8}
        .go[disabled]{background:#e2e8f0;color:#94a3b8;cursor:default}
        .ft{padding:9px 13px;border-top:1px solid #eef1f5;font-size:11px;color:#94a3b8;
          background:#fafbfc}
        .empty{padding:26px 16px;text-align:center;color:#94a3b8;font-size:12.5px}
        @media (prefers-color-scheme: dark){
          .panel{background:#1e293b;border-color:#334155;color:#e2e8f0}
          .hd{border-bottom-color:#334155}
          .hd .x:hover{background:#334155;color:#e2e8f0}
          .row{border-bottom-color:#293548}
          .ico{background:#1e3a8a33;color:#93c5fd}
          .nm{color:#e2e8f0}
          .sub{color:#94a3b8}
          .ft{background:#172033;border-top-color:#334155;color:#64748b}
          .fab[data-empty="1"]{background:#334155}
        }
      </style>
      <button class="fab" type="button" title="ZDM 嗅探到的媒体">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round" stroke-linejoin="round">
          <path d="m10 8 6 4-6 4V8z"/><rect x="2" y="4" width="20" height="16" rx="2"/>
        </svg>
        <span>嗅探</span><span class="badge">0</span>
      </button>
      <div class="panel" hidden>
        <div class="hd"><span class="dot"></span><span class="t">检测到媒体</span>
          <button class="x" type="button" title="关闭">✕</button></div>
        <ul class="list"></ul>
        <div class="ft">由 ZDM 浏览器扩展提供 · HLS / DASH 自适应流将在后续版本支持</div>
      </div>`;
    (document.documentElement || document.body).appendChild(host);
    fab = shadow.querySelector(".fab");
    badge = shadow.querySelector(".badge");
    listEl = shadow.querySelector(".list");
    titleEl = shadow.querySelector(".hd .t");
    const panel = shadow.querySelector(".panel");

    fab.addEventListener("click", () => {
      panelOpen = !panelOpen;
      panel.hidden = !panelOpen;
      if (panelOpen) renderList();
    });
    shadow.querySelector(".hd .x").addEventListener("click", () => {
      panelOpen = false;
      panel.hidden = true;
    });
    document.addEventListener("click", (e) => {
      if (!panelOpen) return;
      if (host.contains(e.target)) return;
      panelOpen = false;
      panel.hidden = true;
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && panelOpen) {
        panelOpen = false;
        panel.hidden = true;
      }
    });
  }

  function renderFab() {
    if (!fab) return;
    const n = items.length;
    badge.textContent = n > 99 ? "99+" : String(n);
    fab.dataset.empty = n ? "0" : "1";
    fab.title = n ? `ZDM 嗅探到 ${n} 个媒体` : "ZDM 嗅探（暂无媒体）";
    if (titleEl) titleEl.textContent = n ? `检测到 ${n} 个媒体` : "暂无媒体";
  }

  function renderList() {
    if (!listEl) return;
    if (!items.length) {
      listEl.innerHTML =
        '<div class="empty">还没有嗅探到媒体<br>播放或刷新页面后会自动出现</div>';
      return;
    }
    listEl.innerHTML = items
      .map(
        (it, i) => `
        <li class="row">
          <span class="ico">${
            it.kind === "audio"
              ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M9 18V5l10-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="16" cy="16" r="3"/></svg>'
              : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="m10 8 6 4-6 4V8z"/><rect x="2" y="4" width="20" height="16" rx="2"/></svg>'
          }</span>
          <span class="meta">
            <span class="nm">${esc(it.name || guessName(it.url, it.kind))}</span>
            <span class="sub">${esc(it.kind === "audio" ? "音频" : "视频")} · ${esc(
          fmtSize(it.size)
        )}</span>
          </span>
          <button class="go" type="button" data-i="${i}">推送</button>
        </li>`
      )
      .join("");
  }

  const listClick = (e) => {
    const b = e.target.closest("button.go");
    if (!b || !listEl) return;
    const it = items[Number(b.dataset.i)];
    if (!it) return;
    b.disabled = true;
    b.textContent = "…";
    pushOne(it)
      .then((ok) => {
        b.textContent = ok ? "已推送" : "失败";
        if (ok) {
          const i = items.indexOf(it);
          if (i >= 0) items.splice(i, 1);
          setTimeout(renderList, 700);
          renderFab();
        } else {
          b.disabled = false;
          b.textContent = "重试";
        }
      })
      .catch(() => {
        b.disabled = false;
        b.textContent = "重试";
      });
  };

  async function pushOne(it) {
    const page = location.href;
    const body = {
      url: it.url,
      filename: it.name || guessName(it.url, it.kind),
      referer: page,
      // 只有同站才带 cookie，见 sameSite() 的说明
      cookie: sameSite(it.url, page) ? document.cookie || undefined : undefined,
      source: "sniffer",
      kind: it.kind || "video",
    };
    const r = await send({ type: "sniffer-push", item: body });
    return !!(r && r.ok);
  }

  async function refresh(forceDom) {
    const dom = scanDomThrottled(forceDom);
    const r = await send({ type: "sniffer-list" });
    merge((r && r.items) || [], dom);
    renderFab();
    if (panelOpen) renderList();
  }

  function observe() {
    let timer = null;
    const mo = new MutationObserver(() => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => refresh(true), 700);
    });
    mo.observe(document.documentElement, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["src", "data-src"],
    });
  }

  function start() {
    if (!document.documentElement) {
      // document_idle 基本不会走到这里，防御一下
      setTimeout(start, 200);
      return;
    }
    build();
    if (listEl) listEl.addEventListener("click", listClick);
    renderFab();
    refresh(true);
    observe();

    chrome.runtime.onMessage.addListener((msg) => {
      if (msg && msg.type === "sniffer-update") {
        // background 只知道自己看见的「网络响应」那一部分；页面里已经躺着的
        // <video>/<audio>（首屏直链媒体，webRequest 可能压根没经过）是我们自己
        // 扫 DOM 得到的。所以这里只能「合并」，不能整体覆盖——直接
        // items = msg.items 会把 DOM 扫描到的首屏媒体全部冲掉。
        if (msg.reset) items = [];
        merge(msg.items || [], scanDomThrottled(false));
        renderFab();
        if (panelOpen) renderList();
      }
      if (msg && msg.type === "sniffer-ping") {
        send({ type: "sniffer-list" }).then((r) => {
          merge((r && r.items) || [], scanDomThrottled(true));
          renderFab();
          if (panelOpen) renderList();
        });
      }
    });
  }

  start();
})();
