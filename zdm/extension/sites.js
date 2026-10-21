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

  /* 页面里的 playerResponse 只有一半可用：adaptiveFormats（1080P/720P 等
     高清轨）几乎全是 signatureCipher 加密形式，没有直接的 f.url —— 解密
     要扒 base.js 做 signature 运算，脆且慢。
     这里改走 yt-dlp 同款路线：用页面自己的 innertube 凭据，以 ANDROID
     客户端身份调 /youtubei/v1/player —— 这个客户端返回的是**未加密直链**
     （url 字段直接可用），也不用做 n 参数变换。MAIN world 里 fetch 是
     同源请求，没有 CORS 问题。 */
  let ytApiCache = null; // { videoId, at, formats }

  /* YouTube 播放器自己知道「这个视频有哪些档位」—— getAvailableQualityLevels()
     返回的就是真实阶梯。它不依赖任何接口/登录态/PO token，永远可用。
     所以 yt-dlp 被拦时，用这个给用户一份真实的清晰度列表。 */
  const YT_LEVEL_HEIGHT = {
    highres: 4320,
    hd2880: 2880,
    hd2160: 2160,
    hd1440: 1440,
    hd1080: 1080,
    hd720: 720,
    large: 480,
    medium: 360,
    small: 240,
    tiny: 144,
  };

  function ytPlayerEl() {
    try {
      return (
        document.getElementById("movie_player") ||
        document.querySelector("#movie_player, .html5-video-player")
      );
    } catch (_) {
      return null;
    }
  }

  function ytQualityLevels() {
    const p = ytPlayerEl();
    if (!p || typeof p.getAvailableQualityLevels !== "function") return [];
    let levels = [];
    try {
      levels = p.getAvailableQualityLevels() || [];
    } catch (_) {
      return [];
    }
    const seen = new Set();
    const out = [];
    for (const q of levels) {
      const h = YT_LEVEL_HEIGHT[String(q)] || 0;
      if (!h || seen.has(h)) continue;
      seen.add(h);
      out.push({ q: String(q), height: h });
    }
    out.sort((a, b) => b.height - a.height);
    return out;
  }

  /* 让播放器切到某一档：切了之后浏览器会真的去拉那一路视频的分段，
     我们靠 webRequest 把它抓下来（就是「嗅探」那条链路）。 */
  function ytSetQuality(q) {
    const p = ytPlayerEl();
    if (!p) return false;
    try {
      if (typeof p.setPlaybackQualityRange === "function") p.setPlaybackQualityRange(q, q);
      if (typeof p.setPlaybackQuality === "function") p.setPlaybackQuality(q);
      return true;
    } catch (_) {
      return false;
    }
  }

  /* 让播放器「重新拉一次流」。
     setPlaybackQualityRange 只是把上限调高，如果当前档位的 buffer 还够，
     YouTube 根本不会发新请求 —— 我们等 9 秒也抓不到那一档的直链，
     菜单就一直停在「切换清晰度中…」。同位置 seek 一小步会强制它重新
     解析 manifest 并按新档位取流。 */
  function ytNudge() {
    const p = ytPlayerEl();
    if (!p) return false;
    try {
      const t = typeof p.getCurrentTime === "function" ? p.getCurrentTime() : 0;
      if (typeof p.seekTo === "function") p.seekTo(Math.max(0, t + 0.4), true);
      if (typeof p.playVideo === "function") p.playVideo();
      return true;
    } catch (_) {
      return false;
    }
  }

  /* 统一拿到 streamingData。

     **坑（曾经让「大小未知」修了好几轮都没修好）**：ytPlayerResponse()
     返回的已经是 `streamingData` 本身，不是整个 playerResponse。之前这里写成
     `pr = ytPlayerResponse(); const sd = pr && pr.streamingData`，等于取
     `streamingData.streamingData` → undefined，于是阶梯和 itag 表**永远是空的**，
     面板就一直是「大小未知」，而且一点报错都没有。
     这里做一层归一化，两种形态（整包 / 只给 streamingData）都吃得下。

     **顺序也很重要**：先问播放器实例（getPlayerResponse），再退回页面变量。
     YouTube 是软导航 —— 点右侧推荐换视频不会重载页面，window.ytInitialPlayerResponse
     还停在上一个视频上；用它就会把上一部的字节数报给这一部（比「未知」更糟）。 */
  function ytStreaming() {
    const unwrap = (o) => {
      if (!o) return null;
      // 整包 → 取 streamingData；已经是 streamingData 就直接用
      if (!o.formats && !o.adaptiveFormats && o.streamingData) return o.streamingData;
      if (o.formats || o.adaptiveFormats || o.serverAbrStreamingUrl) return o;
      return null;
    };
    // 1) 播放器实例：永远跟着当前视频走
    try {
      const p = document.getElementById("movie_player");
      if (p && typeof p.getPlayerResponse === "function") {
        const sd = unwrap(p.getPlayerResponse());
        if (sd) return sd;
      }
    } catch (_) {
      /* 播放器还没准备好 */
    }
    // 2) ytPlayerResponse()（内部依次探 ytInitialPlayerResponse / ytplayer.config /
    //    movie_player，返回的已经是 streamingData）
    try {
      const sd = unwrap(ytPlayerResponse());
      if (sd) return sd;
    } catch (_) {
      /* ignore */
    }
    // 3) 兜底：页面变量（首屏时 getPlayerResponse 可能还没有）
    try {
      return unwrap(window.ytInitialPlayerResponse);
    } catch (_) {
      return null;
    }
  }

  /* itag → 字节数。

     关键点：adaptiveFormats 里的 contentLength **即使 URL 被 signatureCipher
     加密也照样存在**（加密的只是直链，不是元数据）。所以「嗅探到大小」根本
     不需要发 Range 请求去问服务端 —— 那张表页面里就有，直接读。 */
  function ytContentLens() {
    const sd = ytStreaming();
    const out = {};
    const add = (list) => {
      for (const f of list || []) {
        const it = num(f && f.itag);
        const len = num(f && f.contentLength);
        if (it && len) out[it] = len;
      }
    };
    if (sd) {
      add(sd.formats);
      add(sd.adaptiveFormats);
    }
    return out;
  }

  /* 「清晰度 → 大小」阶梯（给嗅探面板用）。

     为什么需要它：YouTube 现在大量走 SABR，播放器对 /videoplayback 发的
     是一条 POST 流式请求 —— 响应是 chunked，既没有 Content-Length 也没有
     Content-Range，URL 里也没有 itag/clen。所以「按这条直链报大小」这条路
     对这个请求本身是**不可能**走通的（那条流根本没有"文件大小"这个概念）。
     能准确说出「1080P 有多大」的地方只有一个：playerResponse 的
     adaptiveFormats[].contentLength。这里把它整理成阶梯交给面板。 */
  function ytLadder() {
    const sd = ytStreaming();
    if (!sd) return { rows: [], title: "" };
    // 标题优先用 document.title：软导航后它立刻是新的，
    // 而 ytInitialPlayerResponse.videoDetails 还停在上一个视频。
    let title = (document.title || "").replace(/\s*[-–]\s*YouTube\s*$/i, "").trim();
    if (!title) {
      try {
        const pr = window.ytInitialPlayerResponse;
        title = (pr && pr.videoDetails && pr.videoDetails.title) || "";
      } catch (_) {
        title = "";
      }
    }
    const best = new Map(); // height -> row
    const add = (f) => {
      const m = (f && f.mimeType) || "";
      if (!m.startsWith("video/")) return;
      const h = num(f.height);
      const len = num(f.contentLength);
      if (!h || !len) return;
      const codec = /avc1/.test(m)
        ? "H.264"
        : /vp0?9/.test(m)
        ? "VP9"
        : /av01/.test(m)
        ? "AV1"
        : "";
      const cur = best.get(h);
      // 同一高度优先 H.264（兼容性最好），其次保留最先出现的
      if (!cur || (codec === "H.264" && cur.codec !== "H.264")) {
        best.set(h, {
          height: h,
          size: len,
          codec: codec,
          label: h + "P" + (codec ? " · " + codec : ""),
        });
      }
    };
    (sd.formats || []).forEach(add);
    (sd.adaptiveFormats || []).forEach(add);
    const rows = [...best.values()].sort((a, b) => b.height - a.height);
    return { rows, title };
  }

  function ytVideoId() {
    try {
      const p = document.getElementById("movie_player");
      if (p && typeof p.getVideoData === "function") {
        const d = p.getVideoData();
        if (d && d.video_id) return d.video_id;
      }
    } catch (_) {
      /* 播放器没起来 */
    }
    try {
      return new URLSearchParams(location.search).get("v") || "";
    } catch (_) {
      return "";
    }
  }

  async function ytInnertubeFormats() {
    const videoId = ytVideoId();
    if (!videoId) return [];
    // 同一个视频 60s 内不重复打接口（菜单反复开关不能每次都请求）
    if (ytApiCache && ytApiCache.videoId === videoId && Date.now() - ytApiCache.at < 60000) {
      return ytApiCache.formats;
    }
    let key = "";
    try {
      key = (window.ytcfg && (ytcfg.get("INNERTUBE_API_KEY") || (ytcfg.data_ && ytcfg.data_.INNERTUBE_API_KEY))) || "";
    } catch (_) {
      key = "";
    }
    const body = {
      videoId,
      context: {
        client: {
          clientName: "ANDROID",
          clientVersion: "19.09.37",
          androidSdkVersion: 30,
          // 不带这个 UA，YouTube 会拒绝 ANDROID 客户端或退回成加密直链
          userAgent:
            "com.google.android.youtube/19.09.37 (Linux; U; Android 11) gzip",
          hl: "en",
          gl: "US",
        },
      },
    };
    try {
      const r = await fetch(
        "/youtubei/v1/player" + (key ? "?key=" + encodeURIComponent(key) + "&prettyPrint=false" : "?prettyPrint=false"),
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            // 这两个头是 ANDROID innertube 返回明文直链的关键
            "User-Agent":
              "com.google.android.youtube/19.09.37 (Linux; U; Android 11) gzip",
            "X-Goog-Api-Format-Version": "2",
            Accept: "*/*",
            "Accept-Language": "en-US,en;q=0.9",
          },
          body: JSON.stringify(body),
        }
      );
      if (!r.ok) throw new Error("HTTP " + r.status);
      const j = await r.json();
      const sd = j && j.streamingData;
      const playability = j && j.playabilityStatus && j.playabilityStatus.status;
      if (!sd || (playability && playability !== "OK")) {
        ytApiCache = { videoId, at: Date.now(), formats: [] };
        return [];
      }
      const urlOf = (f) => {
        if (f && f.url) return f.url;
        // ANDROID 客户端理论上都是明文 url；万一给了 cipher 也解一下
        const c = f && (f.signatureCipher || f.cipher);
        if (!c) return "";
        try {
          const q = new URLSearchParams(c);
          return q.get("url") || "";
        } catch (_) {
          return "";
        }
      };
      const out = [];
      const push = (f, kind) => {
        const url = urlOf(f);
        if (!url) return;
        let size = num(f.contentLength);
        if (!size) {
          try {
            size = num(new URLSearchParams(url.split("?")[1] || "").get("clen"));
          } catch (_) {
            size = 0;
          }
        }
        out.push({
          label: ytLabel(f, kind),
          url,
          kind,
          size,
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
      ytApiCache = { videoId, at: Date.now(), formats: out };
      return out;
    } catch (_) {
      ytApiCache = { videoId, at: Date.now(), formats: [] };
      return [];
    }
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

  async function ytFormats() {
    // 主路：innertube（未加密直链 + contentLength）
    let formats = [];
    try {
      formats = await ytInnertubeFormats();
    } catch (_) {
      formats = [];
    }
    // 兜底：页面 playerResponse 里本来就带明文 url 的条目（通常是 360P muxed）
    if (!formats.length) {
      const sd = ytPlayerResponse();
      if (sd) {
        const push = (f, kind) => {
          if (!f || !f.url) return;
          formats.push({
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
      }
    }
    // 按 itag 去重（先到者优先），视频从高分辨率到低排序、音频按码率
    const seen = new Set();
    const out = [];
    for (const f of formats) {
      const k = f.itag || f.url;
      if (seen.has(k)) continue;
      seen.add(k);
      out.push(f);
    }
    out.sort((a, b) => {
      if (a.kind === "audio" && b.kind === "audio") return (b.bitrate || 0) - (a.bitrate || 0);
      return (b.height || 0) - (a.height || 0);
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

  /* ---- 认出「这个是哪个视频」 ----------------------------------------

     B站新版播放页是软导航（点右侧推荐视频不重载页面），而 window.__playinfo__
     是**首次加载**时写进去的，切视频后不会更新。所以只要沿用页面里的
     __playinfo__，就会出现「标题是新的、内容是上一个视频」——
     必须按当前 bvid/cid 重新取一次数据。 */

  const BILI_API = "https://api.bilibili.com";
  const BILI_FNVAL = 4048; // dash + 4K + HDR + 杜比 + AV1

  function biliPageNo(url) {
    try {
      const p = num(new URL(url, location.href).searchParams.get("p"));
      return p > 0 ? p : 1;
    } catch (_) {
      return 1;
    }
  }

  function biliKey(pageUrl) {
    const u = String(pageUrl || location.href);
    const bv = /\/video\/(BV[0-9A-Za-z]+)/.exec(u);
    if (bv) return { bvid: bv[1], p: biliPageNo(u) };
    const av = /\/video\/av(\d+)/i.exec(u);
    if (av) return { avid: av[1], p: biliPageNo(u) };
    return null;
  }

  function biliGetJson(url) {
    return fetch(url, {
      credentials: "include",
      headers: { Accept: "application/json" },
    }).then((r) => {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  // cid 缓存：key = bvid/avid#p
  const biliCidCache = {};
  async function biliCidFor(key) {
    const ck = (key.bvid || "av" + key.avid) + "#" + key.p;
    if (biliCidCache[ck]) return biliCidCache[ck];
    // 页面状态只有在「当前页面就是这个视频」时才可信
    try {
      const st = window.__INITIAL_STATE__;
      const sameBv =
        st && key.bvid && (st.bvid === key.bvid || (st.videoData && st.videoData.bvid === key.bvid));
      if (sameBv) {
        const sameP = !st.p || num(st.p) === key.p;
        const cid = (st.videoData && st.videoData.cid) || st.cid;
        if (cid && sameP) {
          biliCidCache[ck] = cid;
          return cid;
        }
      }
    } catch (_) {
      /* 没有页面状态就查接口 */
    }
    const q = key.bvid ? "bvid=" + encodeURIComponent(key.bvid) : "aid=" + key.avid;
    const j = await biliGetJson(BILI_API + "/x/player/pagelist?" + q);
    const pages = j && j.data;
    if (Array.isArray(pages) && pages.length) {
      const pg = pages[Math.min(key.p, pages.length) - 1];
      if (pg && pg.cid) {
        biliCidCache[ck] = pg.cid;
        return pg.cid;
      }
    }
    return 0;
  }

  // playurl 结果缓存：cid -> {at, data}
  const biliPlayCache = {};
  async function biliPlayData(key, cid) {
    const hit = biliPlayCache[cid];
    if (hit && Date.now() - hit.at < 300000) return hit.data;
    const q = key.bvid ? "bvid=" + encodeURIComponent(key.bvid) : "aid=" + key.avid;
    const url =
      BILI_API + "/x/player/playurl?fnval=" + BILI_FNVAL + "&fnver=0&fourk=1&qn=127&" + q + "&cid=" + cid;
    const j = await biliGetJson(url);
    if (j && j.code === 0 && j.data && (j.data.dash || j.data.durl)) {
      biliPlayCache[cid] = { at: Date.now(), data: j.data };
      return j.data;
    }
    throw new Error("playurl code=" + ((j && j.code) || "?") + " " + ((j && j.message) || ""));
  }

  /* 直链 URL 一定带着 cid（/upgcxcode/xx/xx/<cid>/<cid>-1-...m4s），
     用它判断「这份数据是不是当前视频的」。 */
  function biliDataIsForCid(data, cid) {
    if (!data || !cid) return false;
    const lists = [];
    if (data.dash) lists.push(data.dash.video || [], data.dash.audio || []);
    if (data.video) lists.push(data.video, data.audio || []);
    if (data.durl) lists.push(data.durl);
    for (const list of lists) {
      for (const it of list) {
        const u = it && (it.baseUrl || it.base_url || it.url);
        if (u && (u.indexOf("/" + cid + "/") >= 0 || u.indexOf("/" + cid + "-") >= 0)) return true;
      }
    }
    return false;
  }

  function biliPickDash(data) {
    if (!data) return null;
    if (data.dash) return data.dash;
    if (data.video || data.durl) return data;
    return null;
  }

  async function biliResolve(pageUrl) {
    const key = biliKey(pageUrl);
    if (!key) return null;
    let cid = 0;
    try {
      cid = await biliCidFor(key);
    } catch (_) {
      cid = 0;
    }

    // 1) 页面自带的 __playinfo__：最快，但必须按 cid 校验（软导航后是旧的）
    const inline = biliPickDash(biliDashInfo());
    if (inline && (!cid || biliDataIsForCid(inline, cid))) return inline;

    // 2) 直接问接口（最准）
    if (cid) {
      try {
        const fresh = biliPickDash(await biliPlayData(key, cid));
        if (fresh) return fresh;
      } catch (_) {
        /* 接口失败再退回页面数据 */
      }
    }

    // 3) 都拿不到才用页面数据（可能不准，但好过空列表）
    return inline || null;
  }

  async function biliFormats(pageUrl) {
    const dash = await biliResolve(pageUrl);
    if (!dash) return [];
    const out = [];

    if (Array.isArray(dash.video)) {
      /* 一条清晰度 B站会同时给 avc / hevc / av1 三条轨。全列出来就是菜单里
         连续三条一模一样的「1080P」—— 用户看到的困惑正是这个。按高度去重，
         优先 avc（h264）：兼容性最好，macOS/Windows 自带播放器和剪辑软件都认。 */
      const score = (codecs) => {
        const c = String(codecs || "").toLowerCase();
        if (/avc|h264/.test(c)) return 3;
        if (/hev|hvc|h265/.test(c)) return 2;
        return 1; // av01 / vp9
      };
      const byHeight = new Map();
      for (const v of dash.video) {
        const url = v && v.baseUrl;
        if (!url) continue;
        const h = num(v.height);
        const prev = byHeight.get(h);
        if (!prev || score(v.codecs) > score(prev.codecs)) byHeight.set(h, v);
      }

      /* 音频轨：同一次 playurl 响应里一定有，直接挑码率最高的一条配给
         每条视频轨。以前是靠 videobtn 从**可见列表**里 pickAudio —— 一旦
         那条音频条目没进列表（或列表被过滤），后端就拿不到 audio_url，
         于是「视频下完了但没声音」，而且后端还静默当成功。
         这样配对不再依赖列表内容。 */
      let bestAudio = null;
      for (const a of dash.audio || []) {
        const au = a && a.baseUrl;
        if (!au) continue;
        if (!bestAudio || num(a.bandwidth) > num(bestAudio.bandwidth)) bestAudio = a;
      }
      const audioUrl = bestAudio ? bestAudio.baseUrl : "";

      const tracks = [...byHeight.values()].sort(
        (a, b) => num(b.height) - num(a.height)
      );
      for (const v of tracks) {
        out.push({
          label: biliLabel(num(v.id), "视频+音频"),
          url: v.baseUrl,
          kind: "video",
          size: 0,
          mime: v.mimeType || "video/mp4",
          width: num(v.width),
          height: num(v.height),
          quality: num(v.id),
          codec: (v.codecs || "").split(".")[0] || "",
          // 后端下完视频后再下这条音频并用 ffmpeg 合流
          audio_url: audioUrl || undefined,
        });
      }
      if (bestAudio) {
        out.push({
          label: biliAudioLabel(bestAudio),
          url: bestAudio.baseUrl,
          kind: "audio",
          size: 0,
          mime: bestAudio.mimeType || "audio/mp4",
          width: 0,
          height: 0,
          quality: num(bestAudio.id),
          codec: (bestAudio.codecs || "").split(".")[0] || "",
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
      // 卡片必须按**它自己的** URL 取清晰度：页面里的 __playinfo__ 是当前
      // 播放视频的，拿它给卡片用就会「标题是新的、内容是上一个视频」。
      formatsFor: biliFormats,
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

  S.currentFormats = async () => {
    const site = S.siteOf();
    if (!site) return [];
    try {
      // formats() 可能是异步的（YouTube 要调 innertube 接口）
      return (await S.SITES[site].formats()) || [];
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
  S.playerFormats = async () => {
    const site = S.siteOf();
    if (!site) return null;
    const fmts = await S.currentFormats();
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

  S.formatsForCard = async (card) => {
    const site = S.siteOf();
    if (!site) return null;
    const info = S.SITES[site].cardInfo(card);
    if (!info) return null;
    // 有 formatsFor 就按卡片自己的 URL 取；没有才退回「当前页面」的格式。
    const cfg = S.SITES[site];
    const fmts = cfg.formatsFor ? await cfg.formatsFor(info.pageUrl) : await S.currentFormats();
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
  window.addEventListener("message", async (ev) => {
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
      else if (d.what === "currentFormats") out = await S.currentFormats();
      else if (d.what === "playerFormats") out = await S.playerFormats();
      else if (d.what === "qualityLevels") out = ytQualityLevels();
      else if (d.what === "setQuality") out = { ok: ytSetQuality(d.sel) };
      else if (d.what === "nudge") out = { ok: ytNudge() };
      else if (d.what === "contentLengths") out = ytContentLens();
      else if (d.what === "videoLadder") out = ytLadder();
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
          out = el ? await S.formatsForCard(el) : null;
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
