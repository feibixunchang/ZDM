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
  const __err = (msg) => {
    try {
      console.warn("[ZDM] " + msg);
    } catch (_) {
      /* 忽略 */
    }
  };
  window.addEventListener("error", (e) => __err(e.message + " @" + e.lineno));
  // 记录运行上下文：isolated world 才有 chrome.runtime
  window.addEventListener("unhandledrejection", (e) => __err("rej: " + e.reason));

  /* 标记常量。必须定义在任何函数调用之前 —— const 有暂时性死区，
     boot() 里用到它们，若定义在后面会抛 ReferenceError。 */
  const MARK = "data-zdm-vbtn";
  const CARD_MARK = "data-zdm-card";
  const BTN_MARK = "data-zdm-btn";

  /* 站点适配器（sites.js）必须跑在页面的 MAIN world，因为它要读页面自己的
     __playinfo__ / ytInitialPlayerResponse —— 那些变量在 isolated world
     里读不到。而本脚本需要 chrome.runtime，必须留在 isolated world。

     做法：把 sites.js 作为 <script src> 动态插进页面（manifest 里的
     web_accessible_resources 允许页面加载它），这样它就在 MAIN world 执行。
     注意 manifest 的 content_scripts 里**不能**写 "world": "MAIN" ——
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
      const timer = setTimeout(() => {
        if (!done) {
          done = true;
          inflight.delete(id);
          resolve(null);
        }
      }, 3000);
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

  // 把 sites.js 插进页面，让它在 MAIN world 执行
  function injectMainWorld() {
    try {
      const url = chrome.runtime.getURL("sites.js");
      if (document.querySelector('script[data-zdm-sites="1"]')) return;
      const el = document.createElement("script");
      el.setAttribute("data-zdm-sites", "1");
      el.src = url;
      el.onerror = () => __err("sites.js 注入失败");
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
        __err("站点适配器桥接超时");
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

  injectMainWorld();
  waitForBridge();

  async function boot() {
    try {
      const site = await ask("siteOf");
      siteName = site || "";
      if (!siteName) {
        __err("不是支持的站点");
        return;
      }
      // 拿站点配置（选择器等）—— 通过 postMessage 传
      const cfg = await ask("config");
      if (!cfg) {
        __err("拿不到站点配置");
        return;
      }
      S = cfg;
      PLAYER_SEL = cfg.player || "";
      start();
    } catch (e) {
      __err("boot: " + (e && e.message));
      console.warn("[ZDM] 初始化失败:", e && e.message, e && e.stack);
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

  const fmtSize = (n) => {
    n = Number(n) || 0;
    if (!n) return "";
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = -1;
    do {
      n /= 1024;
      i++;
    } while (n >= 1024 && i < u.length - 1);
    return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + " " + u[i];
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
:host(.on-player) .btn { opacity: .92; pointer-events: auto; }
:host(.on-player) .btn:hover { opacity: 1; background: rgba(0,0,0, .95); }
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
    const style = document.createElement("style");
    style.textContent = CSS;
    root.appendChild(style);
    return { host, root };
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
      document.body.appendChild(host);
      const place = () => {
        const r = card.getBoundingClientRect();
        const w = btn ? btn.getBoundingClientRect().width : 98;
        host.style.top = Math.round(r.top + 44) + "px";
        host.style.left = Math.round(r.right - w - 56) + "px";
      };
      place();
      openMenu = { card, host, root, btn, place, at: Date.now() };
      window.addEventListener("resize", place);
      window.addEventListener("scroll", place, { passive: true });
    } else {
      card.appendChild(host);
      openMenu = { card, host, root, btn, at: Date.now() };
    }

    // 卡片走 cardInfo，播放器走 playerFormats —— 播放页没有卡片，
    // 但播放器本身就是「正在播放的那个视频」。
    const isPlayer = card.__zdmIsPlayer === true;
    // 通过 postMessage 向 MAIN world 要数据
    const info = isPlayer
      ? await ask("playerFormats")
      : await ask("formatsForCard", cssPathOf(card));
    if (!info) {
      menu.innerHTML = '<div class="tip">没能识别这个视频。</div>';
      return;
    }

    if (!info.formats.length) {
      // 站点能识别，但当前页面拿不到清晰度数据
      menu.innerHTML =
        '<div class="tip"><b>这个视频的清晰度还读不到</b><br>' +
        "列表页只加载了封面，没加载媒体地址。<br>" +
        "点开视频进入播放页后，鼠标移到播放器上就能看到「下载此视频」了。</div>";
      return;
    }

    const head = document.createElement("h4");
    head.textContent = info.title || "选择清晰度";
    menu.appendChild(head);

    for (const f of info.formats) {
      const row = document.createElement("div");
      row.className = "item";
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
      row.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        row.classList.add("done");
        lbl.textContent = "推送中…";
        const ok = await pushFormat(info, f, row);
        if (ok) {
          lbl.textContent = "已推送";
          setTimeout(closeMenu, 900);
        } else {
          row.classList.remove("done");
          row.classList.add("failed");
          lbl.textContent = "推送失败，重试";
          row.onclick = null;
          row.addEventListener("click", async (e2) => {
            e2.stopPropagation();
            row.classList.remove("failed");
            lbl.textContent = "推送中…";
            if (await pushFormat(info, f, row)) {
              lbl.textContent = "已推送";
              setTimeout(closeMenu, 900);
            } else {
              row.classList.remove("failed");
              lbl.textContent = "重试";
            }
          });
        }
      });
      menu.appendChild(row);
    }
  }

  /* ------------------------------------------------------------ 推送 */

  async function pushFormat(info, f, row) {
    const body = {
      url: f.url,
      filename: buildName(info, f),
      referer: info.pageUrl,
      source: "sniffer",
      kind: f.kind && f.kind.startsWith("audio") ? "audio" : "video",
      // 同站才带 cookie（跨站 CDN 不带）
      cookie: sameSite(f.url, info.pageUrl) ? document.cookie || undefined : undefined,
      // 站点标记，后端可以据此补 Cookie / 换 UA
      site: info.site,
    };
    // DASH 的视频轨没有声音。把同站点的音频轨一起给后端，
    // 由它下载后用 ffmpeg 合并成一个有声的视频。
    if (f.kind === "video") {
      const audio = pickAudio(info.formats);
      if (audio) body.audio_url = audio.url;
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

  function sameSite(a, b) {
    try {
      const x = new URL(a).hostname.toLowerCase();
      const y = new URL(b).hostname.toLowerCase();
      return x === y || x.endsWith("." + y) || y.endsWith("." + x);
    } catch (_) {
      return false;
    }
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
    const card = el.closest(S.cards);
    if (!card) return null;
    if (card === document.body || card === document.documentElement) return null;
    return card;
  }

  /* 事件目标是否落在播放页的播放器上。 */
  function playerOf(el) {
    if (!el || !el.closest || !S.player) return null;
    let p = null;
    try {
      p = el.closest(S.player);
    } catch (_) {
      return null;
    }
    if (!p) return null;
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
    if (!isOurButton(e)) return;
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

  /* ------------------------------------------------------------ 扫描 */

  function scan() {
    // 1) 播放页的播放器
    if (PLAYER_SEL) {
      let player = null;
      try {
        player = document.querySelector(PLAYER_SEL);
      } catch (_) {
        player = null;
      }
      if (player && !player.__zdmBtn) {
        player.__zdmIsPlayer = true;
        ensureButton(player);
        // 按钮是 fixed 定位，要按播放器的实际位置摆放。
        const place = () => {
          const host = player.__zdmBtnHost;
          if (!host) return;
          const r = player.getBoundingClientRect();
          const btn = player.__zdmBtn;
          const w = btn ? btn.getBoundingClientRect().width : 98;
          host.style.top = Math.round(r.top + 10) + "px";
          // 往左让开播放器右上角自己的控件（YouTube 的 Logo、B站的投屏等）
          host.style.left = Math.round(r.right - w - 56) + "px";
        };
        place();
        player.__zdmPlace = place;
        window.addEventListener("resize", place);
        window.addEventListener("scroll", place, { passive: true });
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
    let timer = null;
    const mo = new MutationObserver(() => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(scan, 600);
    });
    try {
      mo.observe(document.documentElement, {
        childList: true,
        subtree: true,
      });
    } catch (_) {
      /* 忽略 */
    }
    window.addEventListener("scroll", () => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(scan, 400);
    }, { passive: true });
  }
})();
