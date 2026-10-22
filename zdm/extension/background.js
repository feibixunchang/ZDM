/* ZDM Connector - background service worker */

const DEFAULTS = {
  serverUrl: "http://127.0.0.1:9000",
  intercept: true,
  connections: 8,
  directory: "",
  notify: true,
};

/* How long to wait for ZDM to finish starting after we wake it up. A cold
   start of the WPF client on a slow disk can take several seconds. */
const WAKE_TIMEOUT_MS = 20000;

/* ------------------------------------------------------------------ *
 * 媒体嗅探（v1.12.0）
 *
 * 判据放在 service worker 而不是 content script：只有 webRequest 拿得到
 * 响应的 Content-Type / Content-Length，而这两个头才是「这是不是一个
 * 可下载的媒体文件」的唯一可靠证据（页面里的 <video> 元素既可能是
 * blob: 也可能只是个播放器外壳）。
 *
 * 范围说明：这一版只认「直链媒体」（服务器直接吐字节流的 mp4/mp3/…）。
 * HLS(.m3u8) / DASH(.mpd) 是分片协议，需要的是「拉取+拼接+混流」引擎，
 * 属于下一版的范围，所以这里刻意不把它们算进来——列出一堆点不动
 * 的条目比不列更糟。
 *
 * .m4s 必须算进来：B站的 DASH 视频轨/音频轨就是 .m4s 直链，Content-Type
 * 是 application/octet-stream —— 不认它，B站一个媒体都嗅不到。
 * ------------------------------------------------------------------ */
const MEDIA_EXT =
  /\.(mp4|m4v|webm|mkv|mov|m4a|mp3|aac|flac|wav|ogg|oga|opus|wma|avi|wmv|flv|f4v|3gp|ts|m2ts|m4s)(?:$|[?#])/i;
const MEDIA_AUDIO_EXT = /\.(mp3|m4a|aac|flac|wav|ogg|oga|opus|wma)(?:$|[?#])/i;
/* B站 DASH 轨的 URL 编号规则（实测）：
     视频轨  ".../62131_da3-1-30032.m4s"   —— 30000 + 清晰度id（30006…30127）
     音频轨  ".../62131_da3-1-30216.m4s"   —— 302xx 系列（30216/30232/30250/30251/30257/30259/30280）
   所以「≥30000 就是音频」是错的：那会把所有视频轨误判成音频。
   Content-Type 两者都是 application/octet-stream，只能从编号认。 */
// 5~6 位都可能有：视频轨 100026 / 100110（6 位），音频轨 30216 / 30280（5 位）。
// 只写 \d{5} 会让 6 位的高码率视频轨完全匹配不上。
const BILI_AUDIO_ID = /-(\d{5,6})\.m4s(?:$|[?#])/;
function isBiliAudioId(id) {
  return id >= 30200 && id < 30300;
}
/* YouTube 音频 itag。有些 videoplayback 请求的 URL 不带 mime= 参数，
   只看 mime 会把音频轨当成「一条 0P 视频」塞进列表 —— 于是推送时永远
   配不到音频，下下来只有画面。itag 是可靠的第二判据。 */
const YT_AUDIO_ITAG = new Set([139, 140, 141, 249, 250, 251, 256, 258, 325, 328]);
const BILI_MEDIA_HOST = /(^|\.)bilivideo\.(com|cn)$/;
const YT_MEDIA_HOST = /(^|\.)googlevideo\.com$/;

/** B站 DASH 直链里的 cid（/upgcxcode/99/91/137649199/xxx-1-30280.m4s）。 */
function biliCidOf(url) {
  const m = /\/upgcxcode\/\d+\/\d+\/(\d+)\//.exec(url || "");
  return m ? m[1] : "";
}
function biliAudioCode(url) {
  const m = /-(\d{5,6})\.m4s(?:$|[?#])/.exec(url || "");
  return m ? Number(m[1]) : 0;
}
function hostOf(url) {
  try {
    return new URL(url).hostname.toLowerCase();
  } catch (_) {
    return "";
  }
}

/* 播放列表的**分片**不是独立成品。
 *
 *  实测 2026-10-22：X（Twitter）的 DASH 分片是 .m4s、Content-Type 报
 *  video/mp4，以前整条被判成「视频」—— 推下去只拿到 4 秒钟的碎片
 *  （159 KB 的 .mp4），用户看到的就是「推特下载下来不是真的视频」。
 *  打上 seg 标记：面板照常列出（用户可能就想看某个分片），但
 *  playerbtn 的「下载嗅探到的视频轨」要跳过它们、批量下载不勾它们。
 *
 *  判据两条，满足其一即算：
 *  1. 后缀是分片后缀。**B站除外** —— B站的 .m4s 是整条轨（按
 *     cid-编号命名、支持 Range），标错会让音视频配对失效；
 *  2. 同一个 tab 里已经有一条播放列表、且这条媒体跟它同目录
 *     （Twitter 的 master playlist 和分片就在同一目录下）。
 */
const SEGMENT_EXT =
  /\.(m4s|m4f|fmp4|mp4a|ts|m2ts|cmfv|cmfa)(?:$|[?#])/i;
const FULL_CONTAINER_EXT = /\.(mp4|webm|mkv|mov|m4v|avi|flv)(?:$|[?#])/i;

function isSegmentMedia(tabId, url, host) {
  if (!url) return false;
  if (
    SEGMENT_EXT.test(url) &&
    !/\/upgcxcode\//.test(url) &&
    !BILI_MEDIA_HOST.test(host)
  ) {
    return true;
  }
  // 完整容器格式（mp4/webm/…）几乎不会是分片，别用目录规则误伤
  if (FULL_CONTAINER_EXT.test(url)) return false;
  const list = snifferByTab.get(tabId) || [];
  const plays = list.filter(
    (x) => x && (x.kind === "hls" || x.kind === "dash") && x.url
  );
  if (!plays.length) return false;
  try {
    const u = new URL(url);
    const dir = u.origin + u.pathname.replace(/\/[^/]*$/, "");
    return plays.some((p) => p.url.indexOf(dir + "/") === 0);
  } catch (_) {
    return false;
  }
}

/** 去掉分段请求的 range 参数。
    YouTube 的 videoplayback 是按 Range 分片拉的，URL 里带着 range=0-1234567
    和 rn=8。后端拿着这种地址用普通 GET 下载，只会拿到那一小段 ——
    表现为「下完只有几十 KB / 22.7 KB」。整段下载必须去掉这两个参数。 */
function stripRange(u) {
  try {
    const p = new URL(u);
    p.searchParams.delete("range");
    p.searchParams.delete("rn");
    return p.toString();
  } catch (_) {
    return u;
  }
}

/** 可解析主域（去掉子域）：a.b.com / c.b.com → b.com */
function registrableDomain(host) {
  const p = String(host || "").split(".").filter(Boolean);
  return p.length <= 2 ? p.join(".") : p.slice(-2).join(".");
}
/** URL 路径的前 n 段。/20241010/ab/xyz.mp4 → "20241010/ab" */
function pathHead(u, n) {
  try {
    return new URL(u).pathname.split("/").filter(Boolean).slice(0, n).join("/");
  } catch (_) {
    return "";
  }
}
/** 通用站点（非 YouTube / 非B站）的 DASH 音频轨识别。
 *
 *  DASH 的音频轨用 fMP4 容器，Content-Type 报 video/mp4 是常态（B站就是这样），
 *  抖音 / 快手 / Twitter / Facebook 无一例外。所以音频判定必须看 URL 特征，
 *  且必须排在 Content-Type 之前 —— 否则这些站点的音频轨全被判成视频，
 *  嗅探列表里一条音频都没有，从面板推送必然「只有画面没声音」。
 *
 *  判据（宁缺毋滥，配错一条背景音乐比没声音更糟）：
 *   - 路径里出现独立的 audio 段：.../audio.m4s、/audio/seg.m4s、x-audio.mp4
 *   - query 里显式声明媒体类型：media_type=audio / mime=audio / type=audio
 *  `?audio=1` 这种不算（视频轨也常带多音轨参数，会误伤）。
 */
function genericAudioUrl(url) {
  let path = "";
  let query = "";
  try {
    const u = new URL(url);
    path = u.pathname;
    query = u.search;
  } catch (_) {
    return false;
  }
  if (/[/_-]audio(?:[._/-]|$)/i.test(path)) return true;
  if (/(?:^|[?&])(?:media_type|mime|type|format)=audio(?:%|&|$)/i.test(query)) {
    return true;
  }
  return false;
}

/** 一条音频轨与一条视频轨「是不是同一批 DASH 分发」的置信分。
 *
 *  完全同 host → 100（同一套分发域名，最强信号）；
 *  跨子域但同主域（CDN 多域名）时看目录：路径前两段相同 → 100，
 *  只第一段相同 → 75，完全不同 → 60。
 *  调用方要求 ≥100 才配对，所以「同主域但目录完全不同」这种
 *  （多半是页面背景音乐）会被挡掉。 */
function dashAffinity(audioUrl, videoUrl) {
  const ah = hostOf(audioUrl);
  const vh = hostOf(videoUrl);
  if (!ah || !vh) return 0;
  if (ah === vh) return 100;
  if (registrableDomain(ah) !== registrableDomain(vh)) return 0;
  const a2 = pathHead(audioUrl, 2);
  const v2 = pathHead(videoUrl, 2);
  if (a2 && a2 === v2) return 100;
  if (pathHead(audioUrl, 1) && pathHead(audioUrl, 1) === pathHead(videoUrl, 1)) return 75;
  return 60;
}

/** 在同一个标签页的嗅探结果里替一条视频轨找配套音频轨。
 *
 *  DASH 把画面和声音拆成两次请求：嗅探面板里能点「推送」的只有视频轨，
 *  音频轨是另一条（B站 -1-30280.m4s / YouTube mime=audio）。如果你从
 *  嗅探面板推送，以前后端收到的 audio_url 是空的，于是下下来只有画面。
 *
 *  B站按 cid 配对（URL 路径里的 /99/91/<cid>/），码率高的优先；
 *  YouTube 没有 cid，取同批里体积最大的一条音频轨；
 *  其它站点（抖音 / 快手 / Facebook / X / 新闻站……）走通用规则：
 *  按 DASH 亲和分筛选，并在列表里从视频轨的位置向两侧就近找 ——
 *  音视频轨是几乎同时发出的请求，在插入顺序里必然相邻，这一条同时
 *  保证了「同一次播放」而不是「这个页面很久以前放过的背景音乐」。 */
function audioSiblingFor(tabId, url) {
  if (tabId == null || !url) return null;
  const list = snifferByTab.get(tabId) || [];
  const audios = list.filter(
    (it) => it && it.kind === "audio" && it.url && it.url !== url
  );
  if (!audios.length) return null;
  const cid = biliCidOf(url);
  if (cid) {
    const same = audios.filter((a) => biliCidOf(a.url) === cid);
    if (!same.length) return null;
    // 30280(192k) > 30232(132k) > 30216(64k)
    same.sort((a, b) => biliAudioCode(b.url) - biliAudioCode(a.url));
    return same[0];
  }
  if (YT_MEDIA_HOST.test(hostOf(url))) {
    audios.sort((a, b) => (b.size || 0) - (a.size || 0));
    return audios[0];
  }
  /* 通用站点：亲和分 ≥100（同 host，或同主域且同目录）。
     从视频轨所在位置向两侧扩散，取最近的那条 —— 相邻即同时刻。
     视频轨不在列表里时（at<0）相邻性无从判断，宁可不配：
     乱配一条背景音乐进去，合出来的文件比没声音更糟。 */
  const at = list.findIndex((it) => it && it.url === url);
  if (at < 0) return null;
  const audioIdx = audios
    .map((a) => ({ i: list.indexOf(a), a }))
    .filter((x) => x.i >= 0 && dashAffinity(x.a.url, url) >= 100)
    .sort((x, y) => {
      const dx = Math.abs(x.i - at);
      const dy = Math.abs(y.i - at);
      // 近的优先；同样近就取体积大的（码率高的音轨）
      return dx - dy || (y.a.size || 0) - (x.a.size || 0);
    });
  return audioIdx.length ? audioIdx[0].a : null;
}
const MIN_MEDIA_BYTES = 64 * 1024; // 小于这个的多半是图标、埋点、预告片
const MAX_PER_TAB = 60;
const MAX_TABS = 30; // storage.session 只有 10MB，别把页签全存爆

/** tabId -> [{url, kind, size, name, from}]（内存态，SW 被回收就重来） */
const snifferByTab = new Map();
/* tabId → { itag: 字节数 }。页面把 adaptiveFormats 的长度表交过来后存在这里，
   之后新抓到的条目一进来就能补上大小（嗅探面板才不会一直写「大小未知」）。 */
const lensByTab = new Map();

/* MV3 的 service worker 空闲 30 秒就被回收，内存态全丢。存进
   chrome.storage.session（SW 重启仍在，关浏览器才清）之后：
   - 用户先打开页面、隔一会儿再播放，媒体照样列得出来；
   - SW 重启后 webRequest 监听器会随顶层代码重新注册，内容脚本的定时
     轮询负责把它唤醒（否则休眠期间漏掉整个播放过程）。 */
const STORE_KEY = "snifferByTab";
/* itag 表也要落盘。MV3 的 SW 空闲 30 秒就被回收：snifferByTab 会恢复，
   而 lensByTab 只在内存里 —— 用户下完两个视频（几分钟）再打开嗅探面板时，
   条目是从存储里恢复出来的、长度表却早没了，于是永远显示「大小未知」。
   这正是「扩展改了还是未知」的一个真实原因。 */
const LENS_KEY = "lensByTab";
/* tabId → {pageUrl, title, rows:[{height,size,label}]}
   YouTube 专用：SABR 之后播放器对 /videoplayback 发的是 POST 流式请求，
   响应 chunked、没有长度、URL 里也没有 itag/clen —— 那种「条目」本身没有
   文件大小可言。能说清「1080P 有多大」的只有 adaptiveFormats 那张表，
   所以面板在 YouTube 上展示这份阶梯，而不是那几条裸直链。 */
const ladderByTab = new Map();
const LADDER_KEY = "ladderByTab";
let restored = null;

function ensureRestored() {
  if (restored) return restored;
  restored = chrome.storage.session
    .get([STORE_KEY, LENS_KEY, LADDER_KEY])
    .then((d) => {
      const m = d && d[STORE_KEY];
      if (m && typeof m === "object") {
        for (const [k, v] of Object.entries(m)) {
          const id = Number(k);
          if (Number.isFinite(id) && Array.isArray(v)) snifferByTab.set(id, v);
        }
      }
      const l = d && d[LENS_KEY];
      if (l && typeof l === "object") {
        for (const [k, v] of Object.entries(l)) {
          const id = Number(k);
          if (Number.isFinite(id) && v && typeof v === "object") {
            lensByTab.set(id, v);
          }
        }
      }
      const g = d && d[LADDER_KEY];
      if (g && typeof g === "object") {
        for (const [k, v] of Object.entries(g)) {
          const id = Number(k);
          if (Number.isFinite(id) && v && typeof v === "object") {
            ladderByTab.set(id, v);
          }
        }
      }
    })
    .catch(() => {
      /* 存储不可用就退回纯内存，功能降级但不炸 */
    });
  return restored;
}
ensureRestored();

function persist() {
  const obj = {};
  let n = 0;
  for (const [k, v] of snifferByTab) {
    if (n++ >= MAX_TABS) break;
    obj[k] = v;
  }
  try {
    chrome.storage.session.set({ [STORE_KEY]: obj }).catch(() => {});
  } catch (_) {
    /* 忽略 */
  }
}

function persistLens() {
  const obj = {};
  let n = 0;
  for (const [k, v] of lensByTab) {
    if (n++ >= MAX_TABS) break;
    obj[k] = v;
  }
  try {
    chrome.storage.session.set({ [LENS_KEY]: obj }).catch(() => {});
  } catch (_) {
    /* 忽略 */
  }
}

function persistLadder() {
  const obj = {};
  let n = 0;
  for (const [k, v] of ladderByTab) {
    if (n++ >= MAX_TABS) break;
    obj[k] = v;
  }
  try {
    chrome.storage.session.set({ [LADDER_KEY]: obj }).catch(() => {});
  } catch (_) {
    /* 忽略 */
  }
}

function nameFromUrl(url, kind) {
  let name = "";
  try {
    name = decodeURIComponent(new URL(url).pathname.split("/").filter(Boolean).pop() || "");
  } catch (_) {
    /* ignore */
  }
  name = name.replace(/[\\/:*?"<>|]/g, "_").slice(0, 120);
  if (!name || !/\.[a-z0-9]{2,5}$/i.test(name)) {
    /* 图片以前也被补成 .mp4：扫图片推下去的文件名是 JPEG 数据，
       存成 .mp4 用户当然打不开。按 kind 分别补后缀。 */
    name =
      (name || "media") +
      (kind === "audio" ? ".mp3" : kind === "image" ? ".jpg" : ".mp4");
  }
  /* B站的 DASH 轨是 .m4s（分段 MP4）。存成 .m4s 用户认不出是什么，
     播放器也不认 —— 按音/视频轨分别落成 .mp4 / .m4a。 */
  if (/\.m4s$/i.test(name)) {
    name = name.replace(/\.m4s$/i, kind === "audio" ? ".m4a" : ".mp4");
  }
  /* 播放列表本身不是成品：后端用 ffmpeg 拉完会封装成 mp4，
     文件名也该是 .mp4。 */
  if (kind === "hls" || kind === "dash") {
    name = name.replace(/\.(m3u8|mpd)$/i, "") || "stream";
    if (!/\.[a-z0-9]{2,5}$/i.test(name)) name += ".mp4";
  }
  return name;
}

/** 返回 {url, kind, size, name} 或 null（不是媒体）。 */
function classifyResponse(details) {
  if (details.tabId == null || details.tabId < 0) return null;
  /* 只认最终落地的那一次响应。200 是整段，206 是 Range 分段 ——
     视频站（B站/YouTube）播放时全是分段请求，只认 200 会把它们全滤掉。 */
  if (details.statusCode !== 200 && details.statusCode !== 206) return null;
  const url = details.url || "";
  let ct = "";
  let total = 0;   // 整个资源的大小（Content-Range: bytes x-y/TOTAL）
  let partLen = 0; // 这一次响应的大小（Content-Length）
  for (const h of details.responseHeaders || []) {
    const n = (h.name || "").toLowerCase();
    if (n === "content-type") ct = (h.value || "").toLowerCase().split(";")[0].trim();
    else if (n === "content-range") {
      // 只在没有完整长度时才用 Range 的总数
      const m = /\/\s*(\d+)\s*$/.exec(h.value || "");
      if (m && !total) total = parseInt(m[1], 10) || 0;
    } else if (n === "content-length") {
      partLen = parseInt(h.value || "0", 10) || 0;
    }
  }
  // Range 响应的 Content-Length 只是这一小段的长度（实测 B站是 1024），
  // 拿它当文件大小会把它误判成「小于 64KB 的埋点」而丢掉。
  const len = total || partLen;
  const host = (() => {
    try {
      return new URL(url).hostname.toLowerCase();
    } catch (_) {
      return "";
    }
  })();
  // YouTube 的媒体走 *.googlevideo.com/videoplayback：URL 没有 .mp4/.m4s
  // 后缀，Content-Type 也可能是 application/octet-stream。纯靠扩展名/
  // ContentType 会把整个 YouTube 漏掉（嗅探恒为 0）。按 host 直接认定。
  const KNOWN_MEDIA_HOST = YT_MEDIA_HOST;
  let kind = null;
  /* HLS / DASH 播放列表：它们不是媒体文件，而是「分片清单」。
     必须单独认出来 —— 以前 MEDIA_EXT 里没有 m3u8/mpd，这些请求被整个
     丢掉，于是嗅探列表里只剩几百字节的分片（用户看到的就是
     「300 B」「279 KB」那种），推下去只能下到一个碎片。
     推播放列表给后端 = 让 ffmpeg 跟着 playlist 把整条流拉下来。 */
  const HLS_EXT = /\.m3u8(?:$|[?#])/i;
  const DASH_EXT = /\.mpd(?:$|[?#])/i;
  if (HLS_EXT.test(url) || ct.indexOf("mpegurl") >= 0) {
    kind = "hls";
  } else if (DASH_EXT.test(url) || ct.indexOf("dash+xml") >= 0) {
    kind = "dash";
  } else if (KNOWN_MEDIA_HOST.test(host)) {
    /* YouTube 的音频轨也是 /videoplayback，Content-Type 常被报成
       video/mp4 或干脆没有。真实类型在 URL 的 mime= 参数里
       （mime=audio%2Fmp4 / video%2Fmp4），没有 mime 时看 itag。 */
    let mime = "";
    let itag = 0;
    try {
      const u = new URL(url);
      mime = u.searchParams.get("mime") || "";
      itag = Number(u.searchParams.get("itag") || 0);
    } catch (_) {
      mime = "";
    }
    kind = mime.startsWith("audio/") || YT_AUDIO_ITAG.has(itag) ? "audio" : "video";
  } else if (/\/upgcxcode\//.test(url) || BILI_MEDIA_HOST.test(host)) {
    // B站 DASH：视频轨 -1-30080.m4s，音频轨 -1-30280.m4s，只能从编号认。
    const m = BILI_AUDIO_ID.exec(url);
    kind = m && isBiliAudioId(Number(m[1])) ? "audio" : "video";
  } else if (genericAudioUrl(url)) {
    /* 通用 DASH 的音频轨：Content-Type 常报 video/mp4（fMP4 容器），
       必须排在 Content-Type 之前，否则抖音/快手/Twitter 的音频轨
       全被判成视频，嗅探列表里没有音频 → 推送必然没声音。 */
    kind = "audio";
  } else if (ct.startsWith("video/")) {
    kind = "video";
  } else if (ct.startsWith("audio/")) {
    kind = "audio";
  } else if (
    (ct === "application/octet-stream" || ct === "binary/octet-stream" || ct === "") &&
    MEDIA_EXT.test(url)
  ) {
    kind = MEDIA_AUDIO_EXT.test(url) ? "audio" : "video";
  }
  if (!kind) return null;
  /* 播放列表天生就小（几 KB），不能拿媒体文件的门槛去卡它 */
  const isPlaylist = kind === "hls" || kind === "dash";
  if (len && len < MIN_MEDIA_BYTES && !isPlaylist) return null;
  let size = len;
  if (!size) {
    // YouTube 的 videoplayback URL 自带 clen=<总字节数> 查询参数。响应头
    // 没给 Content-Range / Content-Length 时它就是唯一的长度来源 ——
    // 不解析它，YouTube 嗅探出来的条目大小永远是「未知」。
    try {
      const c = new URL(url).searchParams.get("clen");
      if (c) size = parseInt(c, 10) || 0;
    } catch (_) {
      /* ignore */
    }
  }
  if (size && size < MIN_MEDIA_BYTES && !isPlaylist) return null;
  const seg = isSegmentMedia(details.tabId, url, host);
  return { url, kind, size, name: nameFromUrl(url, kind), from: "net", seg: seg || undefined };
}

function rememberMedia(tabId, item) {
  let list = snifferByTab.get(tabId);
  if (!list) {
    list = [];
    snifferByTab.set(tabId, list);
  }
  /* 发现时间：播放器按钮要靠它判断「哪条 HLS 播放列表对应当前正在播的
     视频」——页面上多个视频轮流播时，只有最新发的那条 playlist 是对的。 */
  if (!item.at) item.at = Date.now();
  // 这张 tab 已经知道「itag → 字节数」的话，新条目一进来就补上大小，
  // 不要等页面的表格同步（那一步只在 boot 时跑一次，常常赶在条目之前）。
  if (!item.size) {
    const n = lensByTab.get(tabId);
    if (n) {
      const bytes = n[itagOf(item.url)];
      if (bytes) item.size = bytes;
    }
  }
  const dup = list.find((x) => x.url === item.url);
  if (dup) {
    // 旧版本的条目可能带着 size=0（比如当时的扩展没拿响应头）。同一 URL
    // 这次拿到了大小就补上，否则「大小未知」会一直挂在列表里刷新不掉。
    if (!dup.size && item.size) {
      dup.size = item.size;
      persist();
    }
    return false;
  }
  list.push(item);
  if (list.length > MAX_PER_TAB) list.shift();
  persist();
  // 拿不到大小的条目异步补一次。两条路：
  //  - YouTube（googlevideo）走桌面端问 —— SW 里 fetch 被 CORS 挡住；
  //  - 其它站点（B站 CDN 等）仍然用 SW 自己的 Range 请求，省一次抖动。
  if (!item.size && item.from === "net") {
    if (YT_MEDIA_HOST.test(hostOf(item.url))) {
      setTimeout(() => probeSizesViaBackend(tabId), 200);
    } else {
      setTimeout(() => probeSize(tabId, item), 60);
    }
  }
  return true;
}

/** 从 videoplayback 直链里取 itag。取不到返回 0。 */
function itagOf(url) {
  try {
    return Number(new URL(url).searchParams.get("itag") || 0);
  } catch (_) {
    return 0;
  }
}

/** 用「itag → 字节数」表补齐某个 tab 里所有还空着大小的条目。 */
function applyLens(tabId, lens) {
  const list = (tabId != null && snifferByTab.get(tabId)) || [];
  let changed = false;
  for (const it of list) {
    if (!it || it.size) continue;
    const n = lens[itagOf(it.url)];
    if (n > 0) {
      it.size = n;
      changed = true;
    }
  }
  return changed;
}

/* ------------------------------------------------- 交给桌面端问真实大小

   YouTube 的 videoplayback 现在大量走 SABR（POST 流式）：
   - 响应既没有 Content-Length 也没有 Content-Range（chunked）；
   - URL 里常常不带 itag / clen，所以「itag 表」也对不上号。
   浏览器侧所有办法都堵死：service worker 里 fetch 的 Origin 是
   chrome-extension://…，googlevideo 的 CORS 只放行 youtube.com，响应头
   一个都读不到（probeSize 里 catch 掉的正是这个）。
   桌面端没有同源策略，带同一份 Referer 发 2 字节 Range 就能读到
   `Content-Range: bytes 0-1/总长度` —— 所以这里把直链丢给后端去问。 */
const backendProbing = new Map(); // tabId -> Set(url)，同一 SW 生命周期内不重复问

async function probeSizesViaBackend(tabId) {
  if (tabId == null) return 0;
  const list = snifferByTab.get(tabId) || [];
  let seen = backendProbing.get(tabId);
  if (!seen) {
    seen = new Set();
    backendProbing.set(tabId, seen);
  }
  const todo = [];
  for (const it of list) {
    if (!it || it.size || !it.url || seen.has(it.url)) continue;
    if (!/^https?:\/\//i.test(it.url)) continue;
    seen.add(it.url);
    todo.push(it.url);
    if (todo.length >= 8) break; // 一次最多问 8 条，不把后端排满
  }
  if (!todo.length) return 0;
  let referer = "";
  try {
    const tab = await chrome.tabs.get(tabId);
    referer = (tab && tab.url) || "";
  } catch (_) {
    /* 标签页可能已经关了，没有 Referer 也能问 */
  }
  let r = null;
  try {
    r = await api("probe-size", { urls: todo, referer });
  } catch (_) {
    r = null;
  }
  const sizes = (r && r.sizes) || {};
  let changed = 0;
  for (const it of list) {
    if (!it || it.size) continue;
    const n = sizes[it.url];
    if (n > 0) {
      it.size = n;
      changed++;
    }
  }
  if (changed) {
    persist();
    await notifyTab(tabId, false); // 面板开着就立刻刷出大小，不用重开
  }
  return changed;
}

/* ---------------------------------------------------------- 清晰度阶梯兜底

   面板在 YouTube 上展示的是「清晰度 → 真实字节数」，来源本应是页面里的
   streamingData。但页面这条路有三种常见断法：
     1) 软导航换视频后，playerResponse 还是上一部的（或已被清空）；
     2) 有些视频只给 serverAbrStreamingUrl，adaptiveFormats 干脆没有；
     3) content script 还没来得及把阶梯推上来。
   而「每一档多大」这件事桌面端本来就答得出来（/api/youtube/resolve 走本机
   yt-dlp，实测 8 档全都带 size）。所以这里加一条兜底：面板一打开，如果
   阶梯还空着就直接问桌面端，拿到之后重绘面板。
   有缓存、有并发保护，同一个视频只问一次。 */
const ladderAsking = new Map(); // tabId -> videoId（正在问）
const ladderTriedAt = new Map(); // tabId -> { vid, at }：上次问的时间，避免反复打转

async function ensureLadderViaBackend(tabId, pageUrl) {
  if (tabId == null) return 0;
  const vid = videoIdOf(pageUrl || "");
  if (!vid) return 0;
  const cur = ladderByTab.get(tabId);
  if (cur && videoIdOf(cur.pageUrl || "") === vid && cur.rows && cur.rows.length) {
    // 全是估算值（页面给的 contentLength 不可信）时也要问一次：本机 yt-dlp
    // 报的是真实字节数，换上去用户才不会再看到「几百 KB 的 4K」。
    const allApprox = cur.rows.every((r) => r && r.approx);
    if (!allApprox) return cur.rows.length;
  }
  if (ladderAsking.get(tabId) === vid) return 0; // 已经在问了
  // 刚问过（含失败）就不要再问一遍：yt-dlp 一次要好几秒，
  // 每次打开面板都重跑会让面板一直转圈。
  const tried = ladderTriedAt.get(tabId);
  if (tried && tried.vid === vid && Date.now() - tried.at < 60000) return 0;
  ladderTriedAt.set(tabId, { vid, at: Date.now() });
  ladderAsking.set(tabId, vid);
  let r = null;
  try {
    let cookie = "";
    try {
      cookie = await youtubeCookieText();
    } catch (_) {
      cookie = "";
    }
    r = await api("youtube/resolve", {
      // 用规范化的 watch 地址：标签页 URL 可能带 list / t 之类的参数
      url: "https://www.youtube.com/watch?v=" + vid,
      cookie,
    });
  } catch (_) {
    r = null;
  } finally {
    if (ladderAsking.get(tabId) === vid) ladderAsking.delete(tabId);
  }
  const formats = (r && r.ok && r.formats) || [];
  const best = new Map(); // height -> row
  // 后端给的是原始 codec 串（avc1.640028 / vp9 / av01.0.08M），
  // 直接拿它和 "H.264" 比是永远不相等的 —— 必须先归一化，否则
  // 「同高度优先 H.264」这条规则形同虚设。
  const normCodec = (c) => {
    const s = String(c || "");
    if (/avc1|^h\.?26[45]$/i.test(s)) return "H.264";
    if (/vp0?9/i.test(s)) return "VP9";
    if (/av01/i.test(s)) return "AV1";
    return s;
  };
  for (const f of formats) {
    const h = Number(f && f.height) || 0;
    const size = Number(f && f.size) || 0;
    if (!h || !size) continue; // 纯音频轨没有 height，跳过
    const codec = normCodec(f.codec);
    const prev = best.get(h);
    // 同高度优先 H.264（浏览器/播放器兼容性最好）
    if (!prev || (codec === "H.264" && prev.codec !== "H.264")) {
      best.set(h, {
        height: h,
        size,
        codec,
        // 后端已经算好 label（「1080P · H.264」），有就直接用
        label: f.label || h + "P" + (codec ? " · " + codec : ""),
      });
    }
  }
  const rows = [...best.values()].sort((a, b) => b.height - a.height);
  if (!rows.length) return 0;
  // 期间用户可能已经切走：只写回到同一个视频的标签页
  let nowUrl = "";
  try {
    const tab = await chrome.tabs.get(tabId);
    nowUrl = (tab && tab.url) || "";
  } catch (_) {
    nowUrl = "";
  }
  if (nowUrl && videoIdOf(nowUrl) !== vid) return 0;
  ladderByTab.set(tabId, {
    pageUrl: "https://www.youtube.com/watch?v=" + vid,
    title: (r && r.title) || "",
    rows,
  });
  persistLadder();
  await notifyTab(tabId, false);
  return rows.length;
}

/* ---------------------------------------------------------------- 大小补全

   用一个只有 2 字节的 Range 请求把资源总量问出来：206 响应的
   Content-Range 一定是 "bytes 0-1/<总量>"。不去读响应体，所以不会真的
   把视频拖下来。 */
const sizeProbing = new Set();

async function probeSize(tabId, item) {
  if (!item || item.size || !item.url || sizeProbing.has(item.url)) return;
  // googlevideo 的 CORS 只放行 youtube.com，SW 里这个 fetch 必然读不到
  // 响应头（要么被拦要么没有 Content-Range），交给桌面端去问。
  if (YT_MEDIA_HOST.test(hostOf(item.url))) {
    probeSizesViaBackend(tabId);
    return;
  }
  sizeProbing.add(item.url);
  try {
    const r = await fetch(item.url, {
      headers: { Range: "bytes=0-1" },
      // googlevideo 的直链和浏览器会话绑定，带上 cookie 才不会被判 403
      credentials: "include",
    });
    let total = 0;
    const m = /\/(\d+)\s*$/.exec(r.headers.get("content-range") || "");
    if (m) total = parseInt(m[1], 10) || 0;
    if (!total && r.status === 200) {
      const cl = parseInt(r.headers.get("content-length") || "0", 10) || 0;
      if (cl > 1) total = cl;
    }
    // 响应体永远不要读进来（可能是几个 GB）
    try {
      if (r.body) r.body.cancel();
    } catch (_) {
      /* ignore */
    }
    if (total > 0) {
      item.size = total;
      persist();
      notifyTab(tabId, false);
    }
  } catch (_) {
    /* 直链过期或不允许跨域 —— 保持「未知」即可 */
  } finally {
    sizeProbing.delete(item.url);
  }
}

/** 从 YouTube 地址里取视频 id（watch?v= / youtu.be/ / shorts/）。 */
function videoIdOf(u) {
  try {
    const x = new URL(u);
    const h = x.hostname.toLowerCase();
    if (h.endsWith("youtu.be")) return x.pathname.split("/").filter(Boolean)[0] || "";
    const m = /\/shorts\/([^/?#]+)/.exec(x.pathname);
    if (m) return m[1];
    return x.searchParams.get("v") || "";
  } catch (_) {
    return "";
  }
}

/** 面板要展示的列表：在有「清晰度阶梯」的页面上用它替换裸直链条目。 */
function viewItems(tabId, currentUrl) {
  const base = (tabId != null && snifferByTab.get(tabId)) || [];
  const lad = tabId != null && ladderByTab.get(tabId);
  if (!lad || !lad.rows || !lad.rows.length) return base;
  const page = lad.pageUrl || "";
  if (!page) return base;
  // YouTube 是软导航（点侧栏推荐不重载页面）：阶梯可能是上一个视频的。
  // 视频 id 对不上就退回原始条目，宁可显示「大小未知」也不报错数字。
  if (currentUrl) {
    const a = videoIdOf(currentUrl);
    const b = videoIdOf(page);
    if (a && b && a !== b) return base;
  }
  const rows = lad.rows.map((r) => ({
    // key 而不是 url 作为身份：同一页的每一档都指向同一个页面地址，
    // 用 url 当键会把它们合成一条（面板里只剩 2160P）。
    key: "yt:" + r.height,
    url: page,
    page_url: page,
    height: Number(r.height) || 0,
    kind: "video",
    size: Number(r.size) || 0,
    // 页面给的 contentLength 有时只是播放器刚拉的一小段，这种情况下的数字是
    // 按时长估算的（面板显示「≈」），随后会被本机解析的真实字节数替换。
    approx: !!r.approx,
    name: (r.label || r.height + "P") + ".mp4",
    from: "ladder",
  }));
  // 同一页里非 YouTube CDN 的媒体（比如站内的 mp4/音频）照样列出来
  const others = base.filter(
    (it) => it && it.url && !YT_MEDIA_HOST.test(hostOf(it.url))
  );
  return rows.concat(others);
}

async function notifyTab(tabId, reset) {
  let url = "";
  try {
    const tab = await chrome.tabs.get(tabId);
    url = (tab && tab.url) || "";
  } catch (_) {
    /* 标签页可能已经关了 */
  }
  const items = viewItems(tabId, url);
  try {
    // reset=true 表示「换了页面，旧结果作废」，content script 收到后要
    // 清空而不是合并，否则新页面会继承上一个页面的媒体。
    await chrome.tabs.sendMessage(tabId, { type: "sniffer-update", items, reset: !!reset });
  } catch (_) {
    /* 页面没有注入 content script（chrome://、应用店、已关闭的标签） */
  }
}

/* ------------------------------------------------------------------ *
 * 站点适配器（sites.js）注入
 *
 * sites.js 必须在页面的 MAIN world 里跑：它要读页面自己的全局变量
 * （B站 __playinfo__ / YouTube ytInitialPlayerResponse），而这些变量在
 * content script 的 isolated world 里读不到。
 *
 * 老做法是在 videobtn.js 里 document.createElement('script') 动态插一段
 * <script src="chrome-extension://.../sites.js">。**B站能过，YouTube 必挂**：
 * YouTube 的 CSP script-src 不含 chrome-extension:，注入直接被浏览器拦掉，
 * 控制台报 "Unsafe attempt to load URL chrome-extension://.../sites.js
 * from frame with URL https://www.youtube/" —— 表现就是 YouTube 上点
 * 「下载此视频」弹不出任何清晰度（而 B站一切正常，极具迷惑性）。
 *
 * chrome.scripting.registerContentScripts 的 world:"MAIN" 是浏览器原生
 * 注入通道，不走页面的 <script> 元素，**页面的 CSP 管不到它**。
 * document_start 执行：videobtn.js（document_idle）起来时桥接早就绪，
 * 不用再等注入。
 *
 * world 字段要 Chrome 102+（manifest 的 minimum_chrome_version 已相应上调）；
 * 老浏览器上这个调用会失败，videobtn.js 会退回 <script src> 注入兜底
 * （B站的 CSP 放行 chrome-extension:，所以老浏览器上 B站仍然可用）。
 *
 * 动态注册的内容脚本由浏览器持久化，SW 被回收/重启都不会丢；但扩展被
 * 重新加载（chrome://extensions 点刷新）时会清空，所以这里在**每次
 * SW 启动**都注册一遍，幂等（同 ID 同定义 = 覆盖写）。
 * ------------------------------------------------------------------ */
const SITES_SCRIPT_ID = "zdm-sites-main";
const SITES_MATCHES = [
  "*://*.bilibili.com/*",
  "*://*.bilivideo.com/*",
  "*://*.youtube.com/*",
  "*://*.youtu.be/*",
];

function registerSitesScript() {
  if (!chrome.scripting || !chrome.scripting.registerContentScripts) return;
  try {
    const p = chrome.scripting.registerContentScripts([
      {
        id: SITES_SCRIPT_ID,
        matches: SITES_MATCHES,
        js: [{ file: "sites.js" }],
        world: "MAIN",
        runAt: "document_start",
        allFrames: false,
      },
    ]);
    // 同 ID 重复注册是覆盖写、不报错；这里只兜「返回了 promise 但被拒」
    // （例如老浏览器不认 world 字段）。
    if (p && typeof p.catch === "function") p.catch(() => {});
  } catch (_) {
    /* 同步抛错同样只是「这条路不通」，由 videobtn.js 兜底 */
  }
}
registerSitesScript();
chrome.runtime.onInstalled.addListener(registerSitesScript);

/* 第二注入通道：content script 桥接迟迟不就绪时，由它现场把 sites.js
 * 打进页面的 MAIN world。
 *
 * 为什么需要它：registerContentScripts 的 world 字段要 Chrome 111+ 才
 * 支持，老一点的浏览器（102~110）上注册会静默失败，YouTube 的 CSP 又
 * 不放行 <script src> 兜底 —— 三条路全断，播放页按钮整个不出现。
 * chrome.scripting.executeScript 的 world:"MAIN" 从 Chrome 95 就有，
 * 这条通道覆盖面宽得多。 */
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "zdm-inject-sites") {
    const tabId = sender.tab && sender.tab.id;
    if (tabId == null || !chrome.scripting || !chrome.scripting.executeScript) {
      sendResponse({ ok: false });
      return false;
    }
    chrome.scripting.executeScript(
      { target: { tabId }, files: ["sites.js"], world: "MAIN" },
      () => {
        const e = chrome.runtime.lastError;
        sendResponse(e ? { ok: false, error: e.message } : { ok: true });
      }
    );
    return true; // 异步应答
  }
});

chrome.webRequest.onResponseStarted.addListener(
  (details) => {
    const item = classifyResponse(details);
    if (!item) return;
    if (rememberMedia(details.tabId, item)) notifyTab(details.tabId);
  },
  { urls: ["http://*/*", "https://*/*"], types: ["main_frame", "sub_frame", "xmlhttprequest", "media", "object"] },
  // 没有这一段，Chrome 根本不会把 Content-Type / Content-Length /
  // Content-Range 放进 details —— 分类只能靠 URL 扩展名兜底，大小全是
  // 「未知」，64KB 噪音过滤也一起失效（YouTube 的点击音效全混进列表）。
  // 实测 B站 CDN 的 206 响应是带着 Content-Range: bytes 0-1023/17828788 的，
  // 只是扩展没收。
  ["responseHeaders"]
);

// 页面跳转后旧列表作废，否则新页面会继承上一个页面的嗅探结果
chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.url) {
    snifferByTab.delete(tabId);
    ladderByTab.delete(tabId); // 换视频了，上一部的清晰度/大小不能留着
    // itag 号码跨视频是复用的（137 永远是 1080P 那类），但**字节数每条视频都不同**。
    // 不跟着清掉，新视频的分段就会被套上上一部的长度，显示一个看起来很像真的错数字。
    lensByTab.delete(tabId);
    backendProbing.delete(tabId); // 新页面 = 新直链，旧的不许再拦着
    ladderTriedAt.delete(tabId); // 换了视频就该重新问一次本机解析
    persist();
    persistLadder();
    persistLens();
    notifyTab(tabId, true);
  }
});
chrome.tabs.onRemoved.addListener((tabId) => {
  snifferByTab.delete(tabId);
  ladderByTab.delete(tabId);
  lensByTab.delete(tabId);
  backendProbing.delete(tabId);
  ladderTriedAt.delete(tabId);
  persistLadder();
  persistLens();
  persist();
});

function getCfg() {
  return new Promise((res) => chrome.storage.sync.get(DEFAULTS, res));
}

function baseUrl(cfg) {
  return (cfg.serverUrl || DEFAULTS.serverUrl).replace(/\/+$/, "");
}

/* ---------------------------------------------------------- YouTube cookie

   为什么必须在这里做：chrome.cookies 只对**扩展自身页面 / service worker**
   开放。content script 跑在 isolated world，`chrome.cookies` 是 undefined，
   调用会抛 TypeError —— 被 catch 吞掉之后表现就是「永远带着空 cookie 去
   解析」，于是 YouTube 每次都回「Sign in to confirm you're not a bot」。
   这个 bug 藏了很久：扩展侧看着逻辑齐全，实际一次都没生效过。 */

/* 单条 cookie → Netscape 一行。
   第 2 列是「是否指定了域」，http.cookiejar 会断言它 == (域名是否以 '.' 开头)，
   写错（比如 .youtube.com + FALSE）yt-dlp 直接报 "invalid Netscape format
   cookies file" 并放弃整次解析 —— 越努力带登录态越必然失败。 */
function netscapeCookieLine(c) {
  let dom = c.domain || "";
  if (!dom) return "";
  const hostOnly = c.hostOnly !== undefined ? !!c.hostOnly : !dom.startsWith(".");
  if (hostOnly && dom.startsWith(".")) dom = dom.slice(1);
  if (!hostOnly && !dom.startsWith(".")) dom = "." + dom;
  return [
    // MozillaCookieJar 用 #HttpOnly_ 前缀标记 HttpOnly 项
    (c.httpOnly ? "#HttpOnly_" : "") + dom,
    dom.startsWith(".") ? "TRUE" : "FALSE",
    c.path || "/",
    c.secure ? "TRUE" : "FALSE",
    c.expirationDate ? String(Math.floor(c.expirationDate)) : "0",
    c.name,
    c.value,
  ].join("\t");
}

/* 需要完整 cookie 的站点。
 *
 *  这些站点的反爬 token / 登录态大多在 HttpOnly 里，content script 拿不到的
 *  document.cookie 不够用：抖音没 cookie 直接报 "Fresh cookies are needed"，
 *  X / Facebook 要登录态也是同理。这些站点改用 chrome.cookies 导出
 *  （严格超集，拿到 HttpOnly 那部分）。
 *  extra 是要一并带上的关联域（YouTube 的 SSO 票据有一部分在 google.com）。
 */
const FULL_COOKIE_SITES = [
  { re: /(^|\.)youtube\.com$|(^|\.)youtu\.be$/, extra: ["google.com"] },
  { re: /(^|\.)douyin\.com$|(^|\.)iesdouyin\.com$/, extra: ["amemv.com"] },
  { re: /(^|\.)kuaishou\.com$|(^|\.)gifshow\.com$/, extra: [] },
  { re: /(^|\.)xiaohongshu\.com$|(^|\.)xhslink\.com$/, extra: [] },
  { re: /(^|\.)twitter\.com$|(^|\.)x\.com$/, extra: ["twitter.com"] },
  {
    re: /(^|\.)facebook\.com$|(^|\.)instagram\.com$|(^|\.)fb\.com$/,
    extra: ["facebook.com"],
  },
  { re: /(^|\.)bilibili\.com$|(^|\.)bilibili\.tv$/, extra: [] },
];

/** 按目标站点导出完整 cookie（Netscape 格式）。不在名单里返回空串。 */
async function siteCookieText(host) {
  const h = (host || "").toLowerCase();
  if (!h) return "";
  const spec = FULL_COOKIE_SITES.find((s) => s.re.test(h));
  if (!spec) return "";
  if (!chrome.cookies || !chrome.cookies.getAll) return "";
  const getAll = (filter) =>
    new Promise((res) => {
      try {
        chrome.cookies.getAll(filter, (cs) => {
          void chrome.runtime.lastError;
          res(cs || []);
        });
      } catch (_) {
        res([]);
      }
    });
  // domain 过滤是精确匹配域名，所以主域也要单独取一份
  // （cookie 记在 .douyin.com 上，而页面是 www.douyin.com）。
  const domains = new Set([h]);
  const parts = h.split(".");
  if (parts.length >= 2) domains.add(parts.slice(-2).join("."));
  spec.extra.forEach((d) => domains.add(d));
  const reqs = [getAll({ url: "https://" + h + "/" })];
  for (const d of domains) reqs.push(getAll({ domain: d }));
  const groups = await Promise.all(reqs);
  const seen = new Set();
  const lines = [];
  for (const g of groups) {
    for (const c of g || []) {
      if (!c || !c.name) continue;
      const line = netscapeCookieLine(c);
      if (!line) continue;
      const key = line.split("\t").slice(0, 3).join("|") + "|" + c.name;
      if (seen.has(key)) continue;
      seen.add(key);
      lines.push(line);
    }
  }
  return lines.length ? "# Netscape HTTP Cookie File\n" + lines.join("\n") + "\n" : "";
}

/** Netscape cookie 文本 → 单行 header 形式（name=value; name2=value2）。
 *
 *  完整导出的 Netscape 文本含 tab 和换行，原样塞进 HTTP 的 Cookie 头会被
 *  直接拒掉（urllib: "Invalid header value b'# Netscape HTTP Cookie
 *  File\\n...'"），于是**探测阶段**就失败，任务报「probe failed」，
 *  一个字节都下不来。下载通道统一走这个转换。
 *
 *  解析通道（zdm-youtube-resolve）仍传 Netscape 原文 —— 那里要的是域信息，
 *  Netscape 自带 .douyin.com 这样的域，旧版后端也认。
 */
function cookieToHeader(text) {
  if (!text) return "";
  if (text.indexOf("\t") < 0) return text; // 已经是 header 形式
  const out = [];
  const seen = new Set();
  for (const line of text.split("\n")) {
    if (!line) continue;
    if (line.startsWith("#") && !line.startsWith("#HttpOnly_")) continue;
    const body = line.startsWith("#HttpOnly_")
      ? line.slice("#HttpOnly_".length)
      : line;
    const p = body.split("\t");
    if (p.length < 7) continue;
    const name = p[5].trim();
    if (!name || seen.has(name)) continue;
    seen.add(name);
    out.push(name + "=" + p[6]);
  }
  return out.join("; ");
}

async function youtubeCookieText() {
  return siteCookieText("youtube.com");
}

async function api(path, body, method = "POST") {
  const cfg = await getCfg();
  const doFetch = () =>
    fetch(baseUrl(cfg) + "/api/" + path.replace(/^\/+/, ""), {
      method,
      headers: {
        "Content-Type": "application/json",
        // Lets the client whitelist this extension id for native messaging even
        // if it was reloaded under a different id.
        "X-ZDM-Extension": chrome.runtime.id,
      },
      body: body ? JSON.stringify(body) : undefined,
    });
  let r;
  try {
    r = await doFetch();
  } catch (e) {
    // 服务重启（升级）的瞬间连接池里可能全是死 socket：歇 400ms 重试
    // 一次，面板就不用等用户重开浏览器才恢复。
    await new Promise((res) => setTimeout(res, 400));
    r = await doFetch();
  }
  return r.json();
}

/* ------------------------------------------------------------- 存活心跳

   桌面端「浏览器扩展」对话框要回答「扩展装上了没有」。不读注册表、
   不翻浏览器配置目录（每个浏览器路径都不同，还不一定读得到），反过来
   听：扩展每次被唤醒都向本机服务报一个到（GET /api/extension/ping，
   无鉴权，服务只记录「来过」的时间）。

   service worker 空闲 30 秒就被回收，所以三层保险：
   - SW 每次被唤醒（嗅探/推送/alarms 都会唤醒它）：顶层代码直接 ping；
   - 浏览器启动 / 扩展安装更新：onStartup / onInstalled 各 ping 一次，
     并创建 30 分钟周期的 chrome.alarms；
   - alarms 到点会唤醒 SW → 又回到第一层。
   浏览器整个关掉后心跳自然停格 —— 桌面端会如实显示「最近一次活跃」，
   而不是误报「未安装」。 */

async function pingPresence() {
  try {
    const cfg = await getCfg();
    const r = await fetch(baseUrl(cfg) + "/api/extension/ping", {
      method: "GET",
      headers: { "X-ZDM-Extension": chrome.runtime.id },
    });
    const j = await r.json().catch(() => null);
    if (j && j.version) {
      // 桌面端升级后自动 reload 扩展，从磁盘加载新的 background/content
      // scripts；不用用户关闭并重开浏览器。
      const key = "zdmAppVersion";
      const prev = (await chrome.storage.local.get(key))[key] || "";
      if (prev && prev !== j.version) {
        await chrome.storage.local.set({ [key]: j.version });
        chrome.runtime.reload();
        return;
      }
      if (!prev) await chrome.storage.local.set({ [key]: j.version });
    }
  } catch (_) {
    /* 服务没开：静默。桌面端自然显示「未检测到」。 */
    // 升级/重启服务后的那几十秒里 ping 会失败；10 秒后再试一次，主界面的
    // 「扩展已连接」就能很快变绿，不需要用户重开浏览器。
    setTimeout(pingPresence, 10000);
  }
}

try {
  chrome.alarms.create("zdm-presence", { periodInMinutes: 1 });
  chrome.alarms.onAlarm.addListener((a) => {
    if (a && a.name === "zdm-presence") pingPresence();
  });
} catch (_) {
  /* 老 Chrome 没有 alarms 就少一层保险，不影响其它功能。 */
}
try {
  chrome.runtime.onInstalled.addListener(pingPresence);
  chrome.runtime.onStartup.addListener(pingPresence);
} catch (_) {}
pingPresence();

function notify(title, message) {
  getCfg().then((cfg) => {
    if (!cfg.notify) return;
    chrome.notifications.create({
      type: "basic",
      iconUrl: "icons/icon48.png",
      title,
      message,
    });
  });
}

/* ------------------------------------------------------------------ *
 * 唤醒 ZDM
 *
 * 首选「原生消息」(native messaging)：chrome.runtime.connectNative 会让
 * Chrome 直接按注册表里的 host 清单启动 Zdm.exe，不需要用户确认。
 *
 * 为什么不用 zdm:// 协议作为主路径：Chrome 只在「有用户手势」的页面上才
 * 允许跳转外部协议，而扩展自己开的隐藏标签页没有手势，跳转会被静默拦掉
 * —— 之前点了下载 ZDM 毫无反应就是这个原因。协议只作为兜底。
 * ------------------------------------------------------------------ */
const HOST_NAME = "com.zdm.launcher";

function launchViaNative(req) {
  return new Promise((resolve) => {
    let port;
    try {
      port = chrome.runtime.connectNative(HOST_NAME);
    } catch (e) {
      resolve(false);
      return;
    }

    let settled = false;
    const finish = (ok) => {
      if (settled) return;
      settled = true;
      try {
        port.disconnect();
      } catch (_) {
        /* already gone */
      }
      resolve(ok);
    };

    port.onMessage.addListener((m) => {
      if (m && m.ok) chrome.storage.local.remove("lastNativeError");
      finish(!!(m && m.ok));
    });
    port.onDisconnect.addListener(() => {
      // Reading lastError is what suppresses Chrome's console warning — and it
      // is the only place the real reason ("host not found", "forbidden")
      // surfaces, so keep it for the popup.
      const err = chrome.runtime.lastError;
      if (err && err.message) {
        chrome.storage.local.set({ lastNativeError: { msg: err.message, at: Date.now() } });
      }
      finish(false);
    });

    try {
      port.postMessage({
        url: req.url,
        filename: req.filename || undefined,
        referer: req.referer || undefined,
        cookie: req.cookie || undefined,
        connections: req.connections || undefined,
        dir: req.dir || undefined,
        source: req.source || undefined,
        kind: req.kind || undefined,
        audio_url: req.audio_url || undefined,
        yt_page: req.yt_page || undefined,
        yt_height: req.yt_height || undefined,
      });
    } catch (e) {
      finish(false);
    }

    // A host that never answers must not stall the handover forever. The host
    // itself may legitimately take ~20s to bring a cold client up, so give it
    // more than the old 5s (which expired on every cold start and always fell
    // through to the zdm:// escape hatch).
    setTimeout(() => finish(false), 15000);
  });
}

function buildProtocolUrl(req) {
  const q = new URLSearchParams();
  q.set("url", req.url);
  if (req.filename) q.set("filename", req.filename);
  if (req.referer) q.set("referer", req.referer);
  if (req.connections) q.set("connections", String(req.connections));
  if (req.dir) q.set("dir", req.dir);
  // 来源标记要能活着走到队列里：协议兜底是冷启动路径，客户端此时
  // 只认启动参数，丢掉 source 会让「浏览器接管」显示成「手动添加」。
  if (req.source) q.set("source", req.source);
  if (req.kind) q.set("kind", req.kind);
  if (req.audio_url) q.set("audio_url", req.audio_url);
  if (req.yt_page) q.set("yt_page", req.yt_page);
  if (req.yt_height) q.set("yt_height", String(req.yt_height));
  // Cookie 也要活过协议跳转：需要登录的视频站，没有它就是 403。
  if (req.cookie) q.set("cookie", req.cookie);
  return "zdm://add?" + q.toString();
}

async function launchZdm(req) {
  if (await launchViaNative(req)) return true;

  // 兜底：没有注册原生消息（或用户用的是不支持它的浏览器）时，
  // 开一个隐藏的扩展页，由它把地址栏跳到 zdm://。
  const target = req && req.url ? buildProtocolUrl(req) : "zdm://open";
  const launcher =
    chrome.runtime.getURL("launch.html") + "?u=" + encodeURIComponent(target);
  try {
    const tab = await chrome.tabs.create({ url: launcher, active: false });
    if (tab && tab.id != null) {
      setTimeout(() => {
        chrome.tabs.remove(tab.id).catch(() => {});
      }, 5000);
    }
    return true;
  } catch (e) {
    console.warn("[ZDM] 无法唤醒客户端:", e);
    return false;
  }
}

/* 轮询直到本机 API 有响应，说明 ZDM 已经起来了。 */
async function waitForServer(cfg, timeoutMs) {
  const base = baseUrl(cfg);
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const r = await fetch(base + "/api/state", { cache: "no-store" });
      if (r.ok) return true;
    } catch (_) {
      /* 还没起来，继续等 */
    }
    await new Promise((r) => setTimeout(r, 600));
  }
  return false;
}

/* 确认任务真的进了 ZDM 的队列。协议启动有可能失败（比如注册表被清理、
 * 首次运行被拦截），这时不能盲目取消浏览器自己的下载。
 *
 * ``since`` 只认这次交接之后新出现的任务：ZDM 会把最多 200 条历史完成任务
 * 恢复进列表，只比 URL 会把"上周下过的同一个地址"误判成本次已入列 ——
 * 浏览器下载被取消，新任务却从未创建，用户静默丢东西。 */
async function taskExists(url, since) {
  try {
    const s = await api("state", null, "GET");
    const items = (s && s.tasks) || [];
    return items.some(
      (t) =>
        t.url === url &&
        (since == null || (t.created_at || 0) >= since)
    );
  } catch (_) {
    return false;
  }
}

/**
 * 把一次下载交给 ZDM，必要时先把客户端拉起来。
 * 返回 true 表示 ZDM 已经接手，浏览器这边可以取消了。
 */
async function handOff(req) {
  const cfg = await getCfg();
  const since = Date.now() / 1000;

  const headers = {};
  if (req.referer) headers.Referer = req.referer;
  if (req.cookie) headers.Cookie = req.cookie;

  const post = () =>
    api("tasks", {
      url: req.url,
      filename: req.filename,
      connections: req.connections || cfg.connections,
      dir: req.dir || cfg.directory || undefined,
      headers,
      source: req.source || "browser",
      kind: req.kind || undefined,
      // DASH 的视频轨需要配套音频，后端会合并
      audio_url: req.audio_url || undefined,
      // YouTube：给了页面地址 + 高度就让后端用 yt-dlp 下（直链不能分块取）
      yt_page: req.yt_page || undefined,
      yt_height: req.yt_height || undefined,
    });

  // 1. 客户端已在运行：直接 POST。
  let answered = false;
  try {
    const res = await post();
    answered = true;
    if (res && res.ok) return true;
  } catch (_) {
    /* 连不上：服务没起，走下面的唤醒流程 */
  }

  // 服务**答了**但拒收（链接不认识、参数不合法……）：再唤醒客户端也不会有
  // 不同结果，只会在 Windows 上无端拉起一次原生消息宿主（一个黑窗口闪过）。
  if (answered) return false;

  // 2. 没在运行：唤醒它。原生消息可能因为「扩展还没重新加载」而失败，
  //    这时立刻降级到协议跳转。唤醒是否成功不在这里判断 —— 以 HTTP
  //    服务能否连上为准，否则一次误报就会把下载白白放回浏览器。
  await launchZdm(req);

  // 3. 等它起来。
  if (!(await waitForServer(cfg, WAKE_TIMEOUT_MS))) {
    notify("ZDM 没有启动", "请在开始菜单里打开 ZDM 下载管理器，或在设置中重新注册浏览器接管。");
    return false;
  }

  // 4. 正常情况下客户端已经从启动参数里取走了这条下载；只有确认队列里
  //    没有 *本次* 的新任务时才补一次 POST。早先版本在这里无条件补 POST，
  //    而 Windows 原生宿主是"先回 ok、任务随后才入列"，于是同一个地址
  //    会被加两遍。
  if (await taskExists(req.url, since)) return true;

  try {
    const res = await post();
    return !!(res && res.ok);
  } catch (_) {
    return false;
  }
}

/* ---- take over browser downloads ---- */
chrome.downloads.onCreated.addListener(async (item) => {
  const cfg = await getCfg();
  if (!cfg.intercept) return;
  const url = item.url || "";
  if (!/^https?:\/\//i.test(url)) return; // blob:, data:, file:, chrome-extension:
  if (item.byExtensionId === chrome.runtime.id) return;

  const req = {
    url,
    filename: item.filename ? item.filename.split(/[\\/]/).pop() : undefined,
    referer: item.referrer || undefined,
    // The wake-up path must carry the user's options too, otherwise a cold
    // start silently falls back to the client's defaults (8 connections,
    // default directory) while a warm POST honoured them.
    connections: cfg.connections,
    dir: cfg.directory || undefined,
    source: "browser",
  };

  // 先把浏览器这边的下载按住：唤醒 ZDM 要几秒，不按的话文件可能已经
  // 下完一半，白跑流量。这里用 pause 而不是 cancel —— 交接失败时必须
  // 能恢复，cancel 是不可逆的。
  let held = false;
  try {
    await chrome.downloads.pause(item.id);
    held = true;
  } catch (e) {
    /* 还没开始或已经结束，无需处理 */
  }

  let ok = false;
  try {
    ok = await handOff(req);
  } catch (e) {
    console.warn("[ZDM] handoff failed:", e);
  }

  if (!ok) {
    if (held) await chrome.downloads.resume(item.id).catch(() => {});
    return; // 交给浏览器自己下，别把用户的东西弄丢
  }

  try {
    await chrome.downloads.cancel(item.id);
    await chrome.downloads.erase({ id: item.id });
  } catch (e) {
    /* 已经结束 */
  }
  notify("已交给 ZDM", item.filename || url);
});

/* ---- context menus ---- */
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: "zdm-download",
      title: "使用 ZDM 下载",
      contexts: ["link", "video", "audio", "image"],
    });
    chrome.contextMenus.create({
      id: "zdm-page",
      title: "使用 ZDM 下载本页资源",
      contexts: ["page"],
    });
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const url = info.linkUrl || info.srcUrl || info.pageUrl;
  if (!url) return;
  const ok = await handOff({
    url,
    referer: tab && tab.url ? tab.url : undefined,
    source: "browser",
  });
  notify(ok ? "已交给 ZDM" : "ZDM 添加失败", ok ? url : "请在设置里重新注册浏览器接管");
});

/* ---- popup helpers ---- */
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "state") {
    api("state", null, "GET").then(sendResponse).catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (msg.type === "task-action") {
    api(`tasks/${msg.id}/${msg.action}`, {}).then(sendResponse).catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (msg.type === "wake") {
    launchZdm({}).then((ok) => sendResponse({ ok }));
    return true;
  }
  if (msg.type === "open-ui") {
    getCfg().then((cfg) => {
      chrome.tabs.create({ url: baseUrl(cfg) + "/" });
    });
    return true;
  }
  // YouTube 清晰度解析：转给本机 ZDM，由它用 yt-dlp 把加密的直链解开
  // （ghostdownload 同款思路：浏览器里解 signatureCipher 太脆，交给桌面端）。
  if (msg.type === "zdm-youtube-resolve") {
    (async () => {
      /* cookie 要按**目标页面**的站点导，不能固定用 YouTube 的。
         以前这里硬编码 youtubeCookieText()，于是抖音 / 快手 / X 解析时
         带过去的是 youtube.com 的 cookie —— 目标站点拿不到自己的登录态，
         yt-dlp 直接报 "Fresh cookies are needed"。 */
      let cookie = "";
      try {
        cookie = await siteCookieText(hostOf(msg.url || "") || "");
      } catch (_) {
        cookie = "";
      }
      // 老版本 content script 会自己带 cookie 过来，仍然兼容
      if (!cookie && msg.cookie) cookie = msg.cookie;
      const r = await api("youtube/resolve", {
        url: msg.url,
        cookie,
        cookie_count: cookie ? cookie.split("\n").length - 2 : 0,
      });
      return r || { ok: false, error: "empty" };
    })()
      .then((r) => sendResponse(r))
      .catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  }
  // 页面交出「清晰度 → 大小」阶梯：YouTube 面板的数据源。
  // SABR 之后那条 /videoplayback 是流式 POST，没有长度可报，因此面板改成
  // 按清晰度列（每档都有确切字节数），推送时带 page_url + height 走后端。
  if (msg.type === "sniffer-ladder") {
    const tabId = sender.tab && sender.tab.id;
    ensureRestored().then(() => {
      const rows = (msg.rows || []).filter(
        (r) => r && Number(r.height) > 0 && Number(r.size) > 0
      );
      if (!tabId || !rows.length) {
        sendResponse({ ok: false });
        return;
      }
      ladderByTab.set(tabId, {
        pageUrl: msg.pageUrl || (sender.tab && sender.tab.url) || "",
        title: msg.title || "",
        rows,
      });
      persistLadder();
      sendResponse({ ok: true, rows: rows.length });
      notifyTab(tabId, false);
    });
    return true;
  }
  // 页面把「itag → 字节数」表交过来，用来补掉列表里的「大小未知」。
  // YouTube 的 videoplayback 分包响应既没有 Content-Length 也没有
  // Content-Range，URL 里也常常不带 clen —— 发 Range 请求去问又会被 403。
  // 而 adaptiveFormats[].contentLength 不管直链有没有加密都在，直接用它最稳。
  if (msg.type === "sniffer-sizes") {
    const tabId = sender.tab && sender.tab.id;
    ensureRestored().then(() => {
      const lens = msg.sizes || {};
      // 记住这张表：之后才抓到的分段（点击播放、切清晰度都会产生新请求）
      // 在 rememberMedia 里直接用，不用再等页面同步一次。
      lensByTab.set(tabId, Object.assign(lensByTab.get(tabId) || {}, lens));
      persistLens();
      const changed = applyLens(tabId, lens);
      if (changed) persist();
      sendResponse({ ok: true, changed });
      if (changed) notifyTab(tabId, false);
      // itag 表只覆盖「URL 里还带 itag」的老式直链；SABR 的链接对不上号，
      // 剩下的交给桌面端按 URL 精确问一遍。
      probeSizesViaBackend(tabId);
    });
    return true;
  }
  /* 扫图片得到的条目是 content script 本地的（from: "dom-img"），从来没进过
     background 的嗅探列表，所以大小探测那条路（probeSize /
     probeSizesViaBackend）根本不会跑到它们 —— 列表里一律显示「大小未知」。
     这里让 content script 把图片 URL 交上来，由桌面端问真实体积
     （桌面端不受 CORS 限制，扩展侧 fetch 读不到 content-range）。 */
  if (msg.type === "sniffer-probe-sizes") {
    (async () => {
      const urls = (msg.urls || [])
        .filter((u) => /^https?:\/\//i.test(u))
        .slice(0, 12);
      if (!urls.length) {
        sendResponse({ ok: true, sizes: {} });
        return;
      }
      let referer = "";
      try {
        const tab = await chrome.tabs.get(sender.tab && sender.tab.id);
        referer = (tab && tab.url) || "";
      } catch (_) {
        /* 标签页关了也没关系 */
      }
      let r = null;
      try {
        r = await api("probe-size", { urls, referer });
      } catch (_) {
        r = null;
      }
      sendResponse({ ok: true, sizes: (r && r.sizes) || {} });
    })();
    return true;
  }
  // 媒体嗅探：content script 来要当前标签页已发现的媒体。
  // 先等 storage.session 恢复完再答 —— SW 刚被唤醒时恢复可能还没完成，
  // 直接读 Map 会返回空，用户就看到「嗅探到 0 个」。
  if (msg.type === "sniffer-list") {
    const tabId = sender.tab && sender.tab.id;
    ensureRestored().then(() => {
      // 打开面板这一刻再补一次大小：条目可能是页面同步之后才抓到的。
      const n = lensByTab.get(tabId);
      if (n && applyLens(tabId, n)) persist();
      const tabUrl = (sender.tab && sender.tab.url) || "";
      sendResponse({
        ok: true,
        items: viewItems(tabId, tabUrl),
      });
      // 先把面板放出来（不让人干等），再异步把「大小未知」补上，
      // 补到之后 notifyTab 会让面板自己刷新。
      probeSizesViaBackend(tabId);
      // YouTube：页面阶梯没上来（软导航换了视频 / SABR-only / 脚本还没跑）
      // 就直接问桌面端要阶梯，答完重绘面板。
      if (/(^|\.)youtube\.com$|(^|\.)youtu\.be$/i.test(hostOf(tabUrl))) {
        ensureLadderViaBackend(tabId, tabUrl);
      }
    });
    return true;
  }
  // 一键推送：走的是和「接管浏览器下载」完全相同的 handOff()，
  // 因此冷启动唤醒、原生消息、协议兜底的行为三者一致。
  if (msg.type === "sniffer-push") {
    const item = msg.item || {};
    const tabId = sender.tab && sender.tab.id;
    (async () => {
      // 推送的若是视频轨：优先用调用方给的音频，否则在嗅探列表里自动配对。
      // DASH 音频是另一条请求，不配对就必然「下完没声音」。
      // 只对视频轨配对 —— 图片/音频条目没有「配套音轨」这个概念。
      let audioUrl = item.audio_url || "";
      let url = item.url;
      if (YT_MEDIA_HOST.test(hostOf(url))) url = stripRange(url);
      if (!audioUrl && item.kind === "video") {
        const sib = audioSiblingFor(tabId, item.url);
        if (sib) {
          audioUrl = YT_MEDIA_HOST.test(hostOf(sib.url)) ? stripRange(sib.url) : sib.url;
        }
      } else if (audioUrl && YT_MEDIA_HOST.test(hostOf(audioUrl))) {
        audioUrl = stripRange(audioUrl);
      }

      /* 登录态几乎都在 HttpOnly 里，content script 传来的 document.cookie
         只有非 HttpOnly 的那一半 —— **非空但残缺**。以前只在 `!cookie` 时
         才换完整导出，残缺版一进来就把完整导出挡住了，yt-dlp 拿着半个
         cookiejar 照样报人机验证（失败任务里存的 "APISID=...; SAPISID=..."
         就是铁证）。chrome.cookies 的导出是严格超集，一律优先用它。

         名单见 FULL_COOKIE_SITES：YouTube 之外，抖音 / 快手 / 小红书 /
         X / Facebook 也一样 —— 抖音没 cookie 直接报 "Fresh cookies are
         needed"，而且这里以前**只**给 YouTube 导，别的站点带的都是
         youtube.com 的 cookie。 */
      let cookie = item.cookie || "";
      const page = item.page_url || item.referer || "";
      const host = hostOf(page) || hostOf(url);
      let full = "";
      try {
        full = await siteCookieText(host);
      } catch (_) {
        full = "";
      }
      if (full) cookie = full;

      const ok = await handOff({
        url,
        filename: item.filename,
        referer: item.referer,
        // 压成单行 header 形式：Netscape 原文会让普通下载通道的探测
        // 直接抛 "Invalid header value"（旧版客户端没有这层兜底）。
        cookie: cookieToHeader(cookie) || undefined,
        source: item.source || "sniffer",
        kind: item.kind || "video",
        // DASH 视频轨的配套音频轨，后端下载后用 ffmpeg 合并
        audio_url: audioUrl || undefined,
        // YouTube：页面地址 + 高度 → 后端走 yt-dlp 通道
        yt_page: item.page_url || undefined,
        yt_height: item.height || undefined,
      });
      sendResponse({ ok });
      if (ok) notify("已推送给 ZDM", item.filename || item.url);
    })().catch(() => sendResponse({ ok: false }));
    return true;
  }
});
