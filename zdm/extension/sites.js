/* ZDM Connector - 站点适配器（content script）
 *
 * 职责：把「页面上的一个视频」翻译成「可下载的清晰度列表」。
 *
 * 为什么要站点适配器：
 *   B站 / YouTube 的视频地址都藏在页面脚本里（不是 <video src>），
 *   而且带鉴权。要拿到「有哪些清晰度」必须读页面自己已经加载好的数据：
 *     - YouTube：播放器实例的 response / streamingData
 *     - B站：__playinfo__ / __INITIAL_STATE__ 里的 dash 或 durl
 *   直接发网络请求会被跨域和登录态挡住，在页面里就地解析最稳。
 *
 * 每个适配器返回统一结构：
 *   { site, title, pageUrl, cover, formats: [{ label, url, kind, size, mime, width, height }] }
 */

(() => {
  if (window.__zdmSitesLoaded) return;
  window.__zdmSitesLoaded = true;

  /* ---------------------------------------------------------------- 工具 */

  const abs = (u) => {
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

  /* 从字节数猜出大致体积，YouTube 的 format 里不一定给 contentLength。 */
  const sizeFrom = (itag, width, height, bitrate) => {
    if (num(itag) && width) {
      // 经验值：按码率 × 长度估算不可靠，退回「按分辨率给个粗略上限」
      void itag;
    }
    void bitrate;
    return 0;
  };

  /* ---------------------------------------------------------------- YouTube */

  /* 从 ytInitialPlayerResponse 里挖出 streamingData。 */
  function ytPlayerResponse() {
    const w = window;
    const pr =
      (w.ytInitialPlayerResponse && w.ytInitialPlayerResponse.streamingData) ||
      (w.ytplayer && w.ytplayer.config &&
        w.ytplayer.config.args &&
        w.ytplayer.config.args.player_response &&
        JSON.parse(w.ytplayer.config.args.player_response).streamingData) ||
      null;
    if (pr) return pr;
    // 播放过一次之后，播放器实例上会挂着 response
    const p = document.getElementById("movie_player");
    const inst = p && (p.getPlayerResponse ? p.getPlayerResponse() : null);
    return inst && inst.streamingData ? inst.streamingData : null;
  }

  function ytFormats() {
    const sd = ytPlayerResponse();
    if (!sd) return [];
    const out = [];
    const push = (f, kind) => {
      if (!f || !f.url) return;
      const w = num(f.width);
      const h = num(f.height);
      out.push({
        label: ytLabel(f, kind),
        url: f.url,
        kind: kind,
        size: num(f.contentLength),
        mime: f.mimeType || "",
        width: w,
        height: h,
        itag: num(f.itag),
        audioQuality: f.audioQuality || "",
        bitrate: num(f.bitrate),
      });
    };
    (sd.formats || []).forEach((f) => push(f, "video+muxed"));
    (sd.adaptiveFormats || []).forEach((f) => {
      if (f.mimeType && f.mimeType.startsWith("video/")) push(f, "video");
      else if (f.mimeType && f.mimeType.startsWith("audio/")) push(f, "audio");
    });
    return out;
  }

  function ytLabel(f, kind) {
    const h = num(f.height);
    if (kind === "audio") {
      const q = f.audioQuality || "";
      const br = num(f.audioSampleRate) / 1000;
      const bits = [];
      if (q) bits.push(q === "AUDIO_QUALITY_LOW" ? "低音质" : q === "AUDIO_QUALITY_MEDIUM" ? "中音质" : "高音质");
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
      if (f.qualityLabel) parts.push(f.qualityLabel);
    }
    if (f.qualityLabel) {
      const ql = String(f.qualityLabel);
      if (!parts.includes(ql)) parts.push(ql);
    }
    if (num(f.fps)) parts.push(num(f.fps) + "fps");
    return parts.join(" · ") || "默认";
  }

  /* YouTube 的卡片：首页 / 搜索 / 频道 / 播放页侧栏。 */
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
    const a = card.querySelector("a#video-title-link, a#thumbnail, a[href*='/watch'], a[href*='/shorts/']");
    const href = a && a.getAttribute("href");
    if (!href) return null;
    const pageUrl = abs(href);
    if (!/\/watch|\/shorts\//.test(pageUrl)) return null;
    const titleEl = card.querySelector("#video-title, yt-formatted-string#video-title, h3");
    const title = (titleEl && (titleEl.getAttribute("title") || titleEl.textContent) || "").trim();
    const img = card.querySelector("img");
    const cover = img ? img.getAttribute("src") || "" : "";
    const durEl = card.querySelector("#text.ytd-thumbnail-overlay-time-status-renderer, .ytd-thumbnail-overlay-time-status-renderer, #overlays #text");
    const duration = (durEl && durEl.textContent || "").trim();
    return { pageUrl, title: title || "YouTube 视频", cover, duration };
  }

  /* YouTube 的清晰度来自「当前播放的视频」，而卡片上可能有十几个不同的视频。
     只有点了某张卡片之后才知道它有哪些清晰度 —— 所以策略是：
       1. 点开/悬停时先给出「读取中」
       2. 真正要下载时，让 background 用页面内的 ytInitialPlayerResponse 解析
     但那样每个卡片都要发一次请求，太慢。
     折中：优先用页面已经加载过的 player response（通常是列表页没有、
     播放页有），拿不到就退回「让用户在播放页使用」，并明确提示。 */

  /* ---------------------------------------------------------------- B站 */

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
      // 只有真的带 dash/durl 结构的才算
      if (c.dash || c.durl || (c.video && c.audio)) return c;
    }
    return null;
  }

  const BILI_QUALITY = {
    6: "240P 极速",
    16: "360P 清晰",
    32: "480P 流畅",
    64: "720P 高清",
    74: "720P 高清",
    80: "1080P 高清",
    112: "1080P 高码率",
    116: "1080P 60帧",
    120: "4K 超清",
    125: "HDR 真彩",
    126: "杜比视界",
    127: "8K 超高清",
  };

  function biliFormats() {
    const dash = biliDashInfo();
    if (!dash) return [];
    const out = [];

    // DASH：video 数组是视频轨，audio 数组是音频轨
    if (Array.isArray(dash.video)) {
      for (const v of dash.video) {
        const url = v && v.baseUrl;
        if (!url) continue;
        const id = num(v.id);
        out.push({
          label: biliLabel(id, "视频+音频"),
          url,
          kind: "video",
          size: 0,
          mime: v.mimeType || "video/mp4",
          width: num(v.width),
          height: num(v.height),
          quality: id,
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

    // 老式 durl：整个视频合在一个文件里
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
    const pageUrl = abs(href);
    if (!/\/video\/(BV|av)/i.test(pageUrl)) return null;
    const titleEl = card.querySelector(
      ".bili-video-card__info--tit, h3[title], .bili-video-card__info--tit h3, [title]"
    );
    const title = (titleEl && (titleEl.getAttribute("title") || titleEl.textContent) || "").trim();
    const img = card.querySelector("img");
    const cover = img ? img.getAttribute("src") || img.getAttribute("data-src") || "" : "";
    const durEl = card.querySelector(".bili-video-card__stats__duration, .duration, .bili-video-card__stats--text");
    const duration = (durEl && durEl.textContent || "").trim();
    return { pageUrl, title: title || "B站视频", cover, duration };
  }

  /* ---------------------------------------------------------------- 站点表 */

  const SITES = {
    youtube: {
      host: /(^|\.)youtube\.com$|(^|\.)youtu\.be$/,
      cards: YT_CARDS,
      cardInfo: ytCardInfo,
      formats: ytFormats,
      // YouTube 的流媒体地址是逐视频的，列表页拿不到；
      // 播放页能拿到完整的 streamingData。
      canListFormats: () => !!ytPlayerResponse(),
    },
    bilibili: {
      host: /(^|\.)bilibili\.com$|(^|\.)bilivideo\.com$/,
      cards: BILI_CARDS,
      cardInfo: biliCardInfo,
      formats: biliFormats,
      canListFormats: () => !!(biliPlayInfo() || biliDashInfo()),
    },
  };

  function siteOf() {
    const h = location.hostname.toLowerCase();
    for (const [name, s] of Object.entries(SITES)) {
      if (s.host.test(h)) return name;
    }
    return null;
  }

  /* 页面里可能已经有多个播放源（YouTube 播放页只有一个）。
     取第一个可用的即可。 */
  function currentFormats() {
    const site = siteOf();
    if (!site) return [];
    try {
      return SITES[site].formats() || [];
    } catch (_) {
      return [];
    }
  }

  /* 从一个视频卡片上找「这个视频的可下载格式」。
     站点提供「当前播放」数据时直接用；否则返回一个「需要点开播放页」的提示。 */
  function formatsForCard(card) {
    const site = siteOf();
    if (!site) return null;
    const info = SITES[site].cardInfo(card);
    if (!info) return null;
    const fmts = currentFormats();
    return {
      site,
      title: info.title,
      pageUrl: info.pageUrl,
      cover: info.cover,
      duration: info.duration,
      formats: fmts,
      hasFormats: fmts.length > 0,
    };
  }

  window.__zdmSites = {
    siteOf,
    currentFormats,
    formatsForCard,
    SITES,
    abs,
  };
})();
