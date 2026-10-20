/* ZDM Connector - 站点适配器（注入到页面的 MAIN world）
 *
 * 为什么要 MAIN world：
 *   视频站的媒体数据（__playinfo__ / ytInitialPlayerResponse）是页面自己的
 *   JS 变量，而 content script 默认跑在 isolated world —— 与页面共享 DOM，
 *   但**不共享 JS 上下文**，读到的 window.__playinfo__ 永远是 undefined。
 *   Chrome MV3 给 content_scripts 加了 "world": "MAIN"，脚本就注入到页面
 *   主世界，能直接读这些变量。
 *
 * 代价：MAIN world 的脚本与页面脚本共享全局作用域，所以这里只挂一个
 * 带前缀的对象（window.__zdmSites），不污染别的名字。
 *
 * 职责：把「页面上的一个视频」翻译成「可下载的清晰度列表」。
 *   - YouTube：播放器实例的 streamingData
 *   - B站：__playinfo__ / __INITIAL_STATE__ 里的 dash（DASH 或 durl）
 */

(() => {
  if (window.__zdmSites) return;
  window.__zdmSites = {};

  const S = window.__zdmSites;
  S.abs = (u) => {
    try {
      return new URL(u, location.href).href;
    } catch (_) {
      return "";
    }
  };
  const num = (v) => {
    const n = Number(v);
    return Number.isFinite(n) ? n : 0;
  };

  /* ---------------------------------------------------------------- YouTube */

  function ytPlayerResponse() {
    const w = window;
    // 1) 页面初始化时注入的
    let pr = w.ytInitialPlayerResponse;
    // 2) 播放器启动参数里的（字符串形式）
    if ((!pr || !pr.streamingData) && w.ytplayer && w.ytplayer.config && w.ytplayer.config.args) {
      const raw = w.ytplayer.config.args.player_response || w.ytplayer.config.args.raw_player_response;
      if (raw) {
        try {
          pr = typeof raw === "string" ? JSON.parse(raw) : raw;
        } catch (_) {
          pr = null;
        }
      }
    }
    if (pr && pr.streamingData) return pr.streamingData;
    // 3) 播放器实例上的（播放过一次之后）
    const p = document.getElementById("movie_player");
    if (p && typeof p.getPlayerResponse === "function") {
      try {
        const r = p.getPlayerResponse();
        if (r && r.streamingData) return r.streamingData;
      } catch (_) {
        /* 播放器还没准备好 */
      }
    }
    return null;
  }

  function ytLabel(f, kind) {
    const h = num(f.height);
    if (kind === "audio") {
      const q = f.audioQuality || "";
      const br = num(f.audioSampleRate) / 1000;
      const bits = [];
      if (q === "AUDIO_QUALITY_LOW") bits.push("低音质");
      else if (q === "AUDIO_QUALITY_MEDIUM") bits.push("中音质");
      else if (q === "AUDIO_QUALITY_HIGH") bits.push("高音质");
      if (br) bits.push(br + " kHz");
      return "音频 · " + (bits.join(" · ") || "默认");
    }
    const w = num(f.width);
    const parts = [];
    if (h) parts.push(h + "P");
    else if (w) parts.push(w + "×" + (f.height || "?"));
    if (kind === "video") {
      // 自适应流只有视频轨；后端会下载配套音频轨并用 ffmpeg 合并
      parts.push("视频+音频");
    }
    if (f.qualityLabel) {
      const ql = String(f.qualityLabel);
      if (!parts.includes(ql)) parts.push(ql);
    }
    if (num(f.fps)) parts.push(num(f.fps) + "fps");
    return parts.join(" · ") || "默认";
  }

  function ytFormats() {
    const sd = ytPlayerResponse();
    if (!sd) return [];
    const out = [];
    const push = (f, kind) => {
      if (!f || !f.url) return;
      out.push({
        label: ytLabel(f, kind),
        url: f.url,
        kind: kind,
        size: num(f.contentLength),
        mime: f.mimeType || "",
        width: num(f.width),
        height: num(f.height),
        itag: num(f.itag),
        audioQuality: f.audioQuality || "",
        bitrate: num(f.bitrate),
      });
    };
    (sd.formats || []).forEach((f) => push(f, "video+muxed"));
    (sd.adaptiveFormats || []).forEach((f) => {
      const m = f.mimeType || "";
      if (m.startsWith("video/")) push(f, "video");
      else if (m.startsWith("audio/")) push(f, "audio");
    });
    return out;
  }

  const YT_CARDS = [
    "ytd-rich-item-renderer",
    "ytd-video-renderer",
    "ytd-grid-video-renderer",
    "ytd-compact-video-renderer",
    "ytd-playlist-video-renderer",
    "ytd-channel-video-renderer",
    "ytd-movie-renderer",
  ].join(",");

  function ytCardInfo(card) {
    const a = card.querySelector(
      "a#video-title-link, a#thumbnail, a[href*='/watch'], a[href*='/shorts/']"
    );
    const href = a && a.getAttribute("href");
    if (!href) return null;
    const pageUrl = S.abs(href);
    if (!/\/watch|\/shorts\//.test(pageUrl)) return null;
    const titleEl = card.querySelector("#video-title, yt-formatted-string#video-title, h3");
    const title = ((titleEl && (titleEl.getAttribute("title") || titleEl.textContent)) || "").trim();
    const img = card.querySelector("img");
    const cover = img ? img.getAttribute("src") || "" : "";
    const durEl = card.querySelector(
      "#text.ytd-thumbnail-overlay-time-status-renderer, .ytd-thumbnail-overlay-time-status-renderer, #overlays #text"
    );
    const duration = ((durEl && durEl.textContent) || "").trim();
    return { pageUrl, title: title || "YouTube 视频", cover, duration };
  }

  /* ---------------------------------------------------------------- B站 */

  const BILI_QUALITY = {
    6: "240P 极速", 16: "360P 清晰", 32: "480P 流畅", 64: "720P 高清",
    74: "720P 高清", 80: "1080P 高清", 112: "1080P 高码率", 116: "1080P 60帧",
    120: "4K 超清", 125: "HDR 真彩", 126: "杜比视界", 127: "8K 超高清",
  };

  function biliPlayInfo() {
    const w = window;
    const raw = w.__playinfo__ || (w.__INITIAL_STATE__ && w.__INITIAL_STATE__.playInfo);
    if (!raw) return null;
    if (typeof raw === "string") {
      try {
        return JSON.parse(raw);
      } catch (_) {
        return null;
      }
    }
    return raw;
  }

  function biliDashInfo() {
    const w = window;
    const st = w.__INITIAL_STATE__;
    const play = biliPlayInfo();
    // __playinfo__ 的形状是 { data: { dash: {...} } }，也有直接 { dash: {...} } 的。
    const cands = [];
    if (play) {
      cands.push(play.data && play.data.dash, play.dash, play.data, play);
    }
    if (st) {
      cands.push(st.videoData && st.videoData.dash, st.dash, st.playInfo && st.playInfo.dash);
    }
    for (const c of cands) {
      if (!c) continue;
      if (c.dash || c.durl || (c.video && c.audio)) return c;
    }
    return null;
  }

  function biliLabel(qid, suffix) {
    const base = BILI_QUALITY[qid] || (qid ? qid + "P" : "默认");
    return base + (suffix ? " · " + suffix : "");
  }

  function biliAudioLabel(a) {
    const br = num(a && a.bandwidth) / 1000;
    const parts = ["音频"];
    if (br) parts.push(Math.round(br) + " kbps");
    const codec = ((a && a.codecs) || "").split(".")[0];
    if (codec) parts.push(codec);
    return parts.join(" · ");
  }

  function biliFormats() {
    const dash = biliDashInfo();
    if (!dash) return [];
    const out = [];

    if (Array.isArray(dash.video)) {
      for (const v of dash.video) {
        const url = v && v.baseUrl;
        if (!url) continue;
        out.push({
          label: biliLabel(num(v.id), "视频+音频"),
          url,
          kind: "video",
          size: 0,
          mime: v.mimeType || "video/mp4",
          width: num(v.width),
          height: num(v.height),
          quality: num(v.id),
          codec: (v.codecs || "").split(".")[0] || "",
        });
      }
      for (const a of dash.audio || []) {
        const url = a && a.baseUrl;
        if (!url) continue;
        out.push({
          label: biliAudioLabel(a),
          url,
          kind: "audio",
          size: 0,
          mime: a.mimeType || "audio/mp4",
          width: 0,
          height: 0,
          quality: num(a.id),
          codec: (a.codecs || "").split(".")[0] || "",
        });
      }
    }

    // 老式 durl：整个视频合在一个文件里（自带声音）
    if (Array.isArray(dash.durl)) {
      for (const d of dash.durl) {
        if (!d || !d.url) continue;
        out.push({
          label: biliLabel(num(d.quality), "视频+音频"),
          url: d.url,
          kind: "video+muxed",
          size: num(d.size),
          mime: "video/mp4",
          width: 0,
          height: 0,
          quality: num(d.quality),
          codec: "",
        });
      }
    }
    return out;
  }

  const BILI_CARDS = [
    "div.bili-video-card",
    "div.video-page-card-small",
    "li.bili-rank-list-video__item",
    "div.video-card",
    "li.rank-item",
    "div.video-card-reco",
    "div.video-card-common",
    "div.rank-wrap",
    "div.bili-feed-card",
  ].join(",");

  function biliCardInfo(card) {
    const a = card.querySelector("a[href*='/video/'], a[href*='bilibili.com/video/']");
    const href = a && a.getAttribute("href");
    if (!href) return null;
    const pageUrl = S.abs(href);
    if (!/\/video\/(BV|av)/i.test(pageUrl)) return null;
    const titleEl = card.querySelector(
      ".bili-video-card__info--tit, h3[title], .bili-video-card__info--tit h3, [title]"
    );
    const title = ((titleEl && (titleEl.getAttribute("title") || titleEl.textContent)) || "").trim();
    const img = card.querySelector("img");
    const cover = img ? img.getAttribute("src") || img.getAttribute("data-src") || "" : "";
    const durEl = card.querySelector(
      ".bili-video-card__stats__duration, .duration, .bili-video-card__stats--text"
    );
    const duration = ((durEl && durEl.textContent) || "").trim();
    return { pageUrl, title: title || "B站视频", cover, duration };
  }

  /* ---------------------------------------------------------------- 站点表 */

  // 注意：videobtn.js 会 querySelectorAll 全部候选，再「去掉被包含的、取面积
  // 最小的」—— 那才是真正的播放器（ytd-watch-flexy 是整页容器、#bilibili-player
  // 是整列容器，都会把按钮带偏）。这里把具体的播放器选择器列全即可。
  const YT_PLAYER = "#movie_player, .html5-video-player, #player, ytd-player, ytd-watch-flexy";
  const BILI_PLAYER =
    ".bpx-player-video-area, #bilibili-player-video-area, .bpx-player-container, #bilibili-player";

  /* 「这张卡片是不是一个视频」的判据，以纯数据下发。

     为什么不能直接把 cardInfo 函数传给 isolated world：函数过不了
     postMessage（结构化克隆只保留数据），videobtn.js 那边拿到的永远是
     undefined，于是「给卡片装按钮」的逻辑每次都被 try/catch 吞掉 ——
     表现就是列表页扫到 20 张卡片、按钮数为 0。 */
  const CARD_SPEC = {
    youtube: {
      link: "a#video-title-link, a#thumbnail, a[href*='/watch'], a[href*='/shorts/']",
      hrefRe: "/watch|/shorts/",
      title: "#video-title, yt-formatted-string#video-title, h3",
    },
    bilibili: {
      link: "a[href*='/video/'], a[href*='bilibili.com/video/']",
      hrefRe: "/video/(BV|av)",
      title: ".bili-video-card__info--tit, h3[title], .bili-video-card__info--tit h3, [title]",
    },
  };

  S.SITES = {
    youtube: {
      host: /(^|\.)youtube\.com$|(^|\.)youtu\.be$/,
      cards: YT_CARDS,
      cardInfo: ytCardInfo,
      formats: ytFormats,
      player: YT_PLAYER,
      playerInfo: () => {
        const t = document.querySelector("h1.title yt-formatted-string, h1.title, #title h1");
        return { title: ((t && t.textContent) || "").trim() || "YouTube 视频" };
      },
    },
    bilibili: {
      host: /(^|\.)bilibili\.com$|(^|\.)bilivideo\.com$/,
      cards: BILI_CARDS,
      cardInfo: biliCardInfo,
      formats: biliFormats,
      player: BILI_PLAYER,
      playerInfo: () => {
        const t = document.querySelector("h1.video-title, h1[class*='title'], .video-title");
        return {
          title: ((t && (t.getAttribute("title") || t.textContent)) || "").trim() || "B站视频",
        };
      },
    },
  };

  S.siteOf = () => {
    const h = location.hostname.toLowerCase();
    for (const [name, s] of Object.entries(S.SITES)) {
      if (s.host.test(h)) return name;
    }
    return null;
  };

  S.currentFormats = () => {
    const site = S.siteOf();
    if (!site) return [];
    try {
      return S.SITES[site].formats() || [];
    } catch (_) {
      return [];
    }
  };

  S.playerElement = () => {
    const site = S.siteOf();
    if (!site) return null;
    const sel = S.SITES[site].player;
    if (!sel) return null;
    try {
      return document.querySelector(sel);
    } catch (_) {
      return null;
    }
  };

  /* 播放页的视频信息（标题 + 清晰度）。每次都重新读页面数据 ——
     __playinfo__ / ytInitialPlayerResponse 是播放器加载完之后才写进页面的。 */
  S.playerFormats = () => {
    const site = S.siteOf();
    if (!site) return null;
    const fmts = S.currentFormats();
    let title = "";
    try {
      const info = S.SITES[site].playerInfo ? S.SITES[site].playerInfo() : null;
      title = (info && info.title) || "";
    } catch (_) {
      title = "";
    }
    return {
      site,
      title: title || "视频",
      pageUrl: location.href,
      cover: "",
      duration: "",
      formats: fmts,
      hasFormats: fmts.length > 0,
    };
  };

  S.formatsForCard = (card) => {
    const site = S.siteOf();
    if (!site) return null;
    const info = S.SITES[site].cardInfo(card);
    if (!info) return null;
    const fmts = S.currentFormats();
    return {
      site,
      title: info.title,
      pageUrl: info.pageUrl,
      cover: info.cover,
      duration: info.duration,
      formats: fmts,
      hasFormats: fmts.length > 0,
    };
  };
  /* ---------------------------------------------------------------- 桥接

     videobtn.js 跑在 isolated world（它需要 chrome.runtime），而本脚本跑在
     MAIN world —— 两者共享 DOM 但不共享 JS 上下文。所以把需要的接口
     挂到 DOM 元素上，videobtn.js 从那里读。

     用 CustomEvent 传数据比写 DOM 属性更好：URL 可能有几千字符，
     属性值放不下（Chrome 对 attribute 值有长度限制）。 */
  const BRIDGE_ID = "zdm-site-bridge";
  let bridge = document.getElementById(BRIDGE_ID);
  if (!bridge) {
    bridge = document.createElement("div");
    bridge.id = BRIDGE_ID;
    bridge.style.display = "none";
    (document.body || document.documentElement).appendChild(bridge);
  }
  /* 按定位串找回元素（定位串由 isolated world 那边生成）：
       "id:<id>"  —— 按 id 找
       "idx:<n>" —— 按在站点卡片列表里的序号找 */
  function resolveEl(spec) {
    if (!spec) return null;
    try {
      if (spec.indexOf("id:") === 0) {
        return document.getElementById(spec.slice(3));
      }
      if (spec.indexOf("idx:") === 0) {
        const site = S.siteOf();
        const sel = site && S.SITES[site] ? S.SITES[site].cards : null;
        if (!sel) return null;
        const list = document.querySelectorAll(sel);
        return list[Number(spec.slice(4))] || null;
      }
    } catch (_) {
      return null;
    }
    return null;
  }

  // 两个 world 之间的通信只能用 postMessage：
  //   - JS 自定义属性（el.__x = v）是每个 world 各自一份，跨 world 读不到；
  //   - attribute 是共享的，但只能放字符串，放不了函数。
  // postMessage 的 event 对象是两个 world 共享的，所以能用它传函数调用。
  window.addEventListener("message", (ev) => {
    const d = ev.data;
    if (!d || d.__zdm !== "query") return;
    let out = null;
    try {
      if (d.what === "siteOf") out = S.siteOf();
      else if (d.what === "config") {
        const site = S.siteOf();
        const cfg = site && S.SITES[site] ? S.SITES[site] : null;
        out = cfg
          ? {
              cards: cfg.cards,
              player: cfg.player,
              site: site,
              // 纯数据的卡片判据，见 CARD_SPEC 的说明
              cardSpec: CARD_SPEC[site] || null,
            }
          : null;
      }
      else if (d.what === "currentFormats") out = S.currentFormats();
      else if (d.what === "playerFormats") out = S.playerFormats();
      else if (d.what === "playerElement") {
        const el = S.playerElement();
        // 元素本身留在 MAIN world，只回传它的标识
        if (el) {
          el.setAttribute("data-zdm-player", "1");
          out = { id: el.id || "", tag: el.tagName };
        }
      } else if ((d.what === "formatsForCard" || d.what === "cardInfo") && d.sel) {
        const el = resolveEl(d.sel);
        if (d.what === "formatsForCard") {
          out = el ? S.formatsForCard(el) : null;
        } else {
          const site = S.siteOf();
          out = el && site && S.SITES[site] && S.SITES[site].cardInfo
            ? S.SITES[site].cardInfo(el)
            : null;
        }
      }
    } catch (e) {
      out = { __error: String(e && e.message) };
    }
    // 回应给发起方。
    // 用 window.postMessage 而不是 ev.source.postMessage ——
    // ev.source 在 MAIN world 里是 isolated world 的 windowproxy，
    // 回传时可能落不到对方的监听器上。直接往 window 发，
    // 两个 world 都能收到（各自按 __zdm/id 过滤）。
    window.postMessage({ __zdm: "reply", id: d.id, data: out }, "*");
  });

  // 标记桥接已就绪（attribute 跨 world 共享）
  bridge.setAttribute("data-zdm-ready", "1");
  // 通知 isolated world：接口就绪
  document.dispatchEvent(new CustomEvent("zdm-sites-ready"));
})();
