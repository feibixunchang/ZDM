/* ZDM Connector - 逐视频卡片的悬浮下载按钮（content script）
 *
 * 用户要的交互：
 *   鼠标移到视频卡片上 → 卡片右上角浮出「下载此视频」提示
 *   点它 → 弹出清晰度列表（1080P / 720P / 音频…）
 *   选一个 → 推送到 ZDM 下载
 *
 * 与 sniffer.js 的浮动面板是两条独立通道：
 *   - 浮动面板：页面级，列出「这个页面加载过哪些媒体」（适合任意网站）
 *   - 本文件：卡片级，列出「这个视频有哪些清晰度」（需要站点适配）
 *
 * 样式用 Shadow DOM 隔离：YouTube / B站 的 CSS 都很强势，
 * 普通 DOM 插入的按钮会被它们的 reset 规则打回原形。
 */

(() => {
  if (window.__zdmVideoBtnLoaded) return;
  window.__zdmVideoBtnLoaded = true;

  // 统一记错误。content script 在 isolated world，页面看不到它的 console，
  // 但扩展的「检查视图」里能看到 console.warn。
  // 注意：console.warn 会进 chrome://extensions 的「错误」页 —— 只留给
  // 真正的故障；预期内的降级（桥接没起来之类）一律走 dbg（debug 级别，
  // 用户不会被吓到）。
  const __err = (msg) => {
    try {
      console.warn("[ZDM] " + msg);
    } catch (_) {
      /* 忽略 */
    }
  };
  const dbg = (msg) => {
    try {
      console.debug("[ZDM] " + msg);
    } catch (_) {
      /* 忽略 */
    }
  };
  window.addEventListener("error", (e) => {
    // 浏览器自身的良性告警不是我们的故障，别转发（否则会出现在扩展的
    // 「错误」页里吓人）：
    //   - ResizeObserver loop ...：页面布局告警，YouTube 每次布局变化都会来一条
    //   - "Script error."：跨域脚本的匿名报错，没有任何可排查信息
    const m = String((e && e.message) || "");
    if (/ResizeObserver loop/i.test(m) || m === "Script error.") return;
    __err(m + " @" + (e && e.lineno));
  });
  // 记录运行上下文：isolated world 才有 chrome.runtime
  window.addEventListener("unhandledrejection", (e) => {
    const r = String((e && e.reason) || "");
    if (/ResizeObserver loop/i.test(r)) return;
    __err("rej: " + r);
  });

  /* 标记常量。必须定义在任何函数调用之前 —— const 有暂时性死区，
     boot() 里用到它们，若定义在后面会抛 ReferenceError。 */
  const MARK = "data-zdm-vbtn";
  const CARD_MARK = "data-zdm-card";
  const BTN_MARK = "data-zdm-btn";

  /* 站点适配器（sites.js）必须跑在页面的 MAIN world，因为它要读页面自己的
     __playinfo__ / ytInitialPlayerResponse —— 那些变量在 isolated world
     里读不到。而本脚本需要 chrome.runtime，必须留在 isolated world。

     注入由 background.js 用 chrome.scripting.registerContentScripts
     （world:"MAIN", document_start）完成 —— 那是浏览器原生通道，不受页面
     CSP 约束。以前这里是直接 document.createElement('script') 插
     <script src="chrome-extension://.../sites.js">，B站能过但 **YouTube
     必挂**：它的 CSP script-src 不含 chrome-extension:，注入被拦，
     YouTube 上点「下载此视频」永远没有清晰度。
     所以本文件不再主动注入，只在桥接迟迟不就绪时（老浏览器没有
     world:"MAIN"）才退回 <script src> 兜底。
     另注意 manifest 的 content_scripts 里**不能**写 "world": "MAIN" ——
     Chrome 155 遇到不认识的字段会把整个 content_scripts 数组丢掉，
     表现为所有 content script 都不执行（且不报错）。

     两者通过 DOM 上的桥接元素通信。 */
  const BRIDGE_ID = "zdm-site-bridge";

  /* MAIN world 那边（sites.js）用 postMessage 应答查询。
     走 postMessage 的原因：JS 自定义属性是每个 world 各自一份，
     MAIN world 设的 el.__zdmApi 在 isolated world 里读到的是 undefined；
     attribute 虽共享但放不了函数。postMessage 的 event 对象两个 world 共用。 */
  let queryId = 0;
  const inflight = new Map();

  window.addEventListener("message", (ev) => {
    const d = ev.data;
    if (!d || d.__zdm !== "reply") return;
    const fn = inflight.get(d.id);
    if (fn) {
      inflight.delete(d.id);
      fn(d.data);
    }
  });

  function ask(what, sel) {
    return new Promise((resolve) => {
      const id = ++queryId;
      let done = false;
      // YouTube 走 innertube 接口要等网络往返，3s 太紧，放宽到 8s
      const timer = setTimeout(() => {
        if (!done) {
          done = true;
          inflight.delete(id);
          resolve(null);
        }
      }, 8000);
      inflight.set(id, (data) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        resolve(data);
      });
      window.postMessage({ __zdm: "query", id, what, sel: sel || "" }, "*");
    });
  }

  let S = null;       // 当前站点的配置
  let siteName = "";

  /* 兜底注入：把 sites.js 作为 <script src> 插进页面，让它在 MAIN world
     执行（manifest 的 web_accessible_resources 允许页面加载它）。

     只有主路径失效时才走这里：background.js 的 chrome.scripting 注册
     需要 Chrome 102+ 的 world:"MAIN"。B站的 CSP 放行 chrome-extension:，
     所以兜底路径在 B站可用；YouTube 的 CSP 不放行 —— 那正是我们改用
     chrome.scripting 的原因。 */
  let fallbackTried = false;
  function injectMainWorld() {
    try {
      const url = chrome.runtime.getURL("sites.js");
      if (document.querySelector('script[data-zdm-sites="1"]')) return;
      const el = document.createElement("script");
      el.setAttribute("data-zdm-sites", "1");
      el.src = url;
      el.onerror = () => __err("sites.js 兜底注入失败（该站点 CSP 可能不放行 chrome-extension:）");
      (document.head || document.documentElement).appendChild(el);
    } catch (e) {
      __err("注入异常: " + (e && e.message));
    }
  }

  /* MAIN world 的脚本会设一个 data-zdm-ready 属性（attribute 跨 world 共享）。 */
  function bridgeReady() {
    const el = document.getElementById(BRIDGE_ID);
    return !!(el && el.getAttribute("data-zdm-ready") === "1");
  }

  function waitForBridge() {
    let waited = 0;
    const timer = setInterval(() => {
      waited += 100;
      if (bridgeReady()) {
        clearInterval(timer);
        boot();
      } else if (waited >= 8000) {
        clearInterval(timer);
        // 桥接始终没起来（三条注入通道都失败）：不能让按钮整个消失，
        // 退化为「无清晰度模式」—— 用内置最小配置把播放页按钮装出来。
        bootWithFallbackConfig();
      }
    }, 100);
    document.addEventListener("zdm-sites-ready", function onReady() {
      if (bridgeReady()) {
        clearInterval(timer);
        document.removeEventListener("zdm-sites-ready", onReady);
        boot();
      }
    });
  }

  /* 内置最小站点配置：所有注入通道都失败时的最后兜底。
     只配置播放器（播放页）、不配置卡片 —— 宁可少装按钮，也不用没验证过的
     选择器把按钮钉到整页容器上（那正是第二轮反馈里「按钮飞出播放窗口」
     的根因）。 */
  const FALLBACK_SITES = [
    {
      match: /(^|\.)bilibili\.com$/i,
      name: "bilibili",
      player: ".bpx-player-video-area",
    },
    {
      match: /(^|\.)youtube\.com$|(^|\.)youtu\.be$/i,
      name: "youtube",
      player: "#movie_player",
    },
  ];

  async function bootWithFallbackConfig() {
    if (started) return;
    const h = location.hostname.toLowerCase();
    const site = FALLBACK_SITES.find((s) => s.match.test(h));
    if (!site) {
      dbg("不是支持的站点");
      return;
    }
    siteName = site.name;
    S = { name: site.name, player: site.player, cards: "", cardSpec: null };
    PLAYER_SEL = site.player;
    dbg("桥接超时，以下载按钮（无清晰度）模式运行: " + site.name);
    start();
  }

  /* 主路径：等 background.js 用 chrome.scripting 注册的 sites.js
     （document_start 执行）把桥接建好。1.2 秒还没就绪就请 background 用
     executeScript(world:"MAIN") 现场补一枪 —— registerContentScripts 的
     world 字段要 Chrome 111+，而 executeScript 的 world 从 Chrome 95 就
     有，老浏览器上主路径静默失败时这条通道还能救。 */
  setTimeout(async () => {
    if (fallbackTried || bridgeReady()) return;
    fallbackTried = true;
    const h = location.hostname.toLowerCase();
    let ok = false;
    try {
      const r = await send({ type: "zdm-inject-sites" });
      ok = !!(r && r.ok);
    } catch (_) {
      ok = false;
    }
    if (ok) return; // sites.js 起来后会广播 zdm-sites-ready → boot
    // 最后才试 <script src>：只有 B站（CSP 放行 chrome-extension:）有效。
    if (h.includes("bilibili.com") || h.includes("bilivideo.com")) {
      injectMainWorld();
      return;
    }
    // YouTube 上三条注入通道都走不通：等 8 秒兜底超时后用内置最小配置
    // 把按钮装出来（清晰度读不到，但按钮不能整个消失）。
    dbg("站点适配器无法注入，播放按钮将以无清晰度模式运行");
  }, 1200);
  waitForBridge();

  /* 把「itag → 字节数」这张表交给 background，让它把嗅探列表里
     还挂着「大小未知」的条目补齐。YouTube 的播放列表经常在
     playerResponse 就绪前就抓到了直链，那时还没有长度可读，所以要重试几次。 */
  async function syncSnifferSizes() {
    let sentLadder = false;
    for (let i = 0; i < 8; i++) {
      try {
        // 先把「清晰度 → 大小」阶梯交过去：SABR 之后 videoplayback 的响应
        // 里根本没有长度，面板上那几条「大小未知」只有这张表能解释清楚。
        if (!sentLadder) {
          const lad = await ask("videoLadder");
          const rows = (lad && lad.rows) || [];
          if (rows.length) {
            sentLadder = true;
            await send({
              type: "sniffer-ladder",
              pageUrl: location.href,
              title: (lad && lad.title) || "",
              rows,
            });
          }
        }
        const lens = await ask("contentLengths");
        if (lens && Object.keys(lens).length) {
          await send({ type: "sniffer-sizes", sizes: lens });
          if (sentLadder) return;
        }
      } catch (_) {
        /* 忽略，下一轮再试 */
      }
      await new Promise((r) => setTimeout(r, 2500));
    }
  }

  let bootRunning = false;
  async function boot() {
    if (bootRunning) return;
    bootRunning = true;
    try {
      /* 桥接的 data-zdm-ready 属性一置位 waitForBridge 就会调这里，但那一刻
         sites.js 未必已经能应答 siteOf/config —— 以前一次失败就直接 return，
         且轮询定时器已被清掉，按钮从此永久消失（「刷新页面后按钮不见了，
         只能新开标签页」就是这个竞态）。改成带退避的重试，全部落空再退化。 */
      for (let i = 0; i < 10; i++) {
        try {
          const site = await ask("siteOf");
          siteName = site || "";
          if (siteName) {
            // 拿站点配置（选择器等）—— 通过 postMessage 传
            const cfg = await ask("config");
            if (cfg) {
              S = cfg;
              PLAYER_SEL = cfg.player || "";
              start();
              if (siteName === "youtube") syncSnifferSizes();
              return;
            }
          }
        } catch (_) {
          /* 忽略，下一轮再试 */
        }
        await new Promise((r) => setTimeout(r, 800));
      }
      bootWithFallbackConfig();
    } finally {
      bootRunning = false;
    }
  }
  // 播放页的播放器：列表页没有它，播放页没有卡片。boot() 时按站点赋值。
  let PLAYER_SEL = "";

  // host 用 data-zdm-vbtn=<角色>；卡片本身用另一个属性，
  // 否则 querySelectorAll("[data-zdm-vbtn=btn]") 会同时命中卡片和它的 host。
  let openMenu = null; // 当前打开的菜单 { el, card }
  let pending = null; // 正在解析格式的卡片

  /* 按钮与菜单都活在 Shadow DOM 里，light DOM 查不到，
     所以要把引用记在卡片自己的 dataset 上。 */
  const btnOf = (card) => card.__zdmBtn || null;

  const esc = (s) =>
    String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );

  const absUrl = (href) => {
    try {
      return new URL(href, location.href).href;
    } catch (_) {
      return "";
    }
  };

  /* 「这张卡片是不是一个视频、标题是什么」—— 纯 DOM 判据，选择器和正则
     来自 sites.js 下发的 cardSpec（函数过不了 postMessage，见那边的说明）。
     以前这里调 S.cardInfo(card)，而 S 来自 postMessage、cardInfo 传不过来，
     于是每次都被 try/catch 吞掉，列表页一个按钮都不装。 */
  function cardInfoLocal(card) {
    const spec = S && S.cardSpec;
    if (!spec || !spec.link) return null;
    let a;
    try {
      a = card.querySelector(spec.link);
    } catch (_) {
      return null;
    }
    const href = a && (a.getAttribute("href") || "");
    if (!href) return null;
    const pageUrl = absUrl(href);
    if (!pageUrl) return null;
    if (spec.hrefRe) {
      try {
        if (!new RegExp(spec.hrefRe, "i").test(pageUrl)) return null;
      } catch (_) {
        /* 正则本身有问题时别把卡片全否了 */
      }
    }
    let title = "";
    if (spec.title) {
      try {
        const t = card.querySelector(spec.title);
        if (t) title = ((t.getAttribute("title") || "") || t.textContent || "").trim();
      } catch (_) {
        /* 选择器失效就当没标题 */
      }
    }
    return { pageUrl, title: title || "视频" };
  }

  /* 注意单位表与循环的配套关系：先判 <1024 标 B，单位表必须从 "KB" 起步。
     之前这里把 "B" 放进循环表、i 从 -1 起步，导致每除一次 1024 单位却只
     前进一级的少一级 —— 80MB 的文件被标成「76.8 KB」，而嗅探面板同一
     数据是「76.8 MB」，正是「数字相同、单位差 1024 倍」的元凶。 */
  const fmtSize = (n) => {
    n = Number(n) || 0;
    if (!n) return "";
    if (n < 1024) return n.toFixed(0) + " B";
    const u = ["KB", "MB", "GB", "TB"];
    let i = -1;
    do {
      n /= 1024;
      i++;
    } while (n >= 1024 && i < u.length - 1);
    return n.toFixed(1) + " " + u[i];
  };

  const send = (msg) =>
    new Promise((res) => {
      try {
        chrome.runtime.sendMessage(msg, (r) => {
          void chrome.runtime.lastError;
          res(r);
        });
      } catch (_) {
        res(null);
      }
    });

  /* ------------------------------------------------------------ 按钮样式 */

  const CSS = `
:host {
  all: initial;
  /* host 本身不能吃掉鼠标事件，否则它会挡住自己 shadow 里的按钮
     （elementFromPoint 命中的是 host，不是按钮）。 */
  pointer-events: none;
}
.btn {
  position: absolute;
  top: var(--zdm-top, 8px); right: var(--zdm-right, 8px);
  display: inline-flex; align-items: center; gap: 5px;
  padding: 5px 10px;
  border: 0; border-radius: 999px;
  background: rgba(20, 20, 24, 0.82);
  color: #fff;
  font: 600 12px/1.2 -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
  cursor: pointer;
  opacity: 0;
  transition: opacity .12s ease;
  pointer-events: none;
  z-index: 2147483000;
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  white-space: nowrap;
  box-shadow: 0 2px 10px rgba(0,0,0,.35);
}
:host(.hover) .btn { opacity: 1; pointer-events: auto; }
/* 播放器上的按钮：播放器盖着遮罩层（.bili-mini-mask 挂在 body 上），
   页面 mousemove 的 target 永远不是播放器本身，靠 closest 找不到它。
   所以播放器的按钮常驻可见，靠自己的 :hover 反馈。 */
:host(.on-player) {
  position: fixed;
  z-index: 2147483000;
}
/* 菜单的层级要比按钮高：菜单项被按钮的 host 挡住时点不到。
   fixed 定位下 z-index 相同则按文档顺序叠放，所以要显式更大。 */
:host(.on-player.menu) {
  z-index: 2147483001;
}
/* host 是 0 尺寸的 fixed 容器：里面用 right:8px 定位会把按钮甩到 host 原点
   左边（宽度都丢掉），按钮/菜单整体左偏、菜单飞到一边。
   播放器上的按钮/菜单改用 left:0 —— 此时「host 原点 = 内容左缘」，
   placePlayer / placePlayerMenu 里减宽度的坐标才成立。卡片按钮仍用
   right:8px（卡片 host 在 position:relative 的卡片内，正常）。 */
:host(.on-player) .btn { opacity: .92; pointer-events: auto; top: 0; left: 0; right: auto; }
:host(.on-player) .btn:hover { opacity: 1; background: rgba(0,0,0, .95); }
/* 播放器菜单：host 已被 placePlayerMenu 钉在按钮正下方（top=按钮底+6,
   left=按钮右-菜单宽，右缘与按钮对齐）。菜单直接铺在 host 原点即可。
   若仍用 right:8px 会在 0 宽 host 里把菜单甩到左边（「面板飞到一边」）。
   卡片菜单的 host 没有 on-player，走 .menu 默认的 top:40px/right:8px，正常。 */
:host(.on-player.menu) .menu { top: 0; left: 0; right: auto; }
.btn:hover { background: rgba(0,0,0, .92); }
.btn svg { width: 13px; height: 13px; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
.btn.busy { opacity: 1; pointer-events: auto; }

.menu {
  /* host 是 pointer-events:none（否则它会挡住自己的按钮），
     菜单要能点回来。 */
  pointer-events: auto;
  position: absolute;
  top: 40px; right: 8px;
  min-width: 208px; max-height: 320px; overflow: auto;
  padding: 6px;
  border-radius: 12px;
  background: rgba(28, 28, 32, 0.97);
  color: #f4f4f5;
  font: 400 12px/1.45 -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
  box-shadow: 0 8px 28px rgba(0,0,0,.45);
  z-index: 2147483000;
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
}
.menu h4 {
  margin: 2px 6px 6px; font-size: 11px; font-weight: 600;
  color: #a1a1aa; text-transform: none;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.item {
  pointer-events: auto;
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
  padding: 7px 9px; border-radius: 8px; cursor: pointer;
  color: #e4e4e7;
}
.item:hover { background: rgba(255,255,255,.10); color: #fff; }
.item .lbl { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.item .sz { color: #a1a1aa; font-size: 11px; flex: none; }
.item.done { color: #4ade80; pointer-events: none; }
.item.failed { color: #f87171; }
.tip { padding: 8px 9px; color: #a1a1aa; line-height: 1.5; }
.tip b { color: #e4e4e7; }
.kbd { display:inline-block; padding: 1px 5px; border-radius: 4px; background: rgba(255,255,255,.12); font-size: 11px; }
`;

  /* ------------------------------------------------------------ 挂载 */

  function makeHost(mark) {
    const host = document.createElement("div");
    host.setAttribute(MARK, mark || "1");
    const root = host.attachShadow({ mode: "open" });
    // 界面语言（ui18n.js）：按钮、清晰度菜单都挂在这棵 shadow 树里，
    // 只观察它本身，不碰宿主页面的 DOM。
    try { if (typeof ZDMUI !== "undefined") ZDMUI.watch(root); } catch (_) {}
    const style = document.createElement("style");
    style.textContent = CSS;
    root.appendChild(style);
    return { host, root };
  }

  /* body 上只留一个播放器按钮（keep 是即将挂上去的那个）。
     卡片按钮的 host 挂在卡片内部，不会是 body 的直接子元素；菜单 host 的
     MARK 是 "menu" —— 两者都不会被这里误伤。 */
  function sweepPlayerButtons(keep) {
    let removed = 0;
    document.querySelectorAll('body > [' + MARK + '="btn"]').forEach((h) => {
      if (h === keep) return;
      if (!h.classList || !h.classList.contains("on-player")) return;
      const owner = h.__zdmOwner;
      if (owner) {
        delete owner.__zdmBtn;
        delete owner.__zdmBtnHost;
        if (trackedPlayers.has(owner)) trackedPlayers.delete(owner);
      }
      if (h.parentNode) h.parentNode.removeChild(h);
      removed++;
    });
    // 菜单 host 一样会残留（扩展热更新前打开的、或 SPA 换容器时丢下的）。
    // 只保留当前正在显示的那一个；它是 fixed 定位的，残留在页面上会一直
    // 飘在那儿，点了还会读到旧的回调。
    document.querySelectorAll('body > [' + MARK + '="menu"]').forEach((h) => {
      if (openMenu && openMenu.host === h) return;
      if (h.parentNode) h.parentNode.removeChild(h);
    });
    return removed;
  }

  function ensureButton(card) {
    const existing = btnOf(card);
    if (existing) return existing;
    const isPlayer = card.__zdmIsPlayer === true;
    // 卡片必须能定位，否则 absolute 定位会跑到页面别处。
    if (!isPlayer && getComputedStyle(card).position === "static") {
      card.style.position = "relative";
    }
    const { host, root } = makeHost("btn");
    const btn = document.createElement("button");
    btn.className = "btn";
    btn.type = "button";
    btn.setAttribute(BTN_MARK, "1");
    btn.innerHTML =
      '<svg viewBox="0 0 24 24"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg>' +
      "<span>下载此视频</span>";
    root.appendChild(btn);
    if (isPlayer) {
      // 播放器的按钮挂到 body 上、用 fixed 定位。
      // 原因：B站的播放器上盖着一层 position:fixed; z-index:10010 的全屏遮罩
      // （.bili-mini-mask），播放器本身在遮罩之下 —— 按钮挂在播放器里就永远
      // 点不到。挂到 body 上才能压过遮罩层。
      host.classList.add("on-player");
      // 不变式：整页只允许一个播放器按钮。挂上去之前先把别的清干净 ——
      // 覆盖三种情况：SPA 换容器后旧元素还在、扩展热更新后旧脚本留下的 host、
      // 以及旧代码从 mouseover 现造出来的野按钮。谁留谁清，绝不能两个。
      sweepPlayerButtons(host);
      document.body.appendChild(host);
    } else {
      card.appendChild(host);
    }
    // 记在卡片上：Shadow DOM 里的元素无法用 querySelector 找回来。
    card.__zdmBtn = btn;
    card.__zdmBtnHost = host;
    card.setAttribute(CARD_MARK, "1");
  // host 反向指回它的主人（播放器的 host 挂在 body 上，正向查不到）
  host.__zdmOwner = card;
  return btn;
}

  /* 菜单刚打开的一小段时间内，不要因为鼠标「路过」而关掉它。
     按钮和菜单之间有 8px 间隙，鼠标横向穿过时 target 既不是按钮也不是菜单，
     没有这段宽限期就会闪退。 */
  function justOpened() {
    return !!(openMenu && openMenu.at && Date.now() - openMenu.at < 450);
  }

  /* 悬停意图：指针离开「菜单 ∪ 按钮 ∪ 所属卡片」后不立刻关，
     给 260ms 让它走到菜单上；这期间一进来就取消关闭。
     B站播放器上盖着一层全屏遮罩（.bili-mini-mask，挂在 body 上），
     鼠标从按钮移向菜单必然穿过它 —— 那一刻 target 既不是按钮也不是菜单，
     也不是播放器的子元素，直接关就等于菜单永远点不到。 */
  let closeTimer = null;
  function cancelPendingClose() {
    if (closeTimer) {
      clearTimeout(closeTimer);
      closeTimer = null;
    }
  }
  function scheduleClose() {
    cancelPendingClose();
    // 鼠标已经进过菜单、或用户点过某一档：菜单从此不再自动关。
    // 以前是鼠标一离开就 260ms 关闭，在 YouTube 播放器上（鼠标动一下
    // 控制条就重排）菜单经常「只显示片刻」，用户手还没按下去就没了。
    if (openMenu && openMenu.pinned) return;
    closeTimer = setTimeout(() => {
      closeTimer = null;
      closeMenu();
    }, 260);
  }

  /* 指针此刻是不是压在菜单或按钮上（跨 Shadow DOM 判定）。 */
  function onMenuOrButton(e) {
    if (!openMenu) return false;
    const path = typeof e.composedPath === "function" ? e.composedPath() : [];
    if (path.indexOf(openMenu.host) !== -1) return true;
    for (const el of path) {
      if (el && el.nodeType === 1 && el.getAttribute && el.getAttribute(BTN_MARK)) return true;
    }
    return false;
  }

  /* ------------------------------------------------------------ 菜单 */

  function closeMenu() {
    cancelPendingClose();
    if (!openMenu) return;
    // 摘掉挂过的 scroll/resize 监听。以前这里只 removeChild，
    // removeMenuListeners 定义了却没人调 —— 播放页每开关一次菜单
    // 就多留一对监听器，页面滚久了会越来越卡。
    removeMenuListeners();
    const { root, host } = openMenu;
    root.querySelector(".menu")?.remove();
    host.remove();
    openMenu = null;
  }

  /* 菜单里的「推送」按钮点完要能关掉菜单。 */
  function removeMenuListeners() {
    if (openMenu && openMenu.place) {
      window.removeEventListener("resize", openMenu.place);
      window.removeEventListener("scroll", openMenu.place);
    }
  }

  /* 给元素生成一个定位串，MAIN world 那边靠它找回同一个元素。
     格式： "id:<id>" 或 "idx:<序号>"。序号是它在站点卡片列表里的位置。 */
  function cssPathOf(el) {
    if (!el || el.nodeType !== 1) return "";
    if (el.id) return "id:" + el.id;
    try {
      const list = document.querySelectorAll(S.cards || "div");
      for (let i = 0; i < list.length; i++) {
        if (list[i] === el) return "idx:" + i;
      }
    } catch (_) {
      /* 忽略 */
    }
    return "";
  }

  /* 向 MAIN world 要数据，带重试：播放页的清晰度是播放器初始化后才写进
     页面的，刚打开时经常还空着，重试一两次就能拿到。 */
  async function askWithRetry(what, tries) {
    let last = null;
    for (let i = 0; i < tries; i++) {
      const d = await ask(what);
      last = d;
      if (d && d.formats && d.formats.length) return d;
      if (i < tries - 1) await new Promise((r) => setTimeout(r, 1500));
    }
    return last;
  }

  /* 一行的身份。合并两次解析结果时靠它判断「这一档是不是已经有了」：
     同一个高度只留一行，音频用码率区分。 */
  function fmtKey(f) {
    if (!f) return "";
    if (f.height) return "h:" + f.height + ":" + (f.kind || "video");
    if (f.kind === "audio") return "a:" + (f.bitrate || f.label || "");
    return "l:" + (f.label || "");
  }

  /* opts.merge：把新结果并进已有菜单，而不是清空重建。
     为什么必须这样：以前 yt-dlp 的结果回来时会 menu.textContent = ""，
     用户正好点在某一行上，那一行就从 DOM 里被摘掉了 —— 表现就是
     「点了完全没反应」。合并时只更新数据、补大小，行本身不动。 */
  function renderFormats(menu, info, opts) {
    const merge = !!(opts && opts.merge);
    let head = merge ? menu.querySelector("h4") : null;
    if (!merge) menu.textContent = "";
    if (!head) {
      head = document.createElement("h4");
      menu.insertBefore(head, menu.firstChild);
    }
    head.textContent = info.title || "选择清晰度";

    const existing = new Map();
    if (merge) {
      menu.querySelectorAll(".item").forEach((r) => {
        if (r.dataset && r.dataset.key) existing.set(r.dataset.key, r);
      });
    }

    for (const f of info.formats) {
      const key = fmtKey(f);
      const old = key ? existing.get(key) : null;
      if (old) {
        // 已有这一档：把更好的一份数据换进去（ladder 那份常常没直链，
        // yt-dlp 那份有），同时补上之前缺的大小。
        if (!old.__zdmFmt || (!old.__zdmFmt.url && f.url)) old.__zdmFmt = f;
        if (f.size) {
          let sz = old.querySelector(".sz");
          if (!sz) {
            sz = document.createElement("span");
            sz.className = "sz";
            old.appendChild(sz);
          }
          sz.textContent = fmtSize(f.size);
        }
        continue;
      }
      const row = document.createElement("div");
      row.className = "item";
      row.dataset.key = key;
      // 行自己记着最新的格式数据：后台解析结果合并进来时改的是它，
      // 点击回调读的也是它，所以「先开菜单、后到直链」也能正常推送。
      row.__zdmFmt = f;
      const lbl = document.createElement("span");
      lbl.className = "lbl";
      lbl.textContent = f.label;
      row.appendChild(lbl);
      if (f.size) {
        const sz = document.createElement("span");
        sz.className = "sz";
        sz.textContent = fmtSize(f.size);
        row.appendChild(sz);
      }
      row.addEventListener("click", (ev) => onPick(info, row, ev));
      menu.appendChild(row);
    }
    // 播放器菜单：内容（尤其清晰度行）渲染进来后菜单会变宽，open 时
    // placePlayerMenu 用的是「读取中」的窄宽，右缘会偏离按钮。重排一次
    // 让右边缘重新对齐按钮右缘（left = 按钮右 - 当前菜单宽）。
    if (openMenu && openMenu.onPlayer && openMenu.place) openMenu.place();
  }

  /* 点某一行。重试也走这里 —— 以前失败分支会再挂一个 listener，
     挂第二次之后同一个行有两套回调，状态就乱了。 */
  async function onPick(info, row, ev) {
    if (ev) ev.stopPropagation();
    const menu = row.parentElement;
    if (menu) menu.__zdmBusy = true;      // 别再让后台结果替换列表
    if (openMenu) openMenu.pinned = true; // 用户已经在操作，菜单不许自动关
    if (row.__zdmPending) return;
    const lbl = row.querySelector(".lbl");
    row.__zdmPending = true;
    row.classList.remove("failed");
    row.classList.add("done");
    if (lbl) lbl.textContent = "推送中…";
    let ok = false;
    try {
      ok = await pushFormat(info, row.__zdmFmt, row);
    } finally {
      row.__zdmPending = false;
    }
    if (ok) {
      if (lbl) lbl.textContent = "已推送";
      setTimeout(closeMenu, 900);
    } else {
      row.classList.remove("done");
      row.classList.add("failed");
      if (lbl) lbl.textContent = "推送失败，点这里重试";
    }
  }

  async function openMenuFor(card, btn) {
    // 换一个菜单开：先把上一个的「延迟关闭」撤掉，否则那个定时器
    // 会在 260ms 后把新开的菜单一起判死刑。
    cancelPendingClose();
    closeMenu();
    const onPlayer = card.__zdmIsPlayer === true;
    const { host, root } = makeHost("menu");
    const menu = document.createElement("div");
    menu.className = "menu";
    root.appendChild(menu);
    if (onPlayer) {
      // 播放器的菜单同样挂到 body 上，否则会被站点的全屏遮罩压住
      host.classList.add("on-player", "menu");
      host.style.visibility = "hidden";
      document.body.appendChild(host);
      // 定位统一走 placePlayerMenu：菜单钉在按钮正下方（按钮现在悬在
      // 播放窗口上方，菜单仍然要落回窗口内，不能飘进页面标题栏）。
      const place = () => placePlayerMenu(openMenu);
      openMenu = { card, host, root, btn, place, onPlayer: true, at: Date.now() };
      place();
      host.style.visibility = "visible";
      window.addEventListener("resize", place);
      window.addEventListener("scroll", place, { passive: true });
    } else {
      card.appendChild(host);
      openMenu = { card, host, root, btn, at: Date.now() };
    }

    // 鼠标/手指一进菜单，就把菜单钉住（不再自动关）。
    // YouTube 播放器上鼠标一动控制条就重排，靠「离开 260ms 自动关」根本
    // 点不到 —— 用户看到的就是菜单「只闪一下」。
    const pin = () => {
      if (openMenu && openMenu.root === root) openMenu.pinned = true;
    };
    root.addEventListener("mouseover", pin);
    root.addEventListener("pointerdown", pin, true);

    // 卡片走 cardInfo，播放器走 playerFormats —— 播放页没有卡片，
    // 但播放器本身就是「正在播放的那个视频」。
    const isPlayer = onPlayer;
    // 先给个「读取中」：B站卡片现在要按自己的 bvid/cid 去问接口，
    // 播放器的清晰度也要等播放器初始化完才会进来。
    menu.innerHTML = '<div class="tip">正在读取清晰度…</div>';
    // 桥接没起来时重试只是白等（MAIN world 里没人应答），一次就放弃
    const tries = bridgeReady() ? 3 : 1;
    let info;
    const isYt = isPlayer && (siteName === "youtube" || (S && S.name === "youtube"));
    let ytNote = "";
    if (isYt) {
      lastYtError = "";
      const ytTitle = () =>
        document.title.replace(/\s*-\s*YouTube\s*$/, "") || "YouTube 视频";
      const putNote = (html) => {
        const note = document.createElement("div");
        note.className = "tip";
        note.innerHTML = html;
        menu.insertBefore(note, menu.firstChild);
        return note;
      };

      /* 1) 先用播放器自己的阶梯铺菜单。
         getAvailableQualityLevels() 是播放器本地状态：不请求接口、不看
         登录态、不需要 PO token，几十毫秒就返回。用户以前点开菜单要盯着
         「正在读取清晰度…」十几秒（yt-dlp 在跑），现在不会再出现了。 */
      const ladder = await ytLadderFormats();
      if (ladder.length) {
        renderFormats(menu, {
          site: "youtube",
          title: ytTitle(),
          pageUrl: location.href,
          cover: "",
          duration: "",
          formats: ladder,
          hasFormats: true,
        });
        putNote(
          "<b>清晰度取自播放器本身</b><br>" +
            "列表马上会补上本机解析的结果（含每档大小）。" +
            "点某一档如果播放器没在拉那一路流，会自动改用本机解析的直链。"
        );
        /* 2) 后台再问一次本机 yt-dlp（带浏览器登录态）。它成功的话一次就能
           给出完整阶梯和文件大小，而且点下去不用等切档。
           注意用 merge 而不是重建列表：重建会在用户手指正按着的时候
           把那一行从 DOM 里抽掉（「点了完全没反应」就是这么来的）。 */
        resolveYouTube().then((better) => {
          if (!better || !better.formats || !better.formats.length) return;
          if (!openMenu || openMenu.root !== root) return; // 菜单已关/已换
          renderFormats(menu, better, { merge: true });
          putNote("<b>已用本机解析补齐</b><br>每档都带文件大小，点击直接推送。");
        });
        return;
      }

      /* 3) 播放器没给出阶梯（播放器没初始化 / 特殊页面）：
           退回 yt-dlp，再退回嗅探直链，最后才是页面内 innertube。 */
      let yinfo = await resolveYouTube();
      if (!(yinfo && yinfo.formats && yinfo.formats.length)) {
        const sniffed = await sniffedYtFormats();
        if (sniffed.length) {
          yinfo = {
            site: "youtube",
            title: ytTitle(),
            pageUrl: location.href,
            cover: "",
            duration: "",
            formats: sniffed,
            hasFormats: true,
          };
          ytNote =
            "<b>只拿到当前正在播放的清晰度</b><br>" +
            "本机解析被 YouTube 拦了，这里给的是播放器自己正在用的直链。";
        }
      }
      if (!(yinfo && yinfo.formats && yinfo.formats.length)) {
        const local = await askWithRetry("playerFormats", tries);
        if (local && local.formats && local.formats.length) {
          yinfo = local;
          ytNote =
            "<b>只拿到 360P</b><br>" +
            "本机解析被 YouTube 拦了，只能退回页面内解析。";
        }
      }
      info = yinfo;
    } else {
      info = isPlayer
        ? await askWithRetry("playerFormats", tries)
        : await ask("formatsForCard", cssPathOf(card));
    }
    if (!info) {
      menu.innerHTML = '<div class="tip">没能识别这个视频。</div>';
      return;
    }

    if (!info.formats || !info.formats.length) {
      // 两条路都走空了，且 yt-dlp 明确说是人机验证/登录态问题：
      // 给出能真正解决的建议，而不是笼统的「等几秒再试」。
      if (isYt && /bot|Sign in|confirm|cookie/i.test(lastYtError || "")) {
        menu.innerHTML =
          '<div class="tip"><b>YouTube 要求人机验证</b><br>' +
          "请先在浏览器里登录 YouTube 账号，然后刷新本页、<br>" +
          "再点一次「下载此视频」。<br>" +
          "<span style='opacity:.7'>" +
          escapeHtml((lastYtError || "").slice(0, 140)) +
          "</span></div>";
        return;
      }
      // 站点能识别，但当前页面拿不到清晰度数据。
      // 播放页仍为空：多半是播放器还在加载，或该视频需要登录/付费。
      menu.innerHTML =
        '<div class="tip"><b>这个视频的清晰度还读不到</b><br>' +
        "播放器可能还在加载，或该视频需要登录 / 付费。<br>" +
        "稍等几秒再点一次「下载此视频」试试。" +
        (isYt && lastYtError
          ? "<br><span style='opacity:.65'>" + escapeHtml(lastYtError).slice(0, 160) + "</span>"
          : "") +
        "</div>";
      return;
    }

    renderFormats(menu, info);
    if (ytNote) {
      const note = document.createElement("div");
      note.className = "tip";
      note.innerHTML =
        ytNote +
        "<br>登录 YouTube 后刷新本页再点一次，可拿到全部清晰度。";
      menu.insertBefore(note, menu.firstChild);
    }
  }

  /* ------------------------------------------------------------ 推送 */

  async function pushFormat(info, f, row) {
    const lbl = row && row.querySelector(".lbl");
    const setLbl = (s) => {
      if (lbl) lbl.textContent = s;
    };

    // YouTube 有页面地址就够了：后端会用 yt-dlp 去下（直链只能从头顺序读
    // 一小段，分块引擎下不动）。所以这里不用等「切清晰度再截直链」，
    // 点下去就能立刻入队。
    const ytPage =
      info && info.site === "youtube" && f && f.height
        ? info.pageUrl || location.href
        : "";

    if ((!f || !f.url) && !ytPage) {
      // 播放器阶梯里的档位还没抓到直链（用户选的不是当前播放的那一档）。
      // 先切档、再等浏览器把它拉下来，然后才真正推送。
      if (f && f.ytLevel) {
        setLbl("切换清晰度中…");
        const got = await resolveYtLevel(f);
        if (got) {
          f = { ...f, url: got.url, size: got.size || f.size };
          if (got.audio_url) f.audio_url = got.audio_url;
        }
      }
      // 切档后仍然没抓到（YouTube 对正在缓冲的那一路根本不会再发请求，
      // 光靠嗅探必然落空）。此时直接回退到本机 yt-dlp 的同清晰度直链 ——
      // 以前到这一步就报「这一档没抓到直链，换一档试试」，等于把锅甩给用户。
      if (!f || !f.url) {
        setLbl("改用本机解析…");
        const better = await resolveYouTube();
        const list = (better && better.formats) || [];
        const same = f && f.height
          ? list.find((x) => x.height && x.height === f.height && x.url)
          : null;
        const pick = same || list.find((x) => x.url);
        if (!pick || !pick.url) {
          setLbl("没拿到直链，稍后再试");
          return false;
        }
        f = {
          ...pick,
          size: pick.size || (f && f.size) || 0,
          label: (f && f.label) || pick.label,
        };
      }
    }

    const body = {
      // YouTube 走页面地址（后端用 yt-dlp 下）；其它站点用直链
      url: f.url || ytPage,
      filename: buildName(info, f),
      // referer 兜底到当前页：播放器路径 info.pageUrl 已等于 location.href，
      // 但嗅探/卡片等路径若缺 pageUrl，留空会让后端少一个关键请求头。
      referer: info.pageUrl || location.href,
      // 从播放界面的「下载此视频」推的，和嗅探面板里点「推送」不是一回事。
      // 以前这里也写 sniffer，任务列表里全标成「嗅探」，排查时就被带偏了。
      source: "player",
      kind: f.kind && f.kind.startsWith("audio") ? "audio" : "video",
      // 同站才带 cookie（跨站 CDN 不带）
      cookie: sameSite(f.url, info.pageUrl) ? document.cookie || undefined : undefined,
      // 站点标记，后端可以据此补 Cookie / 换 UA
      site: info.site,
    };
    // YouTube：把页面地址和想要的高度一起交给后端，由它用 yt-dlp 下载。
    // 直链只能从头顺序读一小段（Google 的分段签名限制），分块引擎下不动，
    // 所以真正干活的是后端的 yt-dlp 通道。
    // 注意：这里必须用带 location.href 兜底的 ytPage，而不是裸 info.pageUrl ——
    // 一旦 info.pageUrl 为空，page_url 就会是 undefined，后端退回内置顺序引擎，
    // 在带 TUN 代理的机器上会被 googlevideo 的 302 反复打断，吞吐暴跌到十几 KB/s。
    if (info.site === "youtube" && f.height) {
      body.page_url = ytPage;
      body.height = f.height;
    }
    // DASH 的视频轨没有声音。优先用后端（yt-dlp）已经配好的 audio_url，
    // 否则退而在本页格式列表里挑最好的音频轨，一起交给后端用 ffmpeg 合并。
    if (f.kind === "video") {
      const audio = f.audio_url || (pickAudio(info.formats) || {}).url;
      if (audio) body.audio_url = audio;
    }
    const r = await send({ type: "sniffer-push", item: body });
    if (!r || !r.ok) {
      __err("推送失败: " + JSON.stringify(r) +
        (chrome.runtime.lastError ? " / " + chrome.runtime.lastError.message : ""));
    }
    return !!(r && r.ok);
  }

  /* 从格式列表里挑一条最好的音频轨。 */
  function pickAudio(formats) {
    if (!formats) return null;
    const audios = formats.filter((f) => f.kind === "audio" && f.url);
    if (!audios.length) return null;
    // 码率高的优先
    audios.sort((a, b) => (b.bitrate || 0) - (a.bitrate || 0));
    return audios[0];
  }

  /* YouTube 主路径：把视频页 URL + 本机浏览器里 YouTube 的登录态（含 HttpOnly
     的 __Secure-1PSID 等）交给 ZDM，由它用 yt-dlp 解开 signatureCipher / n
     参数得到可直接下载的直链（ghostdownload 同款思路：桌面端解密 + 带上登录态）。
     YouTube 现在对未登录请求一律要求「人机验证」，所以必须带上浏览器 cookie。 */
  let lastYtError = ""; // yt-dlp 解析失败原因，两种路径都失败时才提示用

  /* itag → 高度。嗅探到的 googlevideo 直链只带 itag，靠它反推清晰度。
     必须把 AV1(394-402) / VP9(330-337) 也列全：YouTube 现在默认给 AV1，
     漏了它们反推出来就是 0，菜单只能写「当前播放」（截图里就是这现象）。 */
  const YT_ITAG_HEIGHT = {
    5: 240, 6: 270, 17: 144, 18: 360, 22: 720, 34: 360, 35: 480, 36: 240, 37: 1080,
    43: 360, 44: 480, 45: 720, 46: 1080,
    133: 240, 134: 360, 135: 480, 136: 720, 137: 1080, 138: 2160,
    160: 144, 212: 480, 213: 480, 214: 720, 215: 720, 216: 1080, 217: 1080,
    264: 1440, 266: 2160, 298: 720, 299: 1080, 302: 720, 303: 1080,
    308: 1440, 315: 2160,
    // VP9
    330: 144, 331: 240, 332: 360, 333: 480, 334: 720, 335: 1080, 336: 1440, 337: 2160,
    // AV1
    394: 144, 395: 240, 396: 360, 397: 480, 398: 720, 399: 1080, 400: 1440,
    401: 2160, 402: 4320,
  };
  const YT_AUDIO_ITAG = new Set([139, 140, 141, 171, 172, 249, 250, 251, 256, 258, 325, 328]);

  /* 把嗅探到的 googlevideo 直链整理成「高度 → 直链」的索引。
     YouTube 的播放器此刻正在用的 URL 是**已经签好名、浏览器自己刚拉过**的，
     不需要解 signatureCipher、不需要登录态、也不需要 PO token。
     唯一要处理的是 URL 里的 range 参数 —— 带着它只能取到那一小段。 */
  async function sniffIndex() {
    let items = [];
    try {
      const r = await send({ type: "sniffer-list" });
      items = (r && r.items) || [];
    } catch (_) {
      return { byHeight: {}, audio: null };
    }
    const byHeight = {};
    let audio = null;
    for (const it of items) {
      const u = it && it.url;
      if (!u || !/\/videoplayback\?/.test(u)) continue;
      let p;
      try {
        p = new URL(u);
      } catch (_) {
        continue;
      }
      const itag = Number(p.searchParams.get("itag") || 0);
      const size = Number(p.searchParams.get("clen") || it.size || 0);
      p.searchParams.delete("range");
      p.searchParams.delete("rn");
      const url = p.toString();
      if (YT_AUDIO_ITAG.has(itag) || it.kind === "audio") {
        if (!audio || size > audio.size) audio = { url, size };
        continue;
      }
      const h = YT_ITAG_HEIGHT[itag] || 0;
      if (!h) continue;
      const prev = byHeight[h];
      // 同一档会被切成很多段请求，留最大的那条
      if (!prev || size > prev.size) byHeight[h] = { url, size, itag };
    }
    return { byHeight, audio };
  }

  async function sniffedYtFormats() {
    const { byHeight, audio } = await sniffIndex();
    return Object.keys(byHeight)
      .map(Number)
      .sort((a, b) => b - a)
      .map((h) => ({
        label: h + "P · 视频+音频",
        url: byHeight[h].url,
        kind: "video",
        size: plausibleSize(byHeight[h].size),
        height: h,
        itag: byHeight[h].itag,
        audio_url: audio ? audio.url : undefined,
      }));
  }

  /* itag → 字节数（页面里就有，见 sites.js 的 ytContentLens）。
     再转成「高度 → 该高度最大的一份」，给菜单标大小用。 */
  async function ytSizesByHeight() {
    const lens = await ask("contentLengths");
    const out = {};
    for (const k of Object.keys(lens || {})) {
      const h = YT_ITAG_HEIGHT[Number(k)] || 0;
      if (!h) continue;      const n = Number(lens[k]) || 0;
      if (n > (out[h] || 0)) out[h] = n;
    }
    return out;
  }

  /* 播放器自己的清晰度阶梯。

     为什么这条最可靠：getAvailableQualityLevels() 是播放器本地状态，不请求
     接口、不看登录态、不需要 PO token —— YouTube 拦得住 yt-dlp，拦不住
     它自己已经加载好的播放器。点某一档时我们再让播放器切过去，浏览器就会
     真的去拉那一路流，然后从嗅探里把直链截下来。 */
  async function ytLadderFormats() {
    const levels = await ask("qualityLevels");
    if (!Array.isArray(levels) || !levels.length) return [];
    const [{ byHeight, audio }, sizes] = await Promise.all([
      sniffIndex(),
      ytSizesByHeight(),
    ]);
    const audioUrl = audio ? audio.url : undefined;
    // 嗅探里没有音频轨时（暂停播放、或音频段早于扩展注入）退回页面格式
    // 列表；再拿不到就让后端去补 —— 总之不能把「会没声音」当正常交付。
    let fallbackAudio = "";
    if (!audioUrl) {
      try {
        const local = await ask("playerFormats");
        const a = pickAudio(local && local.formats);
        if (a) fallbackAudio = a.url;
      } catch (_) {
        fallbackAudio = "";
      }
    }
    return levels.map((l) => ({
      label: l.height + "P · 视频+音频",
      url: (byHeight[l.height] || {}).url || "",
      kind: "video",
      size: plausibleSize((byHeight[l.height] || {}).size || sizes[l.height] || 0),
      height: l.height,
      ytLevel: l.q,
      // 音频轨整段视频只有一条，任何档位都配它
      audio_url: audioUrl || fallbackAudio || undefined,
    }));
  }

  /* SABR 之后播放器 streamingData 里的 contentLength 常常是「桩」大小：
     十几~几十 KB，那是初始化段/单个分片而不是整片 —— 真视频哪怕 240P
     也不可能小于 1MB。小于 1MB 的一律按未知处理：宁可先不显示，也别把
     「1440P 42.1KB」这种明显错误的数字摆出来（解析成功后合并会补真的）。 */
  function plausibleSize(n) {
    n = Number(n) || 0;
    return n >= 1048576 ? n : 0;
  }

  /* 选了一个「还没抓过」的档位：先切档，再轮询嗅探列表等它的直链出现。 */
  async function resolveYtLevel(f) {
    if (!f.ytLevel) return null;
    // 该档的直链可能已经在嗅探列表里（刚才播过），先看一眼，别白切档。
    let idx = await sniffIndex();
    if (!(idx.byHeight[f.height] && idx.byHeight[f.height].url)) {
      const r = await ask("setQuality", f.ytLevel);
      if (!r || !r.ok) return null;
    }
    const start = Date.now();
    const deadline = start + 12000;
    let nudged = false;
    while (Date.now() < deadline) {
      idx = await sniffIndex();
      const hit = idx.byHeight[f.height];
      if (hit && hit.url) {
        // 音频轨可能没被抓到（例如暂停状态播放器不再请求音频段）。
        // 退回页面自己的格式列表里找一条，否则下下来没有声音。
        let aUrl = idx.audio ? idx.audio.url : undefined;
        if (!aUrl) {
          const local = await ask("playerFormats");
          const a = pickAudio(local && local.formats);
          if (a) aUrl = a.url;
        }
        return { url: hit.url, size: hit.size, audio_url: aUrl };
      }
      // 缓冲还够时播放器压根不会重新请求 → 轻微 seek 推它一把。
      if (!nudged && Date.now() - start > 2000) {
        nudged = true;
        await ask("nudge");
      }
      await new Promise((res) => setTimeout(res, 600));
    }
    return null;
  }

  /* 把后端解析出的「itag → 字节数」交给 background，补掉嗅探面板里的
     「大小未知」。YouTube 的 videoplayback 分包响应既没有 Content-Length
     也没有 Content-Range，URL 里也常常不带 clen，除了这张表没别的来源。 */
  function syncBackendSizes(formats) {
    const sizes = {};
    for (const f of formats || []) {
      if (f && f.itag && f.size) sizes[f.itag] = f.size;
    }
    if (Object.keys(sizes).length) send({ type: "sniffer-sizes", sizes }).catch(() => {});
  }

  /* 把后端解出的完整阶梯交给嗅探面板。面板上那几条 videoplayback 是
     SABR 流式请求，本身没有「文件大小」这个概念（chunked，无 Content-Length
     / Content-Range / itag / clen），所以只能按清晰度报大小。 */
  function publishLadder(formats, title) {
    const rows = [];
    for (const f of formats || []) {
      if (!f || !f.size) continue;
      const h = Number(f.height) || 0;
      if (!h) continue; // 纯音频轨不进面板，视频行自带配套音轨
      rows.push({
        height: h,
        size: Number(f.size) || 0,
        label: String(h) + "P" + (f.codec ? " · " + f.codec : ""),
      });
    }
    if (!rows.length) return;
    rows.sort((a, b) => b.height - a.height);
    send({
      type: "sniffer-ladder",
      pageUrl: location.href,
      title: title || "",
      rows,
    }).catch(() => {});
  }

  async function resolveYouTube() {
    const url = location.href;
    try {
      // cookie 由 background（service worker）去取 —— 这里是 content script，
      // chrome.cookies 在 isolated world 里**根本不存在**，以前在这里取只会
      // 抛 TypeError 再被 catch 吞掉，于是「带登录态」从未真正发生过。
      // 详见 background.js 的 youtubeCookieText()。
      const r = await send({
        type: "zdm-youtube-resolve",
        url,
      });
      if (r && r.ok && r.formats && r.formats.length) {
        syncBackendSizes(r.formats);
        publishLadder(r.formats, r.title);
        return {
          site: "youtube",
          title: r.title || "YouTube 视频",
          pageUrl: url,
          cover: "",
          duration: "",
          formats: r.formats,
          hasFormats: true,
        };
      }
      if (r && r.error) {
        // 先只记下来不弹提示：还要试一次页面内 innertube（它自带浏览器
        // 登录态，很可能直接成功）。两条路都失败时才在菜单里说明原因。
        lastYtError = r.error;
        dbg("yt-dlp 解析失败: " + r.error);
      }
    } catch (e) {
      dbg("yt-dlp 解析异常: " + (e && e.message));
    }
    return null;
  }

  function sameSite(a, b) {
    try {
      const x = new URL(a).hostname.toLowerCase();
      const y = new URL(b).hostname.toLowerCase();
      return x === y || x.endsWith("." + y) || y.endsWith("." + x);
    } catch (_) {
      return false;
    }
  }

  /* 要拼进 innerHTML 的外部文本（解析器报错原因）必须先转义，
     否则里面的 < > 会被当成标签，把提示整段吃掉。 */
  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function buildName(info, f) {
    const t = (info.title || "video").replace(/[\\/:*?"<>|]/g, "_").slice(0, 90);
    if (f.kind === "audio") return t + ".m4a";
    if (f.kind === "video") return t + ".mp4";
    return t + ".mp4";
  }

  /* ------------------------------------------------------------ 事件 */

  function onMouseOver(e) {
    if (!openMenu) return;
    // 鼠标在我们自己的按钮/菜单内部移动：什么都不做。
    // 少了这一条，播放页里鼠标从按钮移向菜单的瞬间菜单就关了。
    if (onMenuOrButton(e)) {
      cancelPendingClose();
      return;
    }
    // 只有鼠标真正离开这张卡片时才关菜单。
    // 之前是「target 变了就关」，而点击菜单项时鼠标会移到菜单上（菜单挂在
    // body 上，不是卡片的子元素），target 一变菜单就被关掉，
    // 结果 pushFormat 还没跑完菜单就没了。
    const t = e.target;
    const inCard = openMenu.card.contains ? openMenu.card.contains(t) : false;
    // 菜单在 shadow DOM 里，host.contains(shadowChild) 恒为 false，
    // 所以要用 composedPath 里有没有菜单的 host 来判断。
    const path = typeof e.composedPath === "function" ? e.composedPath() : [];
    const inMenu = path.some((el) => el === openMenu.host) ||
      (openMenu.host.contains ? openMenu.host.contains(t) : false);
    if (inCard || inMenu || justOpened()) {
      cancelPendingClose();
      return;
    }
    // 路过站点自己的元素（比如 B站播放器上那层全屏遮罩）：延迟关，
    // 260ms 内走到菜单上就取消 —— 见 scheduleClose 的说明。
    scheduleClose();
  }

  function onMouseMove(e) {
    // 鼠标在我们自己的按钮/菜单上：既不要关菜单，也不要切 hover 状态。
    // 播放页的按钮和菜单都挂在 body 上，cardOf() 找不到它们的主人，
    // 以前这里会直接 closeMenu() —— 那是「菜单一点开就消失」的元凶。
    if (openMenu && onMenuOrButton(e)) {
      cancelPendingClose();
      return;
    }
    const card = cardOf(e.target);
    if (!card) {
      if (openMenu && !justOpened()) scheduleClose();
      return;
    }
    // 菜单自己占位，别把 hover 状态抢走
    if (openMenu && openMenu.card === card) return;
    // 播放器的按钮常显、坐标由 scan()/positionTick 算，绝不在这里现造：
    // 这里造的 host 不进 trackedPlayers，就是一个钉在页面左上角不动的第二按钮。
    if (card.__zdmIsPlayer) {
      if (!card.__zdmBtn) scheduleScan(); // 万一 scan 还没跑到，催它一次
      return;
    }
    const btn = ensureButton(card);
    if (btn && card.__zdmBtnHost) card.__zdmBtnHost.classList.add("hover");
  }

  function onMouseOut(e) {
    const card = cardOf(e.target);
    if (!card) return;
    if (openMenu && openMenu.card === card) return; // 菜单开着就留着
    if (card.__zdmBtnHost) card.__zdmBtnHost.classList.remove("hover");
  }

  /* 从事件目标往上找到所属的卡片。
     按钮在 Shadow DOM 里，Element.closest() 不会跨越 shadow 边界，
     所以要先跳出 shadow root 再往上找。 */
  function cardOf(target) {
    let el = target;
    if (el && el.getRootNode) {
      const root = el.getRootNode();
      if (root && root.host) el = root.host; // 跳出 Shadow DOM
    }
    if (!el || !el.closest) return null;
    // 播放器优先：它可能不是任何卡片的子元素
    const player = playerOf(el);
    if (player) return player;
    // 兜底模式下没有卡片配置，closest("") 会直接抛 SyntaxError
    if (!S || !S.cards) return null;
    const card = el.closest(S.cards);
    if (!card) return null;
    if (card === document.body || card === document.documentElement) return null;
    return card;
  }

  /* 事件目标是否落在播放页的播放器上。

     **只认 scan() 正在追踪的那个元素**。closest(S.player) 命中哪个元素取决于
     鼠标当时压在谁身上：video 在 #movie_player 里，命令栏在 .html5-video-player
     里，软导航后还可能命中外层 ytd-player —— 而 scan() 选中的是另一个。
     只要不是同一个对象，get 到的就是「另一个 player」：它没有 __zdmBtn，
     于是 onMouseMove 会现造一个按钮（挂 body、fixed、无 top/left、不进
     trackedPlayers）—— 这就是「播放页出现两个下载按钮、都能点开面板」的根因
     （第二个按钮永远钉在页面左上角不动，因为没人给它算坐标）。
     返回 null 时交给上层：按钮一律由 scan() 创建与定位。 */
  function playerOf(el) {
    if (!el || !el.closest || !S.player) return null;
    let p = null;
    try {
      p = el.closest(S.player);
    } catch (_) {
      return null;
    }
    if (!p) return null;
    // 已经选好播放器之后，别的候选一概不认（scan 还没跑过时放行）
    if (trackedPlayers.size && !trackedPlayers.has(p)) return null;
    if (!p.__zdmIsPlayer) {
      p.__zdmIsPlayer = true;
      p.__zdmPlayer = true;
    }
    return p;
  }

  /* 判断事件是不是发生在我们自己的按钮上。

     注意：跨 Shadow DOM 的事件在页面里被「重定向」了 —— 捕获阶段的
     e.target 是 host 元素，真正的按钮在 e.composedPath() 里。
     所以必须走 composedPath，不能只看 e.target.closest()。 */
  function isOurButton(event) {
    const target = event && event.target;
    if (!target) return false;
    // 已经是从 shadow 内部传出来的（我们自己的监听器在 shadow 里时）
    if (target.hasAttribute && target.hasAttribute(BTN_MARK)) return true;
    const path = typeof event.composedPath === "function" ? event.composedPath() : null;
    if (path) {
      for (const el of path) {
        if (el && el.nodeType === 1 && el.hasAttribute && el.hasAttribute(BTN_MARK)) return true;
      }
    }
    return !!(target.closest && target.closest("[" + BTN_MARK + "]"));
  }

  function onClick(e) {
    if (!isOurButton(e)) {
      // 菜单现在是「钉住」的（不会因为鼠标挪开而关），所以必须给一个
      // 明确的关闭方式：点到菜单以外的任何地方就收起。
      if (openMenu) {
        const path = typeof e.composedPath === "function" ? e.composedPath() : [];
        if (path.indexOf(openMenu.host) === -1) closeMenu();
      }
      return;
    }
    e.preventDefault();
    e.stopPropagation();
    // 从 composedPath 里找出按钮所属的 host（它记着对应的卡片 / 播放器）。
    // 注意：按钮在 shadow 里，它的 parentElement 是 ShadowRoot，
    // 不是 host —— host 要从 getRootNode().host 拿。
    const path = typeof e.composedPath === "function" ? e.composedPath() : null;
    let host = null;
    if (path) {
      for (const el of path) {
        if (el && el.nodeType === 1 && el.hasAttribute && el.hasAttribute(BTN_MARK)) {
          const root = el.getRootNode ? el.getRootNode() : null;
          host = root && root.host ? root.host : el.parentElement;
          break;
        }
      }
    }
    if (!host) {
      const card = cardOf(e.target);
      if (!card) return;
      openMenuFor(card, card.__zdmBtn);
      return;
    }
    // host 记着它的主人（卡片或播放器），直接用它
    const target = host.__zdmOwner || null;
    if (!target) return;
    openMenuFor(target, target.__zdmBtn);
  }

  function onKey(e) {
    if (e.key === "Escape") closeMenu();
  }

  /* ------------------------------------------------------------ 播放器按钮定位
   *
   * 关键坑（用户第三轮反馈的核心）：
   *   - YouTube 的播放器选择器若命中整页容器 ytd-watch-flexy（横跨整屏），
   *     按钮会被放到整页右上角、飞出播放窗口。
   *   - B站 #bilibili-player 是整列容器（视频+简介+评论），同样会偏。
   *   - 播放器是 SPA 异步渲染的，document_idle 时它的 rect 可能还是
   *     (0,0,0,0)，一次性定位会把按钮钉在左上角，要等滚动/resize 才归位。
   *
   * 解法：只保留「不被其它候选包含」且「面积最小」的那一个元素（真正的
   * 播放器），并用 requestAnimationFrame 循环持续把它和菜单钉在播放器
   * 右上角；定位有效前先隐藏，避免闪到左上角。 */

  const trackedPlayers = new Map(); // playerEl -> { host, btn, shown, top, left }
  let rafScheduled = false;

  function schedulePositionLoop() {
    if (rafScheduled) return;
    if (!trackedPlayers.size && !(openMenu && openMenu.onPlayer)) return;
    rafScheduled = true;
    requestAnimationFrame(positionTick);
  }

  function positionTick() {
    rafScheduled = false;
    for (const [p, t] of trackedPlayers) {
      // 回收条件是「播放器元素脱离文档」，不是「按钮脱离文档」——
      // 按钮挂在 body 上永远连着，以前只查按钮，SPA 一换播放器容器
      // 就留下一个钉在过时位置、点不开菜单的僵尸按钮
      // （「页面上出现两三个下载按钮，只有最新那个好使」的元凶）。
      if (!t.host || !t.host.isConnected || !p.isConnected) {
        if (t.host && t.host.parentNode) t.host.parentNode.removeChild(t.host);
        delete p.__zdmBtn;
        delete p.__zdmBtnHost;
        trackedPlayers.delete(p);
        continue;
      }
      placePlayer(p, t);
    }
    if (openMenu && openMenu.onPlayer && openMenu.host && openMenu.host.isConnected) {
      placePlayerMenu(openMenu);
    }
    if (trackedPlayers.size || (openMenu && openMenu.onPlayer)) schedulePositionLoop();
  }

  function placePlayer(p, t) {
    const r = p.getBoundingClientRect();
    if (r.width < 40 || r.height < 40) return; // 还没布局好
    const w = t.btn && t.btn.isConnected ? t.btn.getBoundingClientRect().width : 98;
    const bh = t.btn && t.btn.isConnected ? t.btn.getBoundingClientRect().height : 26;
    // 钉在播放窗口自己的右上角内侧（与 Ghost Download 一致）：
    // 视频内容上方的那一小条、贴右边。绝不放进播放窗口「上方」的页面留白 ——
    // 那里是站点顶栏（YouTube masthead）的地盘，按钮会被它盖住而看不见。
    //
    // YouTube 例外：它的播放窗口右上角在鼠标移入时会冒出一列原生按钮
    // （播放列表、分享、稍后观看…）。我们的按钮常显、位置固定，正好压在那个
    // 播放列表按钮上，用户点不到它。所以 YouTube 往下让 44px 落到那一列下面，
    // 仍在播放窗口右上区内，谁也不挡谁。
    const gapTop = siteName === "youtube" ? 44 : 8;
    let top = Math.round(r.top + gapTop);
    // 小窗兜底：底部约 56px 是控制栏（进度条 + 按钮），别压上去
    const maxTop = Math.round(r.bottom - 56 - bh);
    if (top > maxTop) top = Math.max(Math.round(r.top + 8), maxTop);
    const left = Math.round(r.right - w - 8);
    if (top !== t.top || left !== t.left) {
      t.host.style.top = top + "px";
      t.host.style.left = left + "px";
      t.top = top;
      t.left = left;
    }
    if (!t.shown) {
      t.host.style.visibility = "visible";
      t.shown = true;
    }
  }

  function placePlayerMenu(om) {
    const card = om.card;
    if (!card) return;
    // 菜单钉在按钮正下方、右边缘与按钮对齐（按钮在窗口上方时菜单
    // 仍然落在窗口内，不会飘到页面标题栏上）。
    const br = om.btn && om.btn.isConnected ? om.btn.getBoundingClientRect() : null;
    if (br) {
      const menuEl = om.root ? om.root.querySelector(".menu") : null;
      const mw = menuEl ? menuEl.getBoundingClientRect().width : 224;
      om.host.style.top = Math.round(br.bottom + 6) + "px";
      om.host.style.left = Math.round(Math.max(8, br.right - mw)) + "px";
      return;
    }
    const r = card.getBoundingClientRect();
    const w = om.btn && om.btn.isConnected ? om.btn.getBoundingClientRect().width : 98;
    om.host.style.top = Math.round(r.top + 44) + "px";
    om.host.style.left = Math.round(r.right - w - 56) + "px";
  }

  /* ------------------------------------------------------------ 扫描 */

  function scan() {
    // 1) 播放页的播放器
    if (PLAYER_SEL) {
      let players = [];
      try {
        players = Array.from(document.querySelectorAll(PLAYER_SEL));
      } catch (_) {
        players = [];
      }
      // 只保留不被其它候选包含的元素（最里层）—— 这才是真正的播放器，
      // 而不是包着它的整页/整列容器。
      players = players.filter((p) => !players.some((q) => q !== p && p.contains(q)));
      const rectOf = (el) => {
        try {
          return el.getBoundingClientRect();
        } catch (_) {
          return { width: 0, height: 0, top: 0, right: 0 };
        }
      };
      const bigEnough = (el) => {
        const r = rectOf(el);
        return r.width >= 40 && r.height >= 40;
      };
      // 「这一层真的装着视频」——YouTube 上 ytd-player / #movie_player /
      // .html5-video-player 层层嵌套，只有最里层那个才是画面本身。
      // 选中外层时按钮会落在黑边/留白上，鼠标事件命中的却是里层，
      // 于是又冒出一个「另一个 player」的按钮。
      const hasVideo = (el) => {
        try {
          return !!el.querySelector("video");
        } catch (_) {
          return false;
        }
      };
      const tracked = trackedPlayers.size ? trackedPlayers.keys().next().value : null;
      // 已跟踪的播放器只要还匹配选择器、还在文档里、尺寸正常，就绝不动它。
      // 原因：YT_PLAYER 同时命中好几个元素（含隐藏的 0 尺寸模板），若每次
      // 都按「面积最小」重选，隐藏模板（面积 0）必赢 —— 按钮钉在看不见的
      // 元素上永远 hidden（1.5.30 「按钮彻底消失」的根因）；而 1.5.28 之前
      // 从不回收，第一个撞大运选中的按钮才一直留在屏上（多余按钮的根因）。
      let player =
        tracked && tracked.isConnected && players.includes(tracked) && bigEnough(tracked)
          ? tracked
          : null;
      // 唯一例外：已跟踪的是外层容器（自己不装 video、只有子元素装），
      // 而里层有更贴合的候选 —— 换成里层，否则按钮与鼠标的「主人」永久错位。
      if (player && !hasVideo(player) && players.some((p) => p !== player && player.contains(p) && bigEnough(p) && hasVideo(p))) {
        player = null;
      }
      let fresh = null;
      if (!player) {
        // 只在「当前有有效尺寸」的候选里选，隐藏元素（面积 0）没有资格。
        fresh = players.filter(bigEnough);
        fresh.sort((a, b) => {
          const ha = hasVideo(a) ? 0 : 1;
          const hb = hasVideo(b) ? 0 : 1;
          if (ha !== hb) return ha - hb; // 装着 <video> 的那层优先
          const ra = rectOf(a);
          const rb = rectOf(b);
          return ra.width * ra.height - rb.width * rb.height;
        });
      }
      const chosen = player || fresh[0] || null;
      // 不变式：整页只保留一个播放器按钮。选中的播放器变了（SPA 软导航 /
      // 影院模式切换会换容器元素），旧的那个连按钮一起移除。
      for (const [p, t] of Array.from(trackedPlayers)) {
        if (!p.isConnected || (chosen && p !== chosen)) {
          if (t.host && t.host.parentNode) t.host.parentNode.removeChild(t.host);
          delete p.__zdmBtn;
          delete p.__zdmBtnHost;
          trackedPlayers.delete(p);
        }
      }
      // 兜底清扫：body 上除了「当前追踪的那一个」，其它播放器按钮一律移除。
      // 覆盖三类漏网之鱼：旧版本运行期留下的、扩展热更新前留下的、以及
      // 旧代码从 mousemove 里现造出来的野按钮（它们没有 owner 也没有坐标，
      // 会永远钉在页面角落 —— 用户看到的就是「两个下载按钮」）。
      const liveHost = trackedPlayers.size
        ? trackedPlayers.values().next().value.host
        : null;
      sweepPlayerButtons(liveHost);
      if (chosen && !trackedPlayers.has(chosen)) {
        chosen.__zdmIsPlayer = true;
        ensureButton(chosen);
        const host = chosen.__zdmBtnHost;
        const btn = chosen.__zdmBtn;
        if (host) {
          host.style.visibility = "hidden"; // 定位有效前先藏好
          trackedPlayers.set(chosen, { host, btn, shown: false, top: 0, left: 0 });
          placePlayer(chosen, trackedPlayers.get(chosen));
          schedulePositionLoop();
        }
      }
    }

    // 2) 列表页的视频卡片
    if (!S.cards) return;
    const cards = document.querySelectorAll(S.cards);
    for (const card of cards) {
      if (card.__zdmBtn) continue;
      // body / html 也能匹配 cardInfo 里的宽松选择器（它们包含整页的链接），
      // 绝不能把按钮挂到 body 上——那会变成一个悬浮在整页左上角的怪东西。
      if (card === document.body || card === document.documentElement) continue;
      // 只给能识别出视频链接的卡片装按钮
      try {
        if (!cardInfoLocal(card)) continue;
      } catch (_) {
        continue;
      }
      ensureButton(card);
    }
  }

  let started = false;
  let scanTimer = null;
  /* 统一入口：任何「该重新扫一遍」的信号都走这里，避免多套定时器打架。 */
  function scheduleScan(delay) {
    if (scanTimer) clearTimeout(scanTimer);
    scanTimer = setTimeout(() => {
      scanTimer = null;
      try {
        scan();
      } catch (e) {
        __err("scan: " + e);
      }
    }, delay == null ? 400 : delay);
  }

  function start() {
    if (started) return; // boot() 可能被调用两次（轮询 + 事件）
    if (!document.documentElement) {
      setTimeout(start, 200);
      return;
    }
    started = true;
    // 事件用捕获阶段：站点的 stopPropagation 拦不住我们
    document.addEventListener("mouseover", onMouseOver, true);
    document.addEventListener("mousemove", onMouseMove, true);
    document.addEventListener("mouseout", onMouseOut, true);
    document.addEventListener("click", onClick, true);
    document.addEventListener("keydown", onKey, true);

    scan();

    // 列表页会懒加载，DOM 变化很频繁，节流扫描
    const mo = new MutationObserver(() => scheduleScan(600));
    try {
      mo.observe(document.documentElement, {
        childList: true,
        subtree: true,
      });
    } catch (_) {
      /* 忽略 */
    }
    window.addEventListener("scroll", () => scheduleScan(400), { passive: true });
  }
})();
