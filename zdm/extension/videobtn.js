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

  const sites = window.__zdmSites;
  if (!sites) {
    // sites.js 必须先加载（manifest 里 content_scripts 的 js 数组按顺序执行）
    return;
  }

  const site = sites.siteOf();
  if (!site) return; // 只在支持的站点启用

  const S = sites.SITES[site];

  // host 用 data-zdm-vbtn=<角色>；卡片本身用另一个属性，
  // 否则 querySelectorAll("[data-zdm-vbtn=btn]") 会同时命中卡片和它的 host。
  const MARK = "data-zdm-vbtn";
  const CARD_MARK = "data-zdm-card";
  const BTN_MARK = "data-zdm-btn";
  let openMenu = null; // 当前打开的菜单 { el, card }
  let pending = null; // 正在解析格式的卡片

  /* 按钮与菜单都活在 Shadow DOM 里，light DOM 查不到，
     所以要把引用记在卡片自己的 dataset 上。 */
  const btnOf = (card) => card.__zdmBtn || null;

  const esc = (s) =>
    String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );

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
:host { all: initial; }
.btn {
  position: absolute;
  top: 8px; right: 8px;
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
.btn:hover { background: rgba(0,0,0, .92); }
.btn svg { width: 13px; height: 13px; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
.btn.busy { opacity: 1; pointer-events: auto; }

.menu {
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
    // 卡片必须能定位，否则 absolute 定位会跑到页面别处
    if (getComputedStyle(card).position === "static") {
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
    card.appendChild(host);
    // 记在卡片上：Shadow DOM 里的元素无法用 querySelector 找回来。
    card.__zdmBtn = btn;
    card.__zdmBtnHost = host;
    card.setAttribute(CARD_MARK, "1");
    return btn;
  }

  /* ------------------------------------------------------------ 菜单 */

  function closeMenu() {
    if (!openMenu) return;
    const { root, host } = openMenu;
    root.querySelector(".menu")?.remove();
    host.remove();
    openMenu = null;
  }

  function openMenuFor(card, btn) {
    closeMenu();
    const { host, root } = makeHost("menu");
    const menu = document.createElement("div");
    menu.className = "menu";
    root.appendChild(menu);
    card.appendChild(host);
    openMenu = { card, host, root, btn };

    const info = sites.formatsForCard(card);
    if (!info) {
      menu.innerHTML = '<div class="tip">没能识别这个视频卡片。</div>';
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
    if (openMenu && openMenu.card !== e.target) closeMenu();
  }

  function onMouseMove(e) {
    const card = cardOf(e.target);
    if (!card) {
      if (openMenu) closeMenu();
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
    const card = el.closest(S.cards);
    if (!card) return null;
    if (card === document.body || card === document.documentElement) return null;
    return card;
  }

  /* 判断事件是不是发生在我们自己的按钮上。 */
  function isOurButton(target) {
    let el = target;
    if (!el) return false;
    if (el.getRootNode) {
      const root = el.getRootNode();
      // 事件已经在 shadow 内部：看它自己或它的祖先有没有我们的标记
      if (root && root.host) {
        let n = el;
        while (n && n.getRootNode && n.getRootNode() === root) {
          if (n.hasAttribute && n.hasAttribute(BTN_MARK)) return true;
          n = n.parentElement || n.parentNode;
          if (!n || n.nodeType !== 1) break;
        }
        return false;
      }
    }
    return !!(el.closest && el.closest("[" + BTN_MARK + "]"));
  }

  function onClick(e) {
    if (!isOurButton(e.target)) return;
    e.preventDefault();
    e.stopPropagation();
    const card = cardOf(e.target);
    if (!card) return;
    openMenuFor(card, card.__zdmBtn);
  }

  function onKey(e) {
    if (e.key === "Escape") closeMenu();
  }

  /* ------------------------------------------------------------ 扫描 */

  function scan() {
    if (!S.cards) return;
    const cards = document.querySelectorAll(S.cards);
    for (const card of cards) {
      if (card.__zdmBtn) continue;
      // body / html 也能匹配 cardInfo 里的宽松选择器（它们包含整页的链接），
      // 绝不能把按钮挂到 body 上——那会变成一个悬浮在整页左上角的怪东西。
      if (card === document.body || card === document.documentElement) continue;
      // 只给能识别出视频链接的卡片装按钮
      try {
        if (!S.cardInfo(card)) continue;
      } catch (_) {
        continue;
      }
      ensureButton(card);
    }
  }

  function start() {
    if (!document.documentElement) {
      setTimeout(start, 200);
      return;
    }
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

  start();
})();
