/* ZDM Connector - 通用播放器下载按钮（content script）
 *
 * 用户要的交互：
 *   任意网站的视频播放器上，右上角浮出「下载此视频」
 *   点它 → 一个小面板，给出几条下载路径
 *
 * 与 videobtn.js 的分工：
 *   - videobtn.js 只注入 B站 / YouTube，靠站点规则拿「清晰度列表」，
 *     是「卡片级 + 播放器级」的重交互；
 *   - 本文件注入其余所有页面，**不写任何站点规则**，只回答三个问题：
 *     页面里哪个是主视频、它有没有直链、页面地址能不能交给 yt-dlp。
 *     解析（音视频完整）这条路对所有站点都一样，所以新增平台零代码。
 *
 * 三条下载路径，按「有没有声音」排序：
 *   1. 解析此页面 —— 把页面地址交给后端 yt-dlp，拿回来就是合好的
 *      音视频格式列表（DASH 站点也有声音）。**首选。**
 *   2. 下载视频+音频 —— 嗅探列表里同时抓到视频轨和音频轨时，
 *      把两条一起推给后端，ffmpeg 合流。yt-dlp 不认识的站点靠它兜底。
 *   3. 直接下载当前视频 —— 只用 <video> 的 currentSrc。
 *      可能是纯视频轨（没声音），UI 上必须说清楚。
 *
 * 样式用 Shadow DOM 隔离：站点 CSS（尤其 * {} 通配符）会把普通 DOM
 * 插入的按钮打回原形。
 */

(() => {
  if (window.__zdmPlayerBtnLoaded) return;
  window.__zdmPlayerBtnLoaded = true;

  const __err = (msg) => {
    try {
      console.warn("[ZDM] " + msg);
    } catch (_) {
      /* 忽略 */
    }
  };

  const esc = (s) =>
    String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );

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

  let host = null;
  let shadow = null;
  let btn = null;
  let panel = null;
  let media = null; // 当前主媒体元素
  let lastAudioUrl = "";
  let audioCheckedAt = 0;
  let busy = false;

  /* ---------------------------------------------------------------- *
   * 找到「主」视频
   * ---------------------------------------------------------------- */

  function visibleArea(el) {
    const r = el.getBoundingClientRect();
    if (r.width < 60 || r.height < 40) return 0;
    const st = getComputedStyle(el);
    if (st.display === "none" || st.visibility === "hidden") return 0;
    if (Number(st.opacity) === 0) return 0;
    return r.width * r.height;
  }

  /* 打分：正在播放 > 有时长 > 占屏幕大。全屏的那个一票通过。
     一个页面里常有预告片、缩略图预览、广告视频，只有真在播的
     （或者占屏最大的静止海报）才是用户想下的那个。 */
  function scoreVideo(v) {
    let s = visibleArea(v);
    if (!s) return 0;
    try {
      if (!v.paused && !v.ended) s *= 3;
      if (v.currentTime > 0) s *= 1.2;
      if (v.duration && isFinite(v.duration) && v.duration > 3) s *= 1.5;
    } catch (_) {
      /* readyState 没就绪时读属性可能抛，忽略 */
    }
    if (document.fullscreenElement === v) s *= 10;
    return s;
  }

  function pickMedia() {
    let best = null;
    let bestScore = 0;
    for (const v of document.querySelectorAll("video")) {
      const s = scoreVideo(v);
      if (s > bestScore) {
        bestScore = s;
        best = v;
      }
    }
    if (best) return best;
    /* 没有视频：音乐站。取第一个有源、占面积的 <audio> */
    for (const a of document.querySelectorAll("audio")) {
      const src = a.currentSrc || a.src || "";
      if (src) return a;
    }
    return null;
  }

  /* 当前媒体的直链。blob: 说明播放器走 MSE（把分片拼在内存里），
     这种地址浏览器外的人取不到，只能走解析。 */
  function directUrl(el) {
    if (!el) return "";
    const raw = el.currentSrc || el.src || "";
    if (raw && !/^(blob|data):/i.test(raw)) {
      try {
        return new URL(raw, location.href).href;
      } catch (_) {
        return raw;
      }
    }
    const src = el.querySelector && el.querySelector("source[src]");
    if (src) {
      try {
        return new URL(src.getAttribute("src"), location.href).href;
      } catch (_) {
        return src.getAttribute("src") || "";
      }
    }
    return "";
  }

  /* ---------------------------------------------------------------- *
   * UI
   * ---------------------------------------------------------------- */

  function build() {
    host = document.createElement("div");
    host.id = "zdm-playerbtn-host";
    host.style.cssText =
      "all:initial;position:fixed;left:0;top:0;z-index:2147483646;display:none;";
    shadow = host.attachShadow({ mode: "open" });
    shadow.innerHTML = `
      <style>
        :host{all:initial}
        .wrap{all:initial;position:absolute;right:10px;top:10px;display:flex;
          flex-direction:column;align-items:flex-end;gap:8px;font-family:inherit}
        .btn{all:initial;display:flex;align-items:center;gap:6px;height:32px;
          padding:0 12px;border-radius:999px;cursor:pointer;border:none;
          background:rgba(37,99,235,.92);color:#fff;
          font:600 12px/1 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans CJK SC",sans-serif;
          box-shadow:0 3px 12px rgba(15,23,42,.28);transition:.15s;font-family:inherit}
        .btn:hover{background:#1d4fd8}
        .btn svg{width:14px;height:14px;flex:none}
        .btn[disabled]{background:rgba(100,116,139,.85);cursor:default}
        .pnl{all:initial;display:block;width:268px;border-radius:12px;background:#fff;
          border:1px solid #e3e8ef;box-shadow:0 18px 50px rgba(15,23,42,.22);
          font:13px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans CJK SC",sans-serif;
          color:#1f2937;font-family:inherit;overflow:hidden}
        .pnl[hidden]{display:none}
        .hd{display:flex;align-items:center;gap:7px;padding:10px 12px;
          border-bottom:1px solid #eef1f5;font-weight:700;font-size:12.5px}
        .hd .dot{width:7px;height:7px;border-radius:50%;background:#2563eb;flex:none}
        .hd .x{margin-left:auto;all:initial;cursor:pointer;color:#94a3b8;font-size:15px;
          line-height:1;padding:2px 4px;border-radius:6px;font-family:inherit}
        .hd .x:hover{background:#f1f5f9;color:#475569}
        .bd{padding:10px 12px;display:flex;flex-direction:column;gap:8px}
        .row{all:initial;display:flex;align-items:center;gap:8px;font-family:inherit}
        .row .lb{all:initial;font-size:12px;color:#475569;flex:none;font-family:inherit}
        select{all:initial;flex:1;min-width:0;border:1px solid #d7dee8;border-radius:7px;
          background:#fff;color:#1f2937;font:12px/1 -apple-system,sans-serif;
          padding:6px 7px;cursor:pointer;font-family:inherit}
        .act{all:initial;display:flex;align-items:center;justify-content:space-between;
          gap:8px;cursor:pointer;border:1px solid #dbe3ee;border-radius:9px;
          background:#fbfcfe;padding:9px 10px;font-family:inherit;text-align:left}
        .act:hover{background:#f1f5fb;border-color:#c7d5ea}
        .act .t{all:initial;font-weight:600;font-size:12.5px;color:#1f2937;font-family:inherit}
        .act .d{all:initial;display:block;font-size:11px;color:#6b7280;margin-top:2px;
          line-height:1.45;font-family:inherit}
        .act .k{all:initial;flex:none;font-size:11px;color:#2563eb;font-weight:600;
          font-family:inherit}
        .act[disabled]{opacity:.55;cursor:default}
        .act.pri{background:#eef4ff;border-color:#bcd2f7}
        .act.pri:hover{background:#e3edff}
        .tip{all:initial;font-size:11px;color:#94a3b8;line-height:1.5;font-family:inherit}
        .tip.bad{color:#b45309}
        @media (prefers-color-scheme: dark){
          .pnl{background:#1e293b;border-color:#334155;color:#e2e8f0}
          .hd{border-bottom-color:#334155}
          .hd .x:hover{background:#334155;color:#e2e8f0}
          .act{background:#172033;border-color:#334155}
          .act:hover{background:#1f2b40;border-color:#3f5573}
          .act .t{color:#e2e8f0}
          .act .d{color:#94a3b8}
          .act.pri{background:#1e3a8a4d;border-color:#3b5b8c}
          select{background:#0f172a;border-color:#334155;color:#e2e8f0}
          .row .lb{color:#94a3b8}
          .tip{color:#64748b}
        }
      </style>
      <div class="wrap">
        <div class="pnl" hidden>
          <div class="hd"><span class="dot"></span><span class="tt">下载此视频</span>
            <button class="x" type="button" title="关闭">✕</button></div>
          <div class="bd">
            <div class="row">
              <span class="lb">清晰度</span>
              <select class="h">
                <option value="1080">1080P</option>
                <option value="720">720P</option>
                <option value="480">480P</option>
                <option value="1440">1440P</option>
                <option value="2160">2160P（4K）</option>
              </select>
            </div>
            <button class="act pri" type="button" data-act="hls">
              <span><span class="t">下载此视频（流媒体）</span>
                <span class="d">用嗅探到的播放列表整条拉，后端自动合并分片</span></span>
              <span class="k">推荐</span>
            </button>
            <button class="act" type="button" data-act="resolve">
              <span><span class="t">解析此页面</span>
                <span class="d">音视频完整，由 yt-dlp 取合流格式（限已支持站点）</span></span>
              <span class="k"></span>
            </button>
            <button class="act" type="button" data-act="merge">
              <span><span class="t">下载视频 + 音频</span>
                <span class="d">嗅探已配对到音频轨，后端 ffmpeg 合流</span></span>
              <span class="k"></span>
            </button>
            <button class="act" type="button" data-act="direct">
              <span><span class="t">直接下载当前视频</span>
                <span class="d">只用播放器正在播的那条地址</span></span>
              <span class="k"></span>
            </button>
            <button class="act" type="button" data-act="sniffed">
              <span><span class="t">下载嗅探到的视频轨</span>
                <span class="d">取本页最新抓到的那条视频直链（带声音取决于站点）</span></span>
              <span class="k"></span>
            </button>
            <button class="act" type="button" data-act="panel">
              <span><span class="t">本页所有媒体 / 图片</span>
                <span class="d">打开嗅探面板，可勾选批量下载</span></span>
              <span class="k"></span>
            </button>
            <div class="tip"></div>
          </div>
        </div>
        <button class="btn" type="button" title="ZDM 下载此视频">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
               stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M4 19h16"/>
          </svg>
          <span class="bt">下载此视频</span>
        </button>
      </div>`;
    (document.documentElement || document.body).appendChild(host);
    btn = shadow.querySelector(".btn");
    panel = shadow.querySelector(".pnl");
    shadow.querySelector(".hd .x").addEventListener("click", closePanel);
    btn.addEventListener("click", togglePanel);
    shadow.querySelectorAll(".act").forEach((b) => {
      b.addEventListener("click", () => act(b.dataset.act).catch((e) => __err(String(e))));
    });
    // 点页面别处收起面板（Shadow DOM 里点外面要用 composedPath 判断）
    document.addEventListener(
      "click",
      (e) => {
        if (panel.hidden) return;
        const path = e.composedPath ? e.composedPath() : [];
        if (path.indexOf(host) >= 0) return;
        closePanel();
      },
      true
    );
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !panel.hidden) closePanel();
    });
  }

  function togglePanel() {
    panel.hidden = !panel.hidden;
  }
  function closePanel() {
    panel.hidden = true;
  }
  function setTip(text, bad) {
    const t = shadow.querySelector(".tip");
    t.textContent = text || "";
    t.classList.toggle("bad", !!bad);
  }

  /* 跟随媒体元素：fixed 定位 + 每帧算一次坐标（视频容器常带 transform，
     把按钮塞进容器里会被 overflow/contain 裁掉，所以挂在 body 上算坐标）。
     只在数值真的变了才写样式 —— 每帧无条件写 style 会逼浏览器重排。 */
  let lastBox = "";
  function place() {
    if (!media || !media.isConnected) {
      host.style.display = "none";
      return;
    }
    const r = media.getBoundingClientRect();
    if (media.tagName === "AUDIO" || r.width < 40 || r.height < 30 || r.bottom < 0 || r.top > innerHeight) {
      if (media.tagName !== "AUDIO") {
        host.style.display = "none";
        return;
      }
    }
    host.style.display = "block";
    if (media.tagName === "AUDIO") {
      // 音频元素没有可见尺寸：按钮固定待在右下角
      const box = "audio";
      if (box !== lastBox) {
        host.style.left = "auto";
        host.style.top = "auto";
        host.style.right = "18px";
        host.style.bottom = "18px";
        host.style.width = "auto";
        host.style.height = "auto";
        lastBox = box;
      }
      return;
    }
    const box = [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)].join(",");
    if (box === lastBox) return;
    lastBox = box;
    host.style.left = Math.max(4, r.left) + "px";
    host.style.top = Math.max(4, r.top) + "px";
    host.style.right = "auto";
    host.style.bottom = "auto";
    host.style.width = r.width + "px";
    host.style.height = r.height + "px";
  }

  /* ---------------------------------------------------------------- *
   * 动作
   * ---------------------------------------------------------------- */

  function pageTitle() {
    let t = (document.title || "").trim();
    t = t.replace(/\s*[-_|·—]\s*(bilibili|Bilibili|B站|YouTube|抖音|快手|小红书|微博|知乎).*$/, "");
    t = t.replace(/[\\/:*?"<>|]/g, "_").slice(0, 120);
    return t || "video";
  }

  /* 解析结果里没有 ext 字段，扩展名按 mime 推：
     yt-dlp 的 audio/mp4 就是 m4a，audio/webm 存成 weba 播放器才认。 */
  function extOf(f) {
    const mime = String((f && f.mime) || "").toLowerCase();
    if (f && f.kind === "audio") {
      if (mime.indexOf("webm") >= 0) return "weba";
      if (mime.indexOf("mp4") >= 0) return "m4a";
      return "mp3";
    }
    if (mime.indexOf("webm") >= 0) return "webm";
    return "mp4";
  }

  /* 直链的扩展名：URL 里带着就用它的，带着查询串的（googlevideo 那种）
     按 media 是音频/视频给一个常见值。 */
  function extOfUrl(u, isAudio) {
    try {
      const p = new URL(u).pathname;
      const m = /\.([a-z0-9]{2,5})$/i.exec(p);
      if (m) return m[1].toLowerCase();
    } catch (_) {
      /* ignore */
    }
    return isAudio ? "m4a" : "mp4";
  }

  /* 抖音信息流的播放弹窗 URL 形如 douyin.com/jingxuan?modal_id=<aweme_id>
     （/discover 等同理），yt-dlp 只认 douyin.com/video/<aweme_id>，
     原样丢过去就是 "Unsupported URL"。modal_id 就是 aweme_id，换过去即可。
     referer 仍用原始地址（防盗链按它校验），只有交给解析器的要归一化。 */
  function canonicalPageUrl() {
    try {
      const p = new URL(location.href);
      if (!/(^|\.)douyin\.com$/.test(p.hostname)) return location.href;
      const mid = p.searchParams.get("modal_id");
      if (mid && /^\d+$/.test(mid) && !p.pathname.startsWith("/video/")) {
        return "https://www.douyin.com/video/" + mid;
      }
    } catch (_) {
      /* ignore */
    }
    return location.href;
  }

  async function resolveAndPush() {
    const height = Number(shadow.querySelector(".h").value) || 1080;
    setTip("正在解析页面…");
    const page = canonicalPageUrl();
    const r = await send({ type: "zdm-youtube-resolve", url: page });
    if (!r || !r.ok || !(r.formats || []).length) {
      const err = String((r && r.error) || "没有可用格式");
      /* 报错要能指导下一步，不然用户只看到一句英文 */
      if (/unsupported url/i.test(err)) {
        setTip(
          "yt-dlp 不认识这个页面（首页/信息流通常不行，要具体内容页）。改用上面的「流媒体」或嗅探面板里的 HLS 条目",
          true
        );
      } else if (/fresh cookies|cookies .*needed|login|sign in/i.test(err)) {
        setTip(
          "该站点需要登录态：先在浏览器登录这个站点再试，或改用上面的「流媒体」",
          true
        );
      } else {
        setTip("解析失败：" + err + "，可改用上面的「流媒体」", true);
      }
      return;
    }
    /* 选档：优先正好命中用户选的清晰度；没有就取不超过它的最高一档；
       都没有（比如页面只有 360P）就取最高一档。 */
    const fmts = r.formats.filter((f) => f && f.url);
    let pick =
      fmts.find((f) => Number(f.height) === height) ||
      fmts
        .filter((f) => Number(f.height) && Number(f.height) <= height)
        .sort((a, b) => Number(b.height) - Number(a.height))[0] ||
      fmts.sort((a, b) => Number(b.height || 0) - Number(a.height || 0))[0];
    /* 音乐站点解析回来可能只有音频轨，kind 要照实写，否则后端会按视频
       去配音频、下出一个奇怪的容器。
       HLS / DASH 播放列表同理：后端要按播放列表处理（ffmpeg 整条拉）。 */
    const isAudioPick = pick.kind === "audio";
    const isStreamPick = pick.kind === "hls" || pick.kind === "dash";
    const res = await send({
      type: "sniffer-push",
      item: {
        url: pick.url,
        filename: (r.title || pageTitle()) + "." + extOf(pick),
        referer: location.href,
        cookie: document.cookie || undefined,
        source: "playerbtn",
        kind: isAudioPick ? "audio" : isStreamPick ? pick.kind : "video",
        // DASH 站点：yt-dlp 已经把配套音轨配好了，不带就下成无声
        audio_url: isAudioPick ? undefined : pick.audio_url || undefined,
        // 刻意**不传** page_url：带了它后端会改走 yt-dlp 通道重新解析一遍，
        // 而那条通道要的是 Netscape 原文（带域信息）；我们这里给的是压成
        // 单行的 header 形式，旧版客户端会把它铺到 .youtube.com 域上，
        // 抖音的登录态就发不出去。已经解析好的直链直接下、后端 ffmpeg
        // 合流即可，不需要再解析一次。
        height: Number(pick.height) || height,
      },
    });
    if (res && res.ok) {
      setTip(
        "已推送给 ZDM：" +
          (isAudioPick ? "音频" : (pick.height || "?") + "P")
      );
      closePanel();
    } else {
      setTip("推送失败，请确认 ZDM 正在运行", true);
    }
  }

  /* 从嗅探列表里给当前视频找音频轨：先问 sniffer.js 要列表，
     没有就走 background 的配对（消息 sniffer-push 内部会做）。 */
  async function audioForCurrent() {
    const url = directUrl(media);
    if (!url) return "";
    const api = window.__zdmSniffer;
    if (api && typeof api.items === "function") {
      const items = api.items() || [];
      const vh = (() => {
        try {
          return new URL(url).hostname;
        } catch (_) {
          return "";
        }
      })();
      const ph = (u, n) => {
        try {
          return new URL(u).pathname.split("/").filter(Boolean).slice(0, n).join("/");
        } catch (_) {
          return "";
        }
      };
      let best = "";
      let bestScore = 0;
      for (const it of items) {
        if (!it || it.kind !== "audio" || !it.url || it.url === url) continue;
        let s = 0;
        try {
          const h = new URL(it.url).hostname;
          if (h === vh) s += 100;
          else if (h.split(".").slice(-2).join(".") === vh.split(".").slice(-2).join(".")) s += 60;
        } catch (_) {
          /* ignore */
        }
        if (ph(it.url, 2) && ph(it.url, 2) === ph(url, 2)) s += 40;
        if (s > bestScore) {
          bestScore = s;
          best = it.url;
        }
      }
      if (best) return best;
    }
    return "";
  }

  /* 嗅探到的播放列表（m3u8 / mpd）。
   *
   *  这是**最通用**的一条路：抖音、快手、X、Facebook、绝大多数直播与
   *  自适应流站点，页面都会去取一份播放列表，扩展能嗅到它。取最新的那
   *  一条（页面刚发的请求，对应的就是当前正在播的视频），推给后端由
   *  ffmpeg 整条拉下来。
   *
   *  为什么需要它：yt-dlp 只覆盖 2000+ 站点里的**一部分**——实测快手
   *  只有 generic extractor（Unsupported URL），X 必须是具体推文页
   *  （x.com/home 直接 Unsupported）。而嗅探到的播放列表不挑站点。 */
  async function streamForCurrent() {
    const api = window.__zdmSniffer;
    if (!api || typeof api.items !== "function") return null;
    let list = [];
    try {
      list = api.items() || [];
    } catch (_) {
      return null;
    }
    const streams = list.filter(
      (x) => x && (x.kind === "hls" || x.kind === "dash") && x.url
    );
    if (!streams.length) return null;
    // 最新发的那条 playlist 才是当前视频的（页面上多个视频轮流播时，
    // 只有最后请求的那个对应现在的画面）
    streams.sort((a, b) => (b.at || 0) - (a.at || 0));
    return streams[0];
  }

  async function pushStream() {
    const s = await streamForCurrent();
    if (!s) {
      setTip("这一页还没嗅到播放列表（HLS/DASH）。播放几秒再试，或用下面的其它方式", true);
      return;
    }
    const res = await send({
      type: "sniffer-push",
      item: {
        url: s.url,
        filename: pageTitle() + ".mp4",
        referer: location.href,
        cookie: document.cookie || undefined,
        source: "playerbtn",
        kind: s.kind,
      },
    });
    if (res && res.ok) {
      setTip("已推送给 ZDM：后端会取分片并合并成 mp4");
      closePanel();
    } else {
      setTip("推送失败，请确认 ZDM 正在运行", true);
    }
  }

  /* 嗅探到的最新视频直链。
   *
   *  抖音 / 快手的网页播放器给的往往就是**完整 mp4 直链**（不是 m3u8），
   *  而且它出现在 <video> 的 currentSrc 是 blob: 的页面上 —— 于是
   *  「直接下载当前视频」拿不到地址，但嗅探列表里有。这种站点走这条
   *  路最快。 */
  async function sniffedVideo() {
    const api = window.__zdmSniffer;
    if (!api || typeof api.items !== "function") return null;
    let list = [];
    try {
      list = api.items() || [];
    } catch (_) {
      return null;
    }
    const vids = list.filter((x) => x && x.kind === "video" && x.url);
    if (!vids.length) return null;
    vids.sort((a, b) => (b.at || 0) - (a.at || 0));
    return vids[0];
  }

  async function pushSniffed() {
    const it = await sniffedVideo();
    if (!it) {
      setTip("这一页还没嗅到视频直链，播放几秒再试", true);
      return;
    }
    const res = await send({
      type: "sniffer-push",
      item: {
        url: it.url,
        filename: pageTitle() + ".mp4",
        referer: location.href,
        cookie: document.cookie || undefined,
        source: "playerbtn",
        kind: "video",
      },
    });
    if (res && res.ok) {
      setTip("已推送给 ZDM");
      closePanel();
    } else {
      setTip("推送失败，请确认 ZDM 正在运行", true);
    }
  }

  async function act(what) {
    if (busy) return;
    busy = true;
    try {
      if (what === "hls") {
        await pushStream();
      } else if (what === "sniffed") {
        await pushSniffed();
      } else if (what === "resolve") {
        await resolveAndPush();
      } else if (what === "direct") {
        const url = directUrl(media);
        if (!url) {
          setTip("这个播放器用加密串流（MSE），拿不到直链，请用上面的「流媒体」或「解析此页面」", true);
          return;
        }
        const isAudio = media.tagName === "AUDIO";
        const res = await send({
          type: "sniffer-push",
          item: {
            url,
            filename: pageTitle() + "." + extOfUrl(url, isAudio),
            referer: location.href,
            cookie: document.cookie || undefined,
            source: "playerbtn",
            kind: isAudio ? "audio" : "video",
          },
        });
        setTip(
          res && res.ok
            ? isAudio
              ? "已推送"
              : "已推送（若没声音，是站点把音画分开了）"
            : "推送失败",
          !(res && res.ok)
        );
        if (res && res.ok) closePanel();
      } else if (what === "merge") {
        const url = directUrl(media);
        const audio = await audioForCurrent();
        if (!url || !audio) {
          setTip("还没嗅到配套音频轨，先播放几秒再试，或用「解析此页面」", true);
          return;
        }
        const res = await send({
          type: "sniffer-push",
          item: {
            url,
            filename: pageTitle() + "." + extOfUrl(url, false),
            referer: location.href,
            cookie: document.cookie || undefined,
            source: "playerbtn",
            kind: "video",
            audio_url: audio,
          },
        });
        setTip(res && res.ok ? "已推送，音视频将合流" : "推送失败", !(res && res.ok));
        if (res && res.ok) closePanel();
      } else if (what === "panel") {
        const api = window.__zdmSniffer;
        if (api && typeof api.open === "function") {
          if (typeof api.scanImages === "function") api.scanImages();
          api.open();
        }
        closePanel();
      }
    } finally {
      busy = false;
    }
  }

  /* ---------------------------------------------------------------- *
   * 生命周期：SPA 换页、元素被替换、全屏切换
   * ---------------------------------------------------------------- */

  function sync() {
    const el = pickMedia();
    if (el !== media) {
      media = el;
      lastBox = "";
      if (media) {
        const isAudio = media.tagName === "AUDIO";
        shadow.querySelector(".tt").textContent = isAudio ? "下载此音频" : "下载此视频";
        shadow.querySelector(".bt").textContent = isAudio ? "下载音频" : "下载此视频";
        const mergeBtn = shadow.querySelector('[data-act="merge"]');
        const directBtn = shadow.querySelector('[data-act="direct"]');
        if (isAudio) {
          mergeBtn.style.display = "none";
          directBtn.querySelector(".t").textContent = "直接下载此音频";
        } else {
          mergeBtn.style.display = "";
          directBtn.querySelector(".t").textContent = "直接下载当前视频";
        }
        audioCheckedAt = 0;
        setTip("");
      } else {
        host.style.display = "none";
      }
    }
    if (media) place();
    /* 「下载视频+音频」只在真的嗅到音频轨时才亮起来，避免给一个点了没反应的按钮。
       直链没变就复用上次结论：sync() 每帧跑，不能每次都去翻嗅探列表。 */
    if (media && media.tagName === "VIDEO") {
      const url = directUrl(media);
      const now = Date.now();
      if (url !== lastAudioUrl || now - audioCheckedAt > 1500) {
        lastAudioUrl = url;
        audioCheckedAt = now;
        const mergeBtn = shadow.querySelector('[data-act="merge"]');
        audioForCurrent().then((a) => {
          // 异步回来时元素可能已经被换掉了，别把旧结论写到新按钮上
          if (media && directUrl(media) !== url) return;
          mergeBtn.disabled = !a;
          mergeBtn.querySelector(".k").textContent = a ? "可用" : "";
        });
      }
    }
  }

  function start() {
    if (!document.documentElement) {
      setTimeout(start, 200);
      return;
    }
    build();
    const loop = () => {
      sync();
      requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
    // rAF 在页面切到后台会停，回来时补一次
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) sync();
    });
    window.addEventListener("fullscreenchange", () => setTimeout(sync, 60));
  }

  start();
})();
