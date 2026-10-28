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
  let panelEl = null;
  let lastDomScan = 0;

  /* ---------- 浮动按钮的位置（可拖动） ----------
   *
   * 旧版把按钮钉死在 right:18/bottom:18，右下角是页面自己的控件最爱待的
   * 地方（播放器控制条、客服浮窗、Cookie 提示、「返回顶部」），一颗钉死的
   * 按钮会成片地挡住它们。现在按住按钮就能拖到任意位置，松手记住。
   *
   * 存的不是绝对 left/top，而是「靠哪条边的距离」：
   *   { ax: "left"|"right", x: 距该边的像素, ay: "top"|"bottom", y: 距该边的像素 }
   * 这样窗口缩放、分辨率变化、字体放大让按钮宽度变了（角标从 9 变成 100+
   * 就有几像素差）时，它仍然贴着用户当初放的那条边，而不是被挤到屏幕外。
   * pos = null 表示默认位置（右下角，与旧版一致）。 */
  const POS_KEY = "sniff_pos";
  const MARGIN = 18; // 默认位置离右边的距离，沿用旧版
  const EDGE = 8; // 自由拖动时允许贴到离边多近
  let pos = null;
  let lastPosJson = "";
  let suppressClick = false; // 拖完松手会补一个 click，那颗按钮不能被它开关面板
  /* 当前这次拖动的状态（起点、抓取偏移、是否已越过死区）。声明在模块作用域：
     build() 里写它，start() 里的 storage.onChanged 也要读它（正按着按钮的时候
     别让别的标签页抢位置），藏在 build() 内部就是引用不到的变量。 */
  let drag = null;
  // 用户在本页面上已经动过位置（拖过或复位过）：异步读回来的旧存档就不能再抢位置
  let touchedByUser = false;

  /* ---------- 贴边收纳 ----------
   *
   * 松手时把按钮吸到最近的那条竖边，闲置一会儿收成「只剩图标 + 一个小圆点」的
   * 半透明圆胶囊，鼠标经过、点击或打开面板立刻展开。这一条才是「别挡页面」的
   * 正解：光靠能拖走，下次打开页面它还是钉在右下角。
   *
   * 为什么不做「一半伸出屏幕外」的悬浮球样式：触屏上没有 hover，伸出去的那半
   * 既点不到也没法拖回来，用户会觉得按钮丢了。收成圆点后整颗都在屏内，
   * 手指点一下就展开，鼠标和触屏的行为是一致的。
   *
   * 开关是设置页的「贴边收纳」（sniff_dock，默认开）。关掉就退回纯自由摆放。 */
  const DOCK_KEY = "sniff_dock";
  const SNAP_X = 6; // 吸附之后离边的距离
  const DOCK_TOL = 12; // 离边不超过这个距离才算「贴在边上」，只有贴边才允许收起
  const DOCK_DELAY = 900; // 松手 / 鼠标离开之后多久收起
  const DOCK_GUARD = 1500; // 刚交互过这么久之内不收（光标停在按钮上时不会一缩又弹回）
  let dockOn = false; // 读回设置之前先按「不收」走，免得默认位置也跟着改动态
  let docked = false;
  let hovering = false; // 光标是不是正压在按钮上（含 shadow 内部）
  let dockTimer = null;
  let lastInteract = 0;
  // 光标是不是停在按钮附近（mousemove 记账，离开时才补一次「可以开始倒计时了」）
  let touching = false;

  /* 面板展开方向由按钮所在位置决定（见 placePanel）：按钮被拖到屏幕上半部
     时，面板往下开；拖到左边时，面板往右开。写死 bottom:48px 的话，
     按钮一放到顶部，面板就直接开出可视区了。 */

  /* 面板展开方向由按钮所在位置决定（见 placePanel）：按钮被拖到屏幕上半部
     时，面板往下开；拖到左边时，面板往右开。写死 bottom:48px 的话，
     按钮一放到顶部，面板就直接开出可视区了。 */

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
  function guessName(url, kind, audioOnly) {
    let name = "";
    try {
      const u = new URL(url);
      name = decodeURIComponent(u.pathname.split("/").filter(Boolean).pop() || "");
    } catch (_) {
      name = "";
    }
    name = name.replace(/[\\/:*?"<>|]/g, "_").slice(0, 120);
    if (!name || !/\.[a-z0-9]{2,5}$/i.test(name)) {
      const ext =
        kind === "audio"
          ? ".mp3"
          : kind === "image"
            ? ".jpg"
            : kind === "hls" || kind === "dash"
              ? audioOnly
                ? ".m4a"
                : ".mp4"
              : ".mp4";
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
      const isAudio = tag === "audio" || type.startsWith("audio/");
      /* <video>/<audio> 的 src 也可能直接指向 m3u8/mpd（Twitter、播客站）。
         当普通文件下只会得到一个播放列表文本，必须按流交给 ffmpeg。 */
      const streamKind = /\.m3u8(?:$|[?#])/i.test(abs)
        ? "hls"
        : /\.mpd(?:$|[?#])/i.test(abs)
          ? "dash"
          : "";
      const audioOnly = !!streamKind && isAudio;
      out.push({
        url: abs,
        kind: streamKind || (isAudio ? "audio" : "video"),
        audio: audioOnly || undefined,
        size: timingSize(abs),
        name: guessName(abs, streamKind || (isAudio ? "audio" : "video"), audioOnly),
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

  /* ---------- 位置：算、存、取、复位 ---------- */

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(v, hi));

  function viewport() {
    const de = document.documentElement || {};
    const vw = Math.max(
      Number(de.clientWidth) || 0,
      Number(window.innerWidth) || 0,
      160
    );
    const vh = Math.max(
      Number(de.clientHeight) || 0,
      Number(window.innerHeight) || 0,
      160
    );
    return { vw, vh };
  }

  /* 宿主盒子只包住那颗按钮（面板是 absolute，不占流），所以它的尺寸就是按钮的尺寸。
     收起态是 42px 的圆胶囊 —— 不跟上这个变化的话，「离边距离」会按展开时的宽度算，
     收起来之后就等于把按钮推进屏幕里一截。 */
  function hostSize() {
    if (host && host.offsetWidth) return { w: host.offsetWidth, h: host.offsetHeight || 40 };
    const w = docked && fab && fab.classList.contains("docked") ? 42 : 96;
    return { w, h: w === 42 ? 42 : 40 };
  }

  /* 读回来的值必须先看一眼是不是合法形状：手改过 storage、或者旧版本存过
     别的东西（键名撞车）都不能让它把按钮丢到屏幕外。非法值一律退回默认。 */
  function normPos(p) {
    if (!p || typeof p !== "object") return null;
    const ax = p.ax === "left" || p.ax === "right" ? p.ax : null;
    const ay = p.ay === "top" || p.ay === "bottom" ? p.ay : null;
    const x = Number(p.x);
    const y = Number(p.y);
    if (!ax || !ay || !isFinite(x) || !isFinite(y) || x < 0 || y < 0 || x > 20000 || y > 20000) {
      return null;
    }
    return { ax, x: Math.round(x), ay, y: Math.round(y) };
  }

  const posJson = (p) => (p ? `${p.ax}:${p.x}:${p.ay}:${p.y}` : "");

  /* 由「靠左/右、靠上/下的距离」算出视口坐标并夹紧，然后写回 host。
     拖动过程中、恢复存档、窗口 resize 之后都走这里，行为只有一套。 */
  function applyPos(p) {
    if (!host) return;
    const { vw, vh } = viewport();
    const s = hostSize();
    let left = !p ? vw - s.w - MARGIN : p.ax === "right" ? vw - s.w - p.x : p.x;
    let top = !p ? vh - s.h - MARGIN : p.ay === "bottom" ? vh - s.h - p.y : p.y;
    left = clamp(left, EDGE, vw - s.w - EDGE);
    top = clamp(top, EDGE, vh - s.h - EDGE);
    host.style.left = Math.round(left) + "px";
    host.style.top = Math.round(top) + "px";
    if (panelOpen) placePanel();
  }

  /* 松手时把当前视口坐标换算回「靠边的距离」。以按钮中心落在哪一半屏幕来
     决定锚定哪条边：放在右半边就记「离右多远」，缩放窗口时它跟着右边走。 */
  function posFromBox(left, top) {
    const { vw, vh } = viewport();
    const s = hostSize();
    const ax = left + s.w / 2 <= vw / 2 ? "left" : "right";
    const ay = top + s.h / 2 <= vh / 2 ? "top" : "bottom";
    return {
      ax,
      x: Math.round(ax === "left" ? left : vw - s.w - left),
      ay,
      y: Math.round(ay === "top" ? top : vh - s.h - top),
    };
  }

  /* 只在松手/复位那一次写盘：chrome.storage.sync 有每秒写入配额，
     跟着 pointermove 每帧存会把配额打光并触发 onWriteQuotaExceeded。 */
  function savePos(p) {
    lastPosJson = posJson(p);
    try {
      const box = {};
      box[POS_KEY] = p || null;
      chrome.storage.sync.set(box, () => {
        void chrome.runtime.lastError; // 无痕窗口 / 配额满：位置留在本页面即可，别把按钮拖坏
      });
    } catch (_) {}
  }

  /* 位置和「贴边收纳」开关是同一次 get 读回来的：两个键各读一次会多一个来回，
     而且出现「位置已应用、开关还没到」的中间态更难解释。 */
  function loadPos() {
    try {
      const def = {};
      def[POS_KEY] = null;
      def[DOCK_KEY] = true;
      chrome.storage.sync.get(def, (r) => {
        void chrome.runtime.lastError;
        dockOn = r && r[DOCK_KEY] !== false;
        const p = normPos(r && r[POS_KEY]);
        // 读盘是异步的：万一用户已经拖过（或正按住）这颗按钮，不能拿旧存档把他摆好的位置抢回去
        if (!p || touchedByUser || drag) {
          if (dockOn && !pos) armDock(1500); // 默认位置不算贴边，这里只是把计时器备好
          return;
        }
        pos = p;
        lastPosJson = posJson(p);
        applyPos(pos);
        if (canDock()) armDock(1600); // 摆在上次贴边的位置上，页面开一会儿之后自己收起来
      });
    } catch (_) {}
  }

  function resetPos() {
    touchedByUser = true;
    lastInteract = Date.now();
    pos = null;
    setDocked(false); // 复位之后离边 18px，不再是「贴边」状态
    applyPos(pos);
    savePos(pos);
    // 让按钮轻轻弹一下，表示「是这里」，不然归位没有任何反馈
    if (fab) {
      fab.classList.remove("pop");
      void fab.offsetWidth;
      fab.classList.add("pop");
      setTimeout(() => fab && fab.classList.remove("pop"), 260);
    }
  }

  /* ---------- 面板：跟着按钮所在位置决定往哪边开 ---------- */
  function placePanel() {
    if (!panelEl || !host || panelEl.hidden) return;
    const r = host.getBoundingClientRect();
    const { vw, vh } = viewport();
    const GAP = 8;
    // 窄屏（分屏、iPad 竖屏）时按视口收窄，别让面板自己先溢出
    panelEl.style.width = Math.round(clamp(336, 240, vw - EDGE * 2)) + "px";

    // 纵向：哪边空间大就朝哪边开
    const below = vh - r.bottom - GAP - EDGE;
    const above = r.top - GAP - EDGE;
    const openUp = below < above;
    const maxH = Math.round(clamp(openUp ? above : below, 150, 460));
    panelEl.style.maxHeight = maxH + "px";

    const ph = panelEl.offsetHeight || maxH;
    let top = openUp ? -ph - GAP : (host.offsetHeight || r.height) + GAP;
    // 上面实在放不下（按钮被拖到很靠上的位置）时翻回下面
    if (openUp && r.top - ph - GAP < EDGE) top = (host.offsetHeight || r.height) + GAP;
    panelEl.style.top = Math.round(top) + "px";
    panelEl.style.bottom = "auto";

    // 横向：面板右边缘对齐按钮右边缘（旧版观感），放不下再整体夹回视口
    const pw = panelEl.offsetWidth || 336;
    const left = clamp(r.right - pw, EDGE, Math.max(EDGE, vw - EDGE - pw));
    panelEl.style.right = "auto";
    panelEl.style.left = Math.round(left - r.left) + "px";
  }

  /* 离边多近才算「贴在边上」：默认位置离边 18px，不算贴边，所以刚装上时不会乱收 */
  const flushEdge = (p) => !!p && p.x <= DOCK_TOL;

  function canDock() {
    if (!dockOn || !flushEdge(pos) || panelOpen || drag || hovering || touching) return false;
    // 光标常正停在按钮上（松手时它就在那儿），刚交互过就收会「一缩又弹回」
    return Date.now() - lastInteract >= DOCK_GUARD;
  }

  /* 收起/展开的唯一入口。canDock() 带着全部前提（开关、贴边、面板没开、没在拖、
     光标不在按钮上、刚没交互过），任何一条不成立都拍回 false，调用方不用各自判。 */
  function setDocked(on) {
    if (!fab) return;
    const want = !!on && canDock();
    if (want === docked) {
      if (want) applyPos(pos); // 已经收起但视口/尺寸变了：重新贴一次边
      return;
    }
    docked = want;
    fab.classList.toggle("docked", want);
    // 收起后按钮窄了一截（CSS 收成 42px 圆胶囊）。存的是「离边多远」，
    // 所以重算一次坐标它就继续贴边 —— 换成存绝对 left 这一步就会跑偏。
    applyPos(pos);
  }

  function armDock(ms) {
    stopDockTimer();
    if (!dockOn || panelOpen || drag) {
      setDocked(false);
      return;
    }
    dockTimer = setTimeout(() => {
      dockTimer = null;
      setDocked(true);
      // 只差「刚交互过」这一条时（光标就停在按钮上），等守卫期过了自己再试一次。
      // 不补这一刀的话，定时器一次落空就没人再叫它 —— 除非用户又动了一下鼠标。
      if (!docked && canDockSoon()) armDock(DOCK_GUARD + 150);
    }, ms == null ? DOCK_DELAY : ms);
  }

  /* 除了「刚没交互过」之外都成立：给定时器自己续一次命用 */
  function canDockSoon() {
    return dockOn && flushEdge(pos) && !panelOpen && !drag && !hovering && !touching;
  }

  function stopDockTimer() {
    if (dockTimer) clearTimeout(dockTimer);
    dockTimer = null;
  }

  /* 任何一次真实交互都记一笔：收纳计时得从「手真正离开」开始算。
     只靠 pointerleave 不够 —— 用户不会特地把光标移出那颗小按钮。 */
  function touch() {
    lastInteract = Date.now();
    stopDockTimer();
  }

  /* 松手时把横向位置吸到最近的那条竖边（纵向仍然由用户自己摆）。 */
  function snapToEdge() {
    const { vw } = viewport();
    const s = hostSize();
    const r = host.getBoundingClientRect();
    const toLeft = r.left + s.w / 2 <= vw / 2;
    const left = clamp(toLeft ? SNAP_X : vw - s.w - SNAP_X, EDGE, Math.max(EDGE, vw - s.w - EDGE));
    host.style.left = Math.round(left) + "px";
    return posFromBox(left, parseFloat(host.style.top) || r.top);
  }

  function openPanel() {
    if (!panelEl) return;
    panelOpen = true;
    touch();
    setDocked(false); // 面板要贴着一颗看得清的按钮展开
    panelEl.hidden = false;
    renderList(); // renderList 末尾会再 placePanel 一次（列表变长，高度也变）
    placePanel();
  }

  function closePanel() {
    panelOpen = false;
    if (panelEl) panelEl.hidden = true;
    lastInteract = Date.now();
    armDock(); // 面板关掉之后过一会儿再收回去
  }

  function build() {
    host = document.createElement("div");
    host.id = "zdm-sniffer-host";
    // 固定定位 + 挂在 documentElement 上：避开 position:fixed 与站点
    // transform/contain 的包含块陷阱（那颗按钮会跟着某个容器一起被裁掉）。
    // 左右坐标由 applyPos 按「靠哪条边多远」算出来，初始给右下角。
    host.style.cssText =
      "all:initial;position:fixed;left:0;top:0;z-index:2147483647;";
    shadow = host.attachShadow({ mode: "open" });
    // 界面语言（ui18n.js）：只观察自己这棵 shadow 树，不去遍历宿主页面的 DOM。
    try { if (typeof ZDMUI !== "undefined") ZDMUI.watch(shadow); } catch (_) {}
    shadow.innerHTML = `
      <style>
        :host{all:initial}
        .fab{all:initial;position:relative;display:flex;align-items:center;gap:6px;height:40px;
          padding:0 14px 0 12px;border-radius:999px;cursor:grab;
          background:#2563eb;color:#fff;font:600 13px/1 -apple-system,BlinkMacSystemFont,
          "Segoe UI","Noto Sans CJK SC",sans-serif;border:none;
          box-shadow:0 4px 14px rgba(37,99,235,.4);transition:.15s;font-family:inherit;
          touch-action:none;user-select:none;-webkit-user-select:none;
          -webkit-user-drag:none}
        .fab:hover{background:#1d4fd8}
        .fab.grabbing{cursor:grabbing;transition:none;
          box-shadow:0 10px 26px rgba(15,23,42,.38)}
        .fab.pop{animation:zdm-pop .22s ease-out}
        @keyframes zdm-pop{0%{transform:scale(1)}40%{transform:scale(1.14)}100%{transform:scale(1)}}
        .fab svg{width:17px;height:17px;flex:none}
        /* 贴边收纳态：收成一颗半透明圆点，文字与计数徽标让位，
           有媒体时用一个绿点代替（整颗都还在屏内，触屏也点得到）。 */
        .fab.docked{width:42px;height:42px;padding:0;gap:0;justify-content:center;
          border-radius:50%;opacity:.55;box-shadow:0 2px 10px rgba(15,23,42,.22);
          transition:opacity .18s,width .18s,height .18s}
        .fab.docked:hover,.fab.docked:focus-visible{opacity:1}
        .fab.docked .lbl{display:none}
        .fab.docked .badge{position:absolute;top:-3px;right:-3px;min-width:10px;height:10px;
          padding:0;border-radius:999px;border:2px solid #fff;background:#22c55e;
          color:transparent;font-size:0;line-height:10px}
        .fab.docked[data-empty="1"] .badge{display:none}
        .badge{all:initial;min-width:18px;height:18px;padding:0 5px;border-radius:999px;
          background:#fff;color:#2563eb;font:700 11px/18px -apple-system,sans-serif;
          text-align:center;font-family:inherit}
        .fab[data-empty="1"]{background:#475569;box-shadow:0 4px 14px rgba(15,23,42,.28)}
        .fab[data-empty="1"] .badge{background:transparent;color:#fff}
        .panel{all:initial;display:flex;flex-direction:column;position:absolute;width:336px;
          max-height:min(62vh,460px);overflow:hidden;border-radius:12px;background:#fff;
          border:1px solid #e3e8ef;box-shadow:0 18px 50px rgba(15,23,42,.22);
          font:13px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans CJK SC",sans-serif;
          color:#1f2937;font-family:inherit}
        .panel[hidden]{display:none}
        .hd{display:flex;align-items:center;gap:8px;padding:11px 13px;flex:none;
          border-bottom:1px solid #eef1f5;font-weight:700;font-size:13px}
        .hd .dot{width:7px;height:7px;border-radius:50%;background:#2563eb;flex:none}
        .hd .x{margin-left:auto;all:initial;cursor:pointer;color:#94a3b8;font-size:16px;
          line-height:1;padding:2px 4px;border-radius:6px;font-family:inherit}
        .hd .x:hover{background:#f1f5f9;color:#475569}
        .list{flex:1 1 auto;min-height:0;max-height:min(46vh,340px);overflow-y:auto;
          margin:0;padding:0;list-style:none}
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
        .hd .rst{all:initial;cursor:pointer;border:none;border-radius:6px;
          background:#f1f5f9;color:#64748b;font:600 13px/1 -apple-system,sans-serif;
          padding:5px 8px;flex:none;font-family:inherit}
        .hd .rst:hover{background:#e2e8f0;color:#334155}
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
          .hd .rst{background:#243244;color:#94a3b8}
          .hd .rst:hover{background:#334155;color:#e2e8f0}
          .fab[data-empty="1"]{background:#334155}
        }
      </style>
      <button class="fab" type="button" title="ZDM 嗅探到的媒体（可拖动，双击复位）">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round" stroke-linejoin="round">
          <path d="m10 8 6 4-6 4V8z"/><rect x="2" y="4" width="20" height="16" rx="2"/>
        </svg>
        <span class="lbl">嗅探</span><span class="badge">0</span>
      </button>
      <div class="panel" hidden>
        <div class="hd"><span class="dot"></span><span class="t">检测到媒体</span>
          <button class="imgb" type="button" title="扫描页面里的大图（图库/壁纸/漫画站）">扫图片</button>
          <button class="rst" type="button" title="把嗅探按钮移回默认位置（右下角）">↺</button>
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
    panelEl = shadow.querySelector(".panel");
    // 先按默认位置落位（旧版同样是右下角），存档里有的话由 loadPos 覆盖
    applyPos(pos);

    /* ---- 点击和拖动怎么分家 ----
       起点记下，移动超过 4px 才算拖动（手抖不该让面板闪一下）；真拖过之后
       浏览器还会补发一次 click，那颗按钮不能被它顺手开关面板。
       触屏靠 .fab 上的 touch-action:none 挡掉页面滚动，这里不调
       preventDefault —— 一旦 cancel 掉 pointerdown，触摸路径上的 click 可能
       就不再派发，按钮会变成「只能拖、点不开」。 */
    const DRAG_SLOP = 4;

    fab.addEventListener("pointerdown", (e) => {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      touch();
      // 手指/光标都已经按在按钮上了，收起状态必须先展开再谈拖动
      if (docked) setDocked(false);
      const r = host.getBoundingClientRect();
      drag = {
        id: e.pointerId,
        dx: e.clientX - r.left,
        dy: e.clientY - r.top,
        sx: e.clientX,
        sy: e.clientY,
        moved: false,
      };
      try {
        fab.setPointerCapture(e.pointerId);
      } catch (_) {}
    });

    fab.addEventListener("pointermove", (e) => {
      touch();
      if (!drag || drag.id !== e.pointerId) return;
      if (!drag.moved) {
        if (
          Math.abs(e.clientX - drag.sx) < DRAG_SLOP &&
          Math.abs(e.clientY - drag.sy) < DRAG_SLOP
        ) {
          return;
        }
        drag.moved = true;
        fab.classList.add("grabbing");
        if (panelOpen) closePanel(); // 拖的时候面板只会挡视线
      }
      const { vw, vh } = viewport();
      const s = hostSize();
      host.style.left =
        Math.round(clamp(e.clientX - drag.dx, EDGE, vw - s.w - EDGE)) + "px";
      host.style.top =
        Math.round(clamp(e.clientY - drag.dy, EDGE, vh - s.h - EDGE)) + "px";
      e.preventDefault(); // 只在真拖起来之后调用：挡住选中文字/图片拖拽
    });

    const endDrag = (e) => {
      if (!drag || (e && drag.id !== e.pointerId)) return;
      const moved = drag.moved;
      drag = null;
      fab.classList.remove("grabbing");
      touch();
      if (!moved) return;
      suppressClick = true;
      // 正常情况这次 click 紧跟着就来；400ms 兜底，免得异常路径上吞掉下次真点击
      setTimeout(() => {
        suppressClick = false;
      }, 400);
      const r = host.getBoundingClientRect();
      // 收纳开着就把手横向吸到最近的那条边（纵向仍按用户摆的位置存）；
      // 关掉收纳时这里是纯自由摆放，一个像素都不动。
      pos = dockOn ? snapToEdge() : posFromBox(r.left, r.top);
      touchedByUser = true;
      savePos(pos); // 松手才写盘：pointermove 每帧写会把 storage.sync 的配额打光
      armDock();
    };
    fab.addEventListener("pointerup", endDrag);
    fab.addEventListener("pointercancel", endDrag);
    fab.addEventListener("lostpointercapture", endDrag);

    /* 光标在不在按钮上，直接决定「该不该收」。松手那一刻光标通常还压在按钮上，
       pointerleave 不来就永远不该收。 */
    fab.addEventListener("pointerenter", () => {
      hovering = true;
      touching = true;
      touch();
      if (docked) setDocked(false);
    });
    fab.addEventListener("pointerleave", () => {
      hovering = false;
      // pointerleave 是「光标确实离开了」最可靠的信号：mousemove 记账的 touching
      // 必须在这里一起清掉，否则收过一次之后 touching 会永远卡在 true，再也收不回来。
      touching = false;
      touch();
      armDock();
    });
    fab.addEventListener("focus", () => {
      touch();
      if (docked) setDocked(false);
    });
    fab.addEventListener("blur", () => {
      touch();
      armDock();
    });

    /* 鼠标停在按钮上不动时 pointerleave 永远不会来（松手时它就在原地），而全局
       mousemove 每帧去读按钮矩形又太贵（浏览器要算布局）。只在「确实收到了
       mousemove」时记账一次，配合 canDock() 里的 DOCK_GUARD，行为就稳了。 */
    window.addEventListener(
      "mousemove",
      (e) => {
        if (!dockOn || docked) return;
        const r = fab.getBoundingClientRect();
        if (!r || !r.width) return;
        const inside =
          e.clientX >= r.left - 12 && e.clientX <= r.right + 12 &&
          e.clientY >= r.top - 12 && e.clientY <= r.bottom + 12;
        if (inside && !touching) {
          touching = true;
          touch();            // 光标在按钮上：别收
        } else if (!inside && touching) {
          // 这是「鼠标确实在按钮上待过、现在真的移开了」的那一刻 —— 重新计时。
          // 只靠 pointerleave 不够：松手时 pointer capture 把 enter/leave 的路径打乱了。
          touching = false;
          touch();
          armDock();
        }
      },
      true
    );
    document.addEventListener("keydown", () => touch(), true);

    fab.addEventListener("click", () => {
      touch();
      if (suppressClick) {
        suppressClick = false;
        return;
      }
      if (drag) return; // 还按着（长按未松）时不响应键盘 click
      if (docked) setDocked(false); // 收起态点第一下先展开，别让人以为按不动
      if (panelOpen) closePanel();
      else openPanel();
    });
    // 双击复位：不搬键盘也能把按钮放回默认右下角
    fab.addEventListener("dblclick", () => {
      suppressClick = false;
      resetPos();
    });
    panelEl.querySelector(".hd .x").addEventListener("click", closePanel);
    panelEl
      .querySelector(".hd .rst")
      .addEventListener("click", () => {
        resetPos(); // 面板仍然开着，只是整组跟着按钮回到右下角
      });
    document.addEventListener("click", (e) => {
      if (!panelOpen) return;
      if (host.contains(e.target)) return;
      closePanel();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && panelOpen) closePanel();
    });
  }

  /* 按钮现在能拖动、贴边还会自己收小，光看图标猜不出来，所以把用法写进悬停提示。
     英文界面由 ui18n.js 的 T_RULES 认这句尾巴（它是拼在计数文案后面的，
     整句 T_EXACT 匹配不上）。 */
  const DRAG_HINT = "（可拖动，双击复位）";

  function renderFab() {
    if (!fab) return;
    const n = items.length;
    badge.textContent = n > 99 ? "99+" : String(n);
    fab.dataset.empty = n ? "0" : "1";
    fab.title =
      (n ? `ZDM 嗅探到 ${n} 个媒体` : "ZDM 嗅探（暂无媒体）") + DRAG_HINT;
    if (titleEl) titleEl.textContent = n ? `检测到 ${n} 个媒体` : "暂无媒体";
    // 角标变宽（0 → 100+）会让按钮宽度变几像素；靠「贴哪条边」存的位置
    // 由 applyPos 重新对齐，这里只需在面板开着时把它挪回来。
    if (panelOpen) placePanel();
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
      if (panelOpen) placePanel(); // 内容变了高度就变了，展开方向要重算
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
    if (panelOpen) placePanel();
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
       页面上只有音频播放列表（SoundCloud/播客）时，主轨就是它自己：
       文件名和容器按音频处理，不能生成一个没有画面的 mp4。
       页面上没有播放列表时不推（宁可不响应，也别交出一堆碎片）。 */
    if (it.seg || (it.kind === "hls" && isAudioPlaylistUrl(it.url))) {
      const pair = pickStreamPair(items);
      if (!pair) return false;
      target = { url: pair.url, kind: pair.kind, audio: pair.audio };
      audioUrl = pair.audioUrl;
      filename = smartTitle() + (pair.audio ? ".m4a" : ".mp4");
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
      // 点中的本身就是音频清单时，不配对、按音频收尾
      const selfAudio = !!it.audio || isAudioPlaylistUrl(it.url);
      if (selfAudio) audioUrl = "";
      filename = smartTitle() + (selfAudio ? ".m4a" : ".mp4");
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
    const audioOnly =
      target.kind === "hls" || target.kind === "dash"
        ? !!target.audio || isAudioPlaylistUrl(target.url)
        : false;
    const body = {
      url: target.url,
      filename,
      referer: page,
      // 只有同站才带 cookie，见 sameSite() 的说明
      cookie: sameSite(it.url, page) ? document.cookie || undefined : undefined,
      source: "sniffer",
      kind: target.kind || it.kind || "video",
      // 纯音频清单：后端按音频容器收尾
      audio: audioOnly || undefined,
      audio_only: audioOnly || undefined,
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
    /* 贴边收纳补一刀：旧版本存下的贴边位置（没有经过这次的松手事件）也要能收。
       已经有计时器在等就什么都不做，别把倒计时重置成永远轮不到。 */
    if (dockOn && !docked && !dockTimer && !panelOpen && canDock()) armDock(1500);
  }

  /* 从 URL 猜图片扩展名：优先真实后缀；其次 query 里的 format= 参数
     （图床 CDN 常见 ?x-oss-process=image/format,webp 这种地址本身没后缀）；
     猜不到给 jpg —— 查看器兼容性最好。以前无后缀 URL 直接以 "12345" 这种
     名字推送，存下来的文件没有扩展名，用户根本打不开。 */
  function imgExtOf(url) {
    let m = /\.(jpg|jpeg|png|webp|gif|avif|bmp)(?:$|[?#])/i.exec(url);
    if (m) return m[1].toLowerCase() === "jpeg" ? "jpg" : m[1].toLowerCase();
    m = /(?:format|fm|type|output)[=/,](?:image\/format[=/,])?(webp|png|jpe?g|gif|avif)/i.exec(url);
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
  const IMG_EXT = /\.(jpg|jpeg|png|webp|gif|avif|bmp)(?:$|[?#])/i;
  /* 图床/CDN 常见「URL 没后缀，格式在查询参数里」：
     ?x-oss-process=image/format,webp、?fm=webp、?type=image/png。 */
  function looksImageUrl(u) {
    if (!u) return false;
    if (IMG_EXT.test(u)) return true;
    return /[?&](?:format|fm|type|output)=[^&#]*?(?:webp|png|jpe?g|gif|avif)\b/i.test(u);
  }
  function bestFromSrcset(ss) {
    if (!ss) return "";
    let best = "";
    let bestW = -1;
    for (const part of ss.split(",")) {
      const seg = part.trim().split(/\s+/);
      if (!seg[0]) continue;
      // 描述符可能是 480w 或 2x；都按「越大越清晰」比较
      const w = seg[1] ? parseFloat(seg[1]) : 0;
      if (w >= bestW) {
        bestW = w;
        best = seg[0];
      }
    }
    return best;
  }
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
    const addSrcset = (ss, el) => {
      const best = bestFromSrcset(ss);
      if (best) add(best, el);
    };
    /* 懒加载属性五花八门：不同的图库 CMS 各用各的，漏一个就少一批图。 */
    const IMG_ATTRS = [
      "src",
      "data-src",
      "data-original",
      "data-original-src",
      "data-lazy-src",
      "data-lazysrc",
      "data-actualsrc",
      "data-echo",
    ];
    document.querySelectorAll("img").forEach((el) => {
      for (const a of IMG_ATTRS) {
        const v = el.getAttribute(a);
        if (v) add(v, el);
      }
      const dataUrl = el.getAttribute("data-url");
      if (dataUrl && looksImageUrl(dataUrl)) add(dataUrl, el);
      addSrcset(el.getAttribute("srcset"), el);
      addSrcset(el.getAttribute("data-srcset"), el);
    });
    // <picture><source srcset>：响应式图库的原图常常只在这里
    document.querySelectorAll("picture source").forEach((el) => {
      addSrcset(el.getAttribute("srcset"), el);
      addSrcset(el.getAttribute("data-srcset"), el);
      const src = el.getAttribute("src") || el.getAttribute("data-src") || "";
      if (src) add(src, el);
    });
    // 单图/分享页：页面只在 meta 里给出原图
    document
      .querySelectorAll(
        'meta[property="og:image"],meta[name="twitter:image"],link[rel="image_src"]'
      )
      .forEach((el) => {
        const v = el.getAttribute("content") || el.getAttribute("href") || "";
        if (v) add(v, null);
      });
    // 直接链到原图的 <a href>：图库站的「查看原图」多是这种（含无后缀 CDN）
    let anchors = 0;
    document.querySelectorAll("a[href]").forEach((a) => {
      if (++anchors > 4000) return;
      const href = a.getAttribute("href") || "";
      if (looksImageUrl(href)) add(href, null);
    });
    // 背景图：壁纸/作品集站把原图放在 inline style 或 data-bg 里
    let bgs = 0;
    document
      .querySelectorAll(
        '[data-bg],[data-background],[data-bg-src],[class*="bg"],[class*="background"],[class*="cover"],[class*="hero"],[style*="background"]'
      )
      .forEach((el) => {
        if (++bgs > 500) return;
        for (const a of ["data-bg", "data-background", "data-bg-src"]) {
          const v = el.getAttribute(a);
          if (v && looksImageUrl(v)) add(v, el);
        }
        let bg = el.getAttribute("style") || "";
        if (!/background/i.test(bg)) {
          try {
            bg = getComputedStyle(el).backgroundImage || "";
          } catch (_) {
            bg = "";
          }
        }
        const re = /url\(\s*(['"]?)(.*?)\1\s*\)/gi;
        let m;
        while ((m = re.exec(bg))) {
          if (m[2] && !/^data:/i.test(m[2])) add(m[2], el);
        }
      });
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
    const imgb = shadow.querySelector(".hd .imgb");
    if (imgb) {
      imgb.addEventListener("click", () => {
        const n = addImages();
        openPanel();
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

    /* ---------- 位置：读存档、跟着窗口变化夹紧、跨标签页同步 ---------- */

    // 存档是异步读回来的：先按默认右下角出现，读到之后立刻挪到用户放的位置。
    loadPos();

    /* 视口变了（缩放窗口、F11 全屏、手机转屏、分屏拖动）就把按钮夹回可视区。
       不加这一条，拖到右下角的按钮在窗口变窄之后会留在屏幕外，再也拖不回来 ——
       那是「可拖动」最容易被投诉成的那种翻车。 */
    let placeTimer = null;
    const onViewportChange = () => {
      if (placeTimer) clearTimeout(placeTimer);
      placeTimer = setTimeout(() => {
        applyPos(pos);
        // 视口变了之后按钮还贴不贴边可能就不一样了，重新评估一次
        if (docked && !canDock()) setDocked(false);
        else if (canDock()) setDocked(true);
      }, 120);
    };
    window.addEventListener("resize", onViewportChange, true);
    window.addEventListener("orientationchange", onViewportChange);

    // 同一浏览器多个标签页里那颗按钮应当待在同一个地方（A 站、B 站来回切时尤其明显）
    try {
      chrome.storage.onChanged.addListener((changes, area) => {
        if (area !== "sync" || !changes) return;
        // 设置页刚把「贴边收纳」关掉 → 立刻展开并停止计时；打开 → 该收就收
        if (changes[DOCK_KEY]) {
          const on = changes[DOCK_KEY].newValue !== false;
          if (on !== dockOn) {
            dockOn = on;
            touching = false;
            if (!on) {
              stopDockTimer();
              setDocked(false);
            } else if (canDock()) armDock(2000);
          }
        }
        if (!changes[POS_KEY]) return;
        const p = normPos(changes[POS_KEY].newValue);
        const j = posJson(p);
        if (j === lastPosJson) return; // 自己刚写的那一份，不用再应用一遍
        lastPosJson = j;
        pos = p;
        if (drag) return; // 正按着按钮的时候别抢位置
        applyPos(pos);
        if (!canDock()) setDocked(false);
        else armDock(2500);
      });
    } catch (_) {}

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
    const audioOnly = !!main.audio || isAudioPlaylistUrl(main.url);
    if (audioOnly) audioUrl = "";
    return {
      url: main.url,
      kind: main.kind,
      audioUrl,
      audio: audioOnly || undefined,
    };
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
      open: () => openPanel(),
      close: () => closePanel(),
      // 位置对外露一手，将来「设置页」想做「重置按钮位置」可以直接调
      resetPosition: () => resetPos(),
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
