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

  /* approx=true 表示这个字节数是按「码率 × 时长」估出来的 —— YouTube 的
     SABR 播放器有时只报当前那一小段的长度，量不出整段就只好估。加个 ≈
     是为了不让人把它当成精确值（以前直接把碎片长度当整段显示）。 */
  const fmtSize = (n, approx) => {
    n = Number(n) || 0;
    if (!n) return "大小未知";
    const mark = approx ? "≈ " : "";
    if (n < 1024) return mark + n + " B";
    const u = ["KB", "MB", "GB", "TB"];
    let i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
    return mark + n.toFixed(1) + " " + u[i];
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
      const ext = kind === "audio" ? ".mp3" : kind === "image" ? ".jpg" : ".mp4";
      name = (name || "media") + ext;
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
     「加载之后」的响应，首屏直链媒体靠这一步补上。

     大小从 Resource Timing 拿：浏览器自己加载过的资源，
     performance.getEntriesByName(url)[0].encodedBodySize 就是真实字节数。
     跨域资源没带 Timing-Allow-Origin 时这两个字段是 0，那就只能显示未知
     （真正的下载大小由 ZDM 下载时才知道）。 */
  function timingSize(url) {
    try {
      const e = performance.getEntriesByName(url)[0];
      if (!e) return 0;
      return e.encodedBodySize || e.transferSize || 0;
    } catch (_) {
      return 0;
    }
  }

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
        size: timingSize(abs),
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

  /* 已推送过的条目：推送后条目会从列表里消失，但 background 那边的记录还在，
     下一次刷新又会把它带回来。这里在本地记一笔，让推过的别再冒出来。 */
  const pushed = new Set();

  /* 按需扫出来的图片单独放一份：网络嗅探刻意不收图片（否则每个页面的
     图标、头像、广告图会把列表淹掉），只有用户点「扫图片」才有。 */
  const imgItems = [];

  /* 条目身份。YouTube 的阶梯行每一条都指向同一个页面地址（清晰度在选择器
     里），只看 url 会把 2160/1080/720 合成一条；带 key 的用 key。 */
  const keyOf = (it) => (it && (it.key || it.url)) || "";

  function merge(network, dom, local) {
    const seen = new Map(); // key -> 已收录的那条
    const out = [];
    // background 的网络嗅探结果优先（带 Content-Type / 大小），
    // DOM 扫描的结果补充在后面（首屏直链媒体在 webRequest 之前就加载完了）。
    // local 是「只在内容脚本这一侧的条目」——按需扫出来的图片。
    // 换页之后 background 的列表已经清了，所以 local 必须由调用方决定
    // 要不要拼回来，不能无条件拼（否则新页面会继承上一个页面的媒体）。
    const src = (network || []).concat(dom || []).concat(local || []);
    for (const it of src) {
      if (!it || !it.url || pushed.has(keyOf(it))) continue;
      const k = keyOf(it);
      const prev = seen.get(k);
      if (prev) {
        // 同一条又来了一次（网络嗅探 vs DOM 扫描）。保留信息更全的
        // 那条：有大小 > 没大小。不然先来的 size:0 会把后来的真实大小
        // 挡在门外，列表里就一直显示「大小未知」。
        if (!prev.size && it.size) {
          out[out.indexOf(prev)] = it;
          seen.set(k, it);
        }
        continue;
      }
      seen.set(k, it);
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
          background:#fafbfc;display:flex;align-items:center;gap:8px}
        .ft .sp{flex:1;min-width:0}
        .ftbtn{all:initial;cursor:pointer;border:none;border-radius:6px;background:#eef2ff;
          color:#3730a3;font:600 11px/1 -apple-system,sans-serif;padding:5px 8px;font-family:inherit}
        .ftbtn:hover{background:#e0e7ff}
        .ftbtn[disabled]{background:#f1f5f9;color:#cbd5e1;cursor:default}
        .hd .imgb{all:initial;cursor:pointer;border:none;border-radius:6px;
          background:#eef2ff;color:#3730a3;font:600 11px/1 -apple-system,sans-serif;
          padding:5px 9px;font-family:inherit}
        .hd .imgb:hover{background:#e0e7ff}
        .cb{all:initial;flex:none;width:15px;height:15px;cursor:pointer;accent-color:#2563eb}
        .thumb{all:initial;width:30px;height:30px;border-radius:6px;object-fit:cover;
          background:#eef2f7;flex:none}
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
          <button class="imgb" type="button" title="扫描页面里的大图（图库/壁纸/漫画站）">扫图片</button>
          <button class="x" type="button" title="关闭">✕</button></div>
        <ul class="list"></ul>
        <div class="ft">
          <span class="sp">HLS / DASH 整条推送，后端自动取分片并合并</span>
          <button class="ftbtn" type="button" data-act="all" hidden>全选</button>
          <button class="ftbtn" type="button" data-act="sel" hidden>下载所选 0</button>
        </div>
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

  const KIND_LABEL = {
    video: "视频",
    audio: "音频",
    image: "图片",
    hls: "HLS 流",
    dash: "DASH 流",
  };

  function subLabel(it) {
    if (it.seg) return "播放列表分片";
    if (it.kind === "image" && it.from === "dom-img") return "图片 · 本页扫描";
    return KIND_LABEL[it.kind] || "媒体";
  }
  const KIND_ICON = {
    video:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="m10 8 6 4-6 4V8z"/><rect x="2" y="4" width="20" height="16" rx="2"/></svg>',
    audio:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M9 18V5l10-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="16" cy="16" r="3"/></svg>',
    image:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="m21 15-5-5L5 21"/></svg>',
  };

  /* 已勾选的条目（按 key 记，刷新后 key 不变的还能留着） */
  const selected = new Set();

  function renderList() {
    if (!listEl) return;
    if (!items.length) {
      listEl.innerHTML =
        '<div class="empty">还没有嗅探到媒体<br>播放或刷新页面后会自动出现<br>图库站可以点右上角「扫图片」</div>';
      syncBatch();
      return;
    }
    listEl.innerHTML = items
      .map(
        (it, i) => `
        <li class="row">
          <input class="cb" type="checkbox" data-i="${i}" ${
          selected.has(keyOf(it)) ? "checked" : ""
        } title="勾选后可批量下载">
          ${
            it.kind === "image"
              ? `<img class="thumb" src="${esc(it.url)}" loading="lazy" alt="" referrerpolicy="no-referrer">`
              : `<span class="ico">${KIND_ICON[it.kind] || KIND_ICON.video}</span>`
          }
          <span class="meta">
            <span class="nm">${esc(it.name || guessName(it.url, it.kind))}</span>
            <span class="sub">${esc(subLabel(it))} · ${esc(fmtSize(it.size, it.approx))}</span>
          </span>
          <button class="go" type="button" data-i="${i}" ${
        it.seg
          ? 'title="这是播放列表的一个分片，只包含几秒钟；点推送会自动改推整条流，合并成完整视频"'
          : ""
      }>${it.seg ? "推整条" : "推送"}</button>
        </li>`
      )
      .join("");
    syncBatch();
  }

  /* 底部批量按钮的显隐与计数。图片站一页几百张，逐条点「推送」不现实。 */
  function syncBatch() {
    const sel = shadow && shadow.querySelector('.ftbtn[data-act="sel"]');
    const all = shadow && shadow.querySelector('.ftbtn[data-act="all"]');
    if (!sel || !all) return;
    const n = selected.size;
    sel.hidden = n === 0;
    sel.textContent = `下载所选 ${n}`;
    sel.disabled = false;
    all.hidden = items.length === 0;
    all.textContent =
      n > 0 && n === items.length ? "取消全选" : `全选 ${items.length}`;
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
          pushed.add(keyOf(it)); // 别再让下一次刷新把它带回来
          selected.delete(keyOf(it));
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

  /* 勾选/取消勾选：只改 selected，不重绘整张列表（图库站一页几百行，
     每点一次复选框就重建 DOM 会丢焦点、也会让图片重新加载）。 */
  const listChange = (e) => {
    const cb = e.target.closest("input.cb");
    if (!cb) return;
    const it = items[Number(cb.dataset.i)];
    if (!it) return;
    if (cb.checked) selected.add(keyOf(it));
    else selected.delete(keyOf(it));
    syncBatch();
  };

  /* 批量推送。串行而不是 Promise.all：ZDM 那边是单线程队列，
     一口气灌几十条既没必要，也会把唤醒流程反复触发。 */
  async function pushSelected() {
    const sel = shadow && shadow.querySelector('.ftbtn[data-act="sel"]');
    const picked = items.filter((it) => selected.has(keyOf(it)));
    if (!picked.length) return;
    if (sel) {
      sel.disabled = true;
      sel.textContent = `推送中 0/${picked.length}`;
    }
    let done = 0;
    for (const it of picked) {
      const ok = await pushOne(it).catch(() => false);
      done++;
      if (ok) {
        pushed.add(keyOf(it));
        selected.delete(keyOf(it));
        const i = items.indexOf(it);
        if (i >= 0) items.splice(i, 1);
      }
      if (sel) sel.textContent = `推送中 ${done}/${picked.length}`;
    }
    renderList();
    renderFab();
    if (sel) sel.disabled = false;
    syncBatch();
  }

  async function pushOne(it) {
    const page = location.href;
    let target = it;
    let audioUrl = "";
    let filename = it.name || guessName(it.url, it.kind);
    /* 分片、音频播放列表都不是用户想要的成品：
       · 分片推下去只有几秒钟（实测 Twitter 的 .m4s 159 KB）；
       · 音频播放列表（X 的 /pl/mp4a/…）推下去「只有声音」。
       都改挑「最新的视频轨播放列表 + 同源音频轨」一起推，后端拉完合流。
       页面上没有播放列表时不推（宁可不响应，也别交出一堆碎片）。 */
    if (it.seg || (it.kind === "hls" && isAudioPlaylistUrl(it.url))) {
      const pair = pickStreamPair(items);
      if (!pair) return false;
      target = { url: pair.url, kind: pair.kind };
      audioUrl = pair.audioUrl;
      filename = smartTitle() + ".mp4";
    } else if (it.kind === "hls" || it.kind === "dash") {
      // 视频播放列表：主轨就用点击的这条，另配同源音频轨一起合流
      target = it;
      let best = -1;
      for (const s of items) {
        if (!s || s.url === it.url) continue;
        if (s.kind !== "hls" && s.kind !== "dash") continue;
        if (!isAudioPlaylistUrl(s.url)) continue;
        const d = commonPathDepth(it.url, s.url);
        if (d > best) {
          best = d;
          audioUrl = s.url;
        }
      }
      if (best < 2) audioUrl = ""; // 公共前缀太浅＝大概率不是同一条视频
      filename = smartTitle() + ".mp4";
    } else if (
      (it.kind === "video" || it.kind === "audio") &&
      genericUrlName(filename)
    ) {
      // URL 末段是哈希/流水号（抖音 media-video-hvc1、YouTube
      // videoplayback）时改用页面标题，别让下载目录全是无意义的名字。
      const m = /\.([a-z0-9]{2,5})$/i.exec(filename);
      const ext = m ? m[1].toLowerCase() : it.kind === "audio" ? "m4a" : "mp4";
      filename = smartTitle() + "." + ext;
    }
    const body = {
      url: target.url,
      filename,
      referer: page,
      // 只有同站才带 cookie，见 sameSite() 的说明
      cookie: sameSite(it.url, page) ? document.cookie || undefined : undefined,
      source: "sniffer",
      kind: target.kind || it.kind || "video",
      // 音频播放列表配对（X 的音视频是两份 m3u8），后端拉完合流
      audio_url: audioUrl || undefined,
      // YouTube 阶梯行：真正的下载由后端按「页面地址 + 高度」走 yt-dlp，
      // 这两个字段不带过去，后端就只能拿到一个页面地址、不知道该下哪一档。
      page_url: it.page_url || undefined,
      height: it.height || undefined,
    };
    const r = await send({ type: "sniffer-push", item: body });
    return !!(r && r.ok);
  }

  async function refresh(forceDom) {
    const dom = scanDomThrottled(forceDom);
    const r = await send({ type: "sniffer-list" });
    // local 只传按需扫出来的图片：background 的列表是权威，别把本地旧条目拼回来
    merge((r && r.items) || [], dom, imgItems);
    renderFab();
    if (panelOpen) renderList();
  }

  /* 从 URL 猜图片扩展名：优先真实后缀；其次 query 里的 format= 参数
     （图床 CDN 常见 ?x-oss-process=image/format,webp 这种地址本身没后缀）；
     猜不到给 jpg —— 查看器兼容性最好。以前无后缀 URL 直接以 "12345" 这种
     名字推送，存下来的文件没有扩展名，用户根本打不开。 */
  function imgExtOf(url) {
    let m = /\.(jpg|jpeg|png|webp|gif|avif|bmp)(?:$|[?#])/i.exec(url);
    if (m) return m[1].toLowerCase() === "jpeg" ? "jpg" : m[1].toLowerCase();
    m = /format[=/,](?:image\/format[=/,])?(webp|png|jpe?g|gif|avif)/i.exec(url);
    if (m) return m[1].toLowerCase().replace("jpeg", "jpg");
    return "jpg";
  }

  /* 页面已经加载过的资源，浏览器知道它们到底多大。
     DOM 扫描出来的图片没有响应头（Content-Length 拿不到），所以列表里
     一律显示「大小未知」；performance 的 resource timing 里有
     encodedBodySize / transferSize，用它补上。 */
  function perfSizes() {
    const m = new Map();
    try {
      const es = performance.getEntriesByType("resource") || [];
      for (const e of es) {
        if (!e || !e.name) continue;
        const sz = e.encodedBodySize || e.transferSize || e.decodedBodySize || 0;
        if (sz > 0) m.set(e.name, sz);
      }
    } catch (_) {
      /* ignore */
    }
    return m;
  }

  /* 按需扫图片。图库/壁纸/漫画/商品详情页一页几百张，全部交给 webRequest
     会把列表淹掉（而且大半是图标和头像），所以只在用户点了「扫图片」时扫。
     判据：渲染面积足够大；懒加载还没拿到尺寸的（naturalWidth=0）保留，
     那种多半是原图，让用户自己判断。 */
  const IMG_NAME_SKIP =
    /(favicon|sprite|icon|logo|avatar|badge|emoji|loading|placeholder|spacer|pixel|beacon|qr|arrow|close|menu|search)/i;
  function scanImages() {
    const page = location.href;
    const out = [];
    const seen = new Set();
    const sizes = perfSizes();
    const add = (raw, el) => {
      let abs;
      try {
        abs = new URL(raw, page).href;
      } catch (_) {
        return;
      }
      if (!/^https?:/i.test(abs) || seen.has(abs)) return;
      seen.add(abs);
      let name = "";
      try {
        name = decodeURIComponent(new URL(abs).pathname.split("/").filter(Boolean).pop() || "");
      } catch (_) {
        /* ignore */
      }
      if (IMG_NAME_SKIP.test(name)) return;
      const w = el ? el.naturalWidth || Number(el.getAttribute("width")) || 0 : 0;
      const h = el ? el.naturalHeight || Number(el.getAttribute("height")) || 0 : 0;
      const rect = el ? el.getBoundingClientRect() : null;
      const shown = rect ? Math.min(rect.width, 480) * Math.min(rect.height, 480) : 0;
      if (w && h && w < 200 && h < 200 && shown < 160 * 160) return;
      if (!/\.[a-z0-9]{2,5}$/i.test(name)) name = (name || "image") + "." + imgExtOf(abs);
      out.push({
        url: abs,
        kind: "image",
        size: sizes.get(abs) || 0,
        name: name.replace(/[\\/:*?"<>|]/g, "_").slice(0, 120),
        from: "dom-img",
      });
    };
    document.querySelectorAll("img").forEach((el) => {
      const src =
        el.getAttribute("src") ||
        el.getAttribute("data-src") ||
        el.getAttribute("data-original") ||
        "";
      if (src && !/^(data|blob):/i.test(src)) add(src, el);
      const ss = el.getAttribute("srcset") || "";
      if (ss) {
        // srcset 里挑描述宽度最大的那一张（通常是原图）
        let best = "";
        let bestW = -1;
        for (const part of ss.split(",")) {
          const seg = part.trim().split(/\s+/);
          if (!seg[0]) continue;
          const w = seg[1] ? parseFloat(seg[1]) : 0;
          if (w >= bestW) {
            bestW = w;
            best = seg[0];
          }
        }
        if (best) add(best, el);
      }
    });
    // 直接链到原图的 <a href="*.jpg">：图库站的「查看原图」多是这种
    document
      .querySelectorAll('a[href$=".jpg"],a[href$=".jpeg"],a[href$=".png"],a[href$=".webp"],a[href$=".gif"],a[href$=".avif"]')
      .forEach((a) => add(a.getAttribute("href"), null));
    return out.slice(0, MAX_ROWS);
  }

  /* 图片的体积只能问桌面端：DOM 扫描拿不到 Content-Length，而
     performance 的 encodedBodySize 对跨域资源是 0（除非对方返回
     Timing-Allow-Origin）。桌面端不受 CORS 限制，能问到真实大小。 */
  async function probeImageSizes() {
    const need = imgItems
      .filter((x) => !x.size && /^https?:\/\//i.test(x.url))
      .slice(0, 12);
    if (!need.length) return;
    let r = null;
    try {
      r = await send({ type: "sniffer-probe-sizes", urls: need.map((x) => x.url) });
    } catch (_) {
      r = null;
    }
    const sizes = (r && r.sizes) || {};
    let changed = 0;
    for (const it of imgItems) {
      if (it.size) continue;
      const n = sizes[it.url];
      if (n > 0) {
        it.size = n;
        changed++;
      }
    }
    if (changed) {
      renderList();
      renderFab();
    }
  }

  function addImages() {
    const found = scanImages();
    let added = 0;
    for (const it of found) {
      if (pushed.has(keyOf(it))) continue;
      if (imgItems.some((x) => x.url === it.url)) continue;
      if (items.some((x) => x.url === it.url)) continue;
      imgItems.push(it);
      added++;
    }
    renderFab();
    renderList();
    // 体积问桌面端要（跨域资源浏览器这边量不出来）
    if (added) probeImageSizes();
    return added;
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
    if (listEl) {
      listEl.addEventListener("click", listClick);
      listEl.addEventListener("change", listChange);
    }
    const panel = shadow.querySelector(".panel");
    const imgb = shadow.querySelector(".hd .imgb");
    if (imgb) {
      imgb.addEventListener("click", () => {
        const n = addImages();
        panelOpen = true;
        panel.hidden = false;
        renderList();
        imgb.textContent = n ? `图片 +${n}` : "无新图片";
        setTimeout(() => {
          imgb.textContent = "扫图片";
        }, 1600);
      });
    }
    const btnAll = shadow.querySelector('.ftbtn[data-act="all"]');
    const btnSel = shadow.querySelector('.ftbtn[data-act="sel"]');
    if (btnAll) {
      btnAll.addEventListener("click", () => {
        /* 分片不进批量：批量下出来的是一堆几秒钟的碎片，没有意义。
           （分片行的单个「推整条」按钮会自动改推播放列表。） */
        const pickable = items.filter((it) => !it.seg);
        if (selected.size && selected.size === pickable.length) selected.clear();
        else pickable.forEach((it) => selected.add(keyOf(it)));
        renderList();
      });
    }
    if (btnSel) {
      btnSel.addEventListener("click", () => {
        pushSelected().catch(() => {});
      });
    }
    renderFab();
    refresh(true);
    observe();

    /* 给通用播放器按钮（playerbtn.js）用：它要读当前嗅探列表来配对音轨、
       也要能把面板调出来。两个文件是同一批 content script，同在 isolated
       world，直接挂 window 上最省事（不用再走一遍消息往返）。 */
  /* ---------- 播放列表的音视频配对 ---------- */

  /* X（Twitter）的 HLS 把音视频分成**两份独立的播放列表**：
   *    视频  /amplify_video/<id>/pl/avc1/1280x720/xxx.m3u8
   *    音频  /amplify_video/<id>/pl/mp4a/128000/xxx.m3u8
   * 只推视频那份 = 有画面没声音；只推音频那份 = 只有声音（用户两种都
   * 撞到过）。两份都要：主轨 + audio_url 一起给后端，ffmpeg 拉完合流。
   * 配对靠「同源 + 路径公共前缀最深」：同一条视频的两份共享
   * /amplify_video/<id>/，不同推文的公共前缀只有 /amplify_video/ 一段
   * —— 前缀深度 <2 的不配（跨视频误配）。 */
  const AUDIO_PLAYLIST_URL =
    /\/(mp4a|aac|mp3|opus|ogg|flac)(?:\/|$)|\/audio(?:\/|[-_.])|mime=audio|audio[-_]?track/i;

  function isAudioPlaylistUrl(u) {
    return !!u && AUDIO_PLAYLIST_URL.test(u);
  }

  function commonPathDepth(urlA, urlB) {
    try {
      const a = new URL(urlA);
      const b = new URL(urlB);
      if (a.origin !== b.origin) return -1;
      const pa = a.pathname.split("/").filter(Boolean);
      const pb = b.pathname.split("/").filter(Boolean);
      let i = 0;
      while (i < pa.length && i < pb.length && pa[i] === pb[i]) i++;
      return i;
    } catch (_) {
      return -1;
    }
  }

  /* 从播放列表集合里挑主轨（优先视频轨）+ 配对音轨。
   *
   *  选主轨不用「最新优先」：时间线自动播放时，别的视频的播放列表可能
   *  比当前这条还新。改成「优先挑**有音频配对**的视频轨里最新的」——
   *  X 的音视频两份是一起请求的，有配对的那条几乎肯定是正在看的。 */
  function pickStreamPair(list) {
    const pls = (list || []).filter(
      (x) => x && (x.kind === "hls" || x.kind === "dash") && x.url && !x.seg
    );
    if (!pls.length) return null;
    const byNew = pls.slice().sort((a, b) => (b.at || 0) - (a.at || 0));
    const vids = byNew.filter((s) => !isAudioPlaylistUrl(s.url));
    const auds = byNew.filter((s) => isAudioPlaylistUrl(s.url));

    const deepestAudio = (vUrl) => {
      let best = -1;
      let bestUrl = "";
      for (const a of auds) {
        if (a.url === vUrl) continue;
        const d = commonPathDepth(vUrl, a.url);
        if (d > best) {
          best = d;
          bestUrl = a.url;
        }
      }
      return best >= 2 ? bestUrl : ""; // 前缀太浅＝大概率不是同一条视频
    };

    let main = null;
    let audioUrl = "";
    for (const v of vids) {
      const a = deepestAudio(v.url);
      if (a) {
        main = v;
        audioUrl = a;
        break;
      }
    }
    if (!main) {
      // 没有任何视频轨带音频配对：退回「最新的视频轨」，音频尽力配
      main = vids[0] || byNew[0]; // 全是音频轨时至少还能出声
      if (main && !isAudioPlaylistUrl(main.url)) audioUrl = deepestAudio(main.url);
    }
    return { url: main.url, kind: main.kind, audioUrl };
  }

  /* URL 末段当文件名是不是「没意义」的：抖音的 media-video-hvc1、
   * YouTube 的 videoplayback、纯哈希（oMEizcg5DiAsHAr1ASAJgsIcf0ACIBADOBwePi）。
   * 是的话推送文件名改用页面标题。 */
  function genericUrlName(name) {
    if (!name) return true;
    const stem = name.replace(/\.[a-z0-9]{2,5}$/i, "");
    if (!stem) return true;
    if (
      /^(media[-_]video|video|audio|media|index|master|playlist|videoplayback|seg|chunk|hls|dash)/i.test(
        stem
      )
    ) {
      return true;
    }
    // 纯哈希样：无 CJK、无分隔符、≥16 位的字母数字串
    if (
      !/[\u4e00-\u9fff]/.test(stem) &&
      !/[\s._-]/.test(stem) &&
      stem.length >= 16 &&
      /^[A-Za-z0-9_-]+$/.test(stem)
    ) {
      return true;
    }
    return false;
  }

  /* ---------- 文件名用标题 ---------- */

  /* document.title 是不是「站点名」而不是「内容标题」。
   *
   *  抖音精选这类信息流页面，title 恒为应用名「抖音精选电脑版」——
   *  含品牌词、没有句子特征、很短，十个视频下下来全叫同一个名字。
   *  而 X 的 title 是推文正文（"(8) X 上的 XXX.：……"），带句子标点，
   *  必须放行。 */
  const BRAND_CN = {
    douyin: "抖音",
    kuaishou: "快手",
    bilibili: "哔哩",
    weibo: "微博",
    xiaohongshu: "小红书",
    zhihu: "知乎",
    douyu: "斗鱼",
    huya: "虎牙",
  };
  function titleIsSiteName(t) {
    if (!t) return true;
    if (t.length > 16) return false; // 站点名都很短
    if (/@|https?:|www\.|http/.test(t)) return false; // 明显是内容标题
    const host = (location.hostname || "").replace(/^www\./, "").toLowerCase();
    const zh = BRAND_CN[host.split(".")[0]] || "";
    if (zh && t.indexOf(zh) >= 0) return true;
    const brand = host.split(".")[0];
    return brand.length >= 4 && t.toLowerCase().indexOf(brand) >= 0;
  }

  /* <video> 附近的说明文字。
   *
   *  抖音精选的弹窗里，视频标题（作者 + 文案）就在播放器旁边的容器里。
   *  没有站点规则可用（各站结构都不同），只能用通用启发式：从 <video>
   *  往上找几层容器，取里面最长的叶子文本。playerbtn 的面板是 Shadow
   *  DOM，textContent 天然不含它，不会把自己抓进来。 */
  function captionNear(media) {
    if (!media || !media.parentElement) return "";
    let node = media;
    let budget = 600; // 别把整个页面走完
    for (let up = 0; up < 4 && node && budget > 0; up++) {
      node = node.parentElement;
      if (!node) break;
      let best = "";
      const walk = (el) => {
        if (budget <= 0) return;
        for (const c of el.children) {
          if (budget-- <= 0) return;
          const tag = c.tagName;
          if (
            tag === "SCRIPT" || tag === "STYLE" || tag === "BUTTON" ||
            tag === "INPUT" || tag === "SVG" || tag === "CANVAS" ||
            tag === "VIDEO" || tag === "AUDIO"
          ) {
            continue;
          }
          const hasBlock = Array.prototype.some.call(
            c.children,
            (x) =>
              /^(DIV|P|SECTION|ARTICLE|LI|UL|OL|H[1-6]|VIDEO|AUDIO)$/.test(
                x.tagName
              )
          );
          if (!hasBlock) {
            const t = (c.textContent || "").replace(/\s+/g, " ").trim();
            if (t.length >= 6 && t.length <= 150 && t.length > best.length) {
              best = t;
            }
          }
          walk(c);
        }
      };
      walk(node);
      if (best) return best;
    }
    return "";
  }

  /* 推送文件名的标题来源，按可靠性排序：
   * 1. 播放器元素自带的 title / aria-label；
   * 2. og:title / twitter:title；
   * 3. document.title（仅当它像内容标题）；
   * 4. <video> 附近的说明文字（document.title 是站点名时的兜底）；
   * 5. document.title 原文（实在没有更好的）。 */
  function smartTitle(media) {
    const clean = (s) =>
      String(s || "")
        .replace(/\s+/g, " ")
        .replace(/[\\/:*?"<>|]/g, "_")
        .trim()
        .slice(0, 100);
    // 播放器控件的操作提示（抖音 <video> 的 title 就是「点击按住可拖动
    // 视频」）不是内容标题。真正的标题很少这么短还带这些动词。
    const UI_HINT = /点击|按住|拖动|轻触|双击|播放|暂停|静音|全屏|倍速|画中画/;
    const accept = (s) => {
      const v = clean(s);
      if (v.length < 6 || titleIsSiteName(v)) return "";
      if (v.length <= 16 && UI_HINT.test(v)) return "";
      return v;
    };
    if (media && media.getAttribute) {
      for (const a of ["title", "aria-label", "alt"]) {
        const v = accept(media.getAttribute(a));
        if (v) return v;
      }
    }
    for (const sel of [
      'meta[property="og:title"]',
      'meta[name="twitter:title"]',
      'meta[property="og:video:title"]',
    ]) {
      const el = document.querySelector(sel);
      const v = accept(el && el.getAttribute("content"));
      if (v) return v;
    }
    const raw = clean(
      (document.title || "").replace(
        /\s*[-_|·—]\s*(bilibili|B站|YouTube|抖音|快手|小红书|微博|知乎).*$/,
        ""
      )
    );
    const dt = accept(raw);
    if (dt) return dt;
    const cap = accept(captionNear(media));
    if (cap) return cap;
    return raw || "video";
  }

  window.__zdmSniffer = {
    items: () => items.slice(),
    images: () => imgItems.slice(),
    title: (media) => smartTitle(media),
    pickStreamPair: (list) => pickStreamPair(list),
      open: () => {
        panelOpen = true;
        panel.hidden = false;
        renderList();
      },
      close: () => {
        panelOpen = false;
        panel.hidden = true;
      },
      refresh: () => refresh(true).catch(() => {}),
      scanImages: () => addImages(),
    };

    chrome.runtime.onMessage.addListener((msg) => {
      if (msg && msg.type === "sniffer-update") {
        // background 只知道自己看见的「网络响应」那一部分；页面里已经躺着的
        // <video>/<audio>（首屏直链媒体，webRequest 可能压根没经过）是我们自己
        // 扫 DOM 得到的。所以这里只能「合并」，不能整体覆盖——直接
        // items = msg.items 会把 DOM 扫描到的首屏媒体全部冲掉。
        if (msg.reset) {
          items = [];
          imgItems.length = 0;
          selected.clear();
        }
        // 非 reset 的推送是「有新发现」的增量通知，msg.items 是完整列表，
        // 但 DOM 扫到的那部分只在本地 —— 所以保留本地条目一起合并。
        merge(msg.items || [], scanDomThrottled(false), items.concat(imgItems));
        renderFab();
        if (panelOpen) renderList();
      }
      if (msg && msg.type === "sniffer-ping") {
        send({ type: "sniffer-list" }).then((r) => {
          merge((r && r.items) || [], scanDomThrottled(true), items.concat(imgItems));
          renderFab();
          if (panelOpen) renderList();
        });
      }
    });

    /* 定时轮询，两个作用：
       1. MV3 的 service worker 空闲 30 秒就被回收，回收期间 webRequest
          监听器不存在 —— 用户先开页面、过一会儿再播放，整个播放过程的
          媒体请求会全部漏掉。这条消息本身就是唤醒：SW 收到消息会重新执行
          background.js、重新注册监听器，之后打开的视频才有人嗅。
       2. 播放过程中持续刷新角标数字，不用等用户点开面板。 */
    const POLL_MS = 3000;
    setInterval(() => {
      // 页面不可见时降频：后台标签页没必要每 3 秒吵一次
      if (document.hidden) return;
      refresh(false).catch(() => {});
    }, POLL_MS);
  }

  start();
})();
