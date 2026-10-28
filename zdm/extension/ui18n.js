/* 界面语言（v1.18.0 英文界面）
 *
 * 设计与网页版 zdm/web/index.html 里那一套完全相同：在 DOM 这一层做替换，
 * 而不是把每处文案改成 t('key')。这些界面（嗅探面板、播放器按钮、清晰度菜单）
 * 都是拼 innerHTML 拼出来的，改造成 key 化要动四个文件里上百处渲染，漏一处
 * 就是半中半英；而在 DOM 层做一次，动态刷新的部分连 MutationObserver 一起
 * 覆盖。查不到的词条原样保留中文 —— 宁可看到中文，也不要看到编出来的错译。
 *
 * 两份文案表为什么不合并：网页版是 http 页面、这里是扩展自己的运行时，
 * 互相加载不了对方的文件（页面 CSP 也不允许从 chrome-extension: 取脚本）。
 * 表里各写一份，是这套架构的固有代价，改语言时两处都要过。
 */
(function (global) {
  "use strict";

  const T_EXACT = {
    // ---- 扩展自己的页面（popup / options / launch）
    "ZDM · 下载管理器": "ZDM · Download Manager",
    "ZDM 下载管理器": "ZDM Download Manager",
    "ZDM 设置": "ZDM settings",
    "接管浏览器下载": "Take over browser downloads",
    "自动接管浏览器下载": "Take over browser downloads automatically",
    "ZDM 未运行": "ZDM is not running",
    "启动 ZDM": "Start ZDM",
    "正在启动…": "Starting…",
    "打开管理界面": "Open the manager",
    "更新扩展": "Update extension",
    "重新加载本扩展，使磁盘上的新版生效（Windows 上 ZDM 升级后点这个）": "Reload this extension so the new files on disk take effect (click this after ZDM updates on Windows)",
    "暂无进行中的任务": "Nothing is downloading right now",
    "未连接": "Not connected",
    "端口": "Port",
    "与本机 ZDM 下载服务通信（默认 ": "Talks to the ZDM download service on this machine (default ",
    // 上面那句被 <code> 切成两段，收尾的全角括号是独立文本节点
    "）": ")",
    "服务地址": "Service address",
    "保存目录（留空用服务端默认下载目录）": "Save folder (blank = the server's download folder)",
    "/home/你的用户名/Downloads": "/home/you/Downloads",
    "每个任务的连接数": "Connections per task",
    "显示通知": "Show notifications",
    // 贴边收纳开关（options）
    "网页里的嗅探按钮贴边后自动收纳": "Dock the in-page sniffer button against the screen edge",
    "把「嗅探」按钮拖到屏幕左边或右边，松手会吸到边上；一会儿不碰它就收成一颗半透明小圆点（有媒体时带个绿点），鼠标移上去或点一下就展开。关掉这里就是自由摆放，不吸边也不收起。":
      "Drag the “Sniff” button to the left or right edge and it snaps there; after a moment it shrinks into a translucent dot (green when media was found). Hover or click to expand it again. Turn this off for free placement — no snapping, no shrinking.",
    "保存": "Save",
    "测试连接": "Test connection",
    "已保存": "Saved",
    "连接成功 ✓": "Connection OK ✓",
    "连接失败：ZDM 未运行（点浏览器里的下载会自动唤醒它，也可手动打开 ZDM 一次）": "Connection failed: ZDM is not running (clicking a download in the browser wakes it automatically, or start ZDM once by hand)",
    "界面语言": "Interface language",
    "界面语言的默认值跟随浏览器；改这里只影响扩展的界面，不影响网页本身。": "Defaults to your browser language. This only changes the extension's own UI, never the page you are on.",
    "自动 Auto": "Auto",
    // ---- 唤醒中间页
    "正在唤醒 ZDM 下载管理器…": "Waking up ZDM…",
    "如果没有反应，请从开始菜单手动打开一次 ZDM。": "If nothing happens, open ZDM once from the Start menu.",
    // ---- 嗅探面板
    "还没有嗅探到媒体": "No media sniffed yet",
    "播放或刷新页面后会自动出现": "Play the media, or reload the page, and it shows up here",
    "图库站可以点右上角「扫图片」": "On image sites, use “Scan images” at the top right",
    "ZDM 嗅探到的媒体": "Media found by ZDM",
    "ZDM 嗅探（暂无媒体）": "ZDM sniffer (no media yet)",
    // 按钮本体那两个字（<span>嗅探</span>）以前表里没有，英文界面下会露出中文
    "嗅探": "Sniff",
    "检测到媒体": "Media detected",
    "把嗅探按钮移回默认位置（右下角）": "Move the sniffer button back to its default place (bottom right)",
    "暂无媒体": "No media yet",
    "媒体": "Media",
    "图片": "Images",
    "视频": "Video",
    "音频": "Audio",
    "HLS 流": "HLS stream",
    "DASH 流": "DASH stream",
    "播放列表分片": "Playlist segment",
    "音频流（HLS）": "Audio stream (HLS)",
    "扫图片": "Scan images",
    "扫描页面里的大图（图库/壁纸/漫画站）": "Scan this page for large images (gallery / wallpaper / comic sites)",
    // 面板底部那句是纯静态文案，之前没进表，英文界面下整句是中文
    "HLS / DASH 整条推送，后端自动取分片并合并": "HLS / DASH is sent as a whole — the backend fetches and merges the segments",
    "图片 · 本页扫描": "Images · scanned from this page",
    "推送": "Send",
    "推整条": "Send the whole stream",
    "下载所选": "Download selected",
    "全选": "Select all",
    "取消全选": "Select none",
    "勾选后可批量下载": "Tick the box to download several at once",
    "关闭": "Close",
    "重试": "Retry",
    "失败": "Failed",
    "已推送": "Sent",
    "大小未知": "size unknown",
    "无新图片": "No new images",
    "这是播放列表的一个分片，只包含几秒钟；点推送会自动改推整条流，合并成完整视频": "This is one segment of a playlist, only a few seconds long. Sending it switches to the whole stream and merges it into a full video",
    "哔哩": "Bilibili",
    "抖音": "Douyin",
    "快手": "Kuaishou",
    "微博": "Weibo",
    "小红书": "Xiaohongshu",
    "知乎": "Zhihu",
    "虎牙": "Huya",
    "斗鱼": "Douyu",
    // ---- 通用播放器按钮
    "ZDM 下载此视频": "ZDM · Download this video",
    "下载此视频": "Download this video",
    "下载此音频": "Download this audio",
    "下载音频": "Download audio",
    "直接下载当前视频": "Download this video directly",
    "直接下载此音频": "Download this audio directly",
    "正在解析页面…": "Parsing the page…",
    "没有可用格式": "No formats available",
    "可用": "available",
    "推送失败": "Send failed",
    "推送失败，请确认 ZDM 正在运行": "Could not send — make sure ZDM is running",
    "已推送给 ZDM": "Sent to ZDM",
    "已推送给 ZDM：": "Sent to ZDM: ",
    "解析失败：": "Parsing failed: ",
    "已推送（HLS 流，ffmpeg 合并中）": "Sent (HLS stream, ffmpeg is merging)",
    "已推送（音频流，ffmpeg 合并中）": "Sent (audio stream, ffmpeg is merging)",
    "已推送（若没声音，是站点把音画分开了）": "Sent (no sound? this site splits video and audio)",
    "已推送，音视频将合流": "Sent — video and audio will be merged",
    "已推送给 ZDM：后端会取分片并合并成 mp4": "Sent to ZDM: it will fetch the segments and merge them into mp4",
    "已推送给 ZDM：音视频两轨都会拉取并合并成 mp4": "Sent to ZDM: both tracks are fetched and merged into mp4",
    "已推送给 ZDM：音频流，后端会取分片并封装为 m4a": "Sent to ZDM: audio stream, it fetches the segments and packages them as m4a",
    "这一页还没嗅到视频直链，播放几秒再试": "No direct video link sniffed on this page yet — play a few seconds and try again",
    "这一页还没嗅到播放列表或视频轨。播放几秒再试": "No playlist or video track sniffed on this page yet. Play a few seconds and try again",
    "这一页抓到的都是播放列表的分片（推下去只有几秒），请用上面的「流媒体」": "Everything on this page is a playlist segment (a few seconds each) — use “Streaming” above",
    "这一页没有播放列表（该站点给的是直链），请用下面的「下载嗅探到的视频轨」": "This page has no playlist (the site hands out direct links) — use “Download the sniffed video track” below",
    "这个播放器用加密串流（MSE），拿不到直链，请用上面的「流媒体」或「解析此页面」": "This player uses encrypted streaming (MSE), so there is no direct link — use “Streaming” or “Parse this page” above",
    "该站点需要登录态：先在浏览器登录这个站点再试，或改用上面的「流媒体」": "This site needs a login: sign in in the browser first, or use “Streaming” above",
    "还没嗅到配套音频轨，先播放几秒再试，或用「解析此页面」": "No matching audio track sniffed yet — play a few seconds and try again, or use “Parse this page”",
    "yt-dlp 不认识这个页面（首页/信息流通常不行，要具体内容页）。改用上面的「流媒体」或嗅探面板里的 HLS 条目": "yt-dlp does not know this page (home feeds usually fail — it needs a content page). Use “Streaming” above or an HLS entry in the sniffer",
    // ---- B 站 / YouTube 清晰度按钮
    "YouTube 视频": "YouTube video",
    "B站视频": "Bilibili video",
    "选择清晰度": "Choose quality",
    "切换清晰度中…": "Switching quality…",
    "正在读取清晰度…": "Reading the quality list…",
    "没能识别这个视频。": "Could not identify this video.",
    "推送中…": "Sending…",
    "推送失败:": "Send failed:",
    "推送失败，点这里重试": "Send failed — click here to retry",
    "没拿到直链，稍后再试": "No direct link came back, try again shortly",
    "改用本机解析…": "Falling back to local parsing…",
    "已用本机解析补齐": "Completed by local parsing",
    "每档都带文件大小，点击直接推送。": "Every entry shows its size; click one to send it.",
    "只拿到 360P": "Only 360P came through",
    "只拿到当前正在播放的清晰度": "Only the quality currently playing",
    "清晰度取自播放器本身": "The quality list comes from the player itself",
    "列表马上会补上本机解析的结果（含每档大小）。": "Local results (with the size of each) appear in this list shortly.",
    "本机解析被 YouTube 拦了，只能退回页面内解析。": "YouTube blocked local parsing, so it falls back to in-page parsing.",
    "本机解析被 YouTube 拦了，这里给的是播放器自己正在用的直链。": "YouTube blocked local parsing — these are the links the player itself is using.",
    "YouTube 要求人机验证": "YouTube wants a human check",
    "这个视频的清晰度还读不到": "The qualities of this video are not readable yet",
    "请先在浏览器里登录 YouTube 账号，然后刷新本页、": "Sign in to YouTube in this browser first, then reload the page and",
    "再点一次「下载此视频」。": "click “Download this video” again.",
    "登录 YouTube 后刷新本页再点一次，可拿到全部清晰度。": "Sign in to YouTube, reload this page and click again to get every quality.",
    "播放器可能还在加载，或该视频需要登录 / 付费。": "The player may still be loading, or this video needs a login / payment.",
    "稍等几秒再点一次「下载此视频」试试。": "Wait a few seconds and click “Download this video” again.",
    "点某一档如果播放器没在拉那一路流，会自动改用本机解析的直链。": "If the player is not pulling the quality you pick, the local link is used automatically.",
    "桥接超时，以下载按钮（无清晰度）模式运行:": "The bridge timed out — running as a plain download button (no qualities):",
    "站点适配器无法注入，播放按钮将以无清晰度模式运行": "The site adapter could not be injected; the button works without quality choices",
    "sites.js 兜底注入失败（该站点 CSP 可能不放行 chrome-extension:）": "The sites.js fallback could not be injected (this site's CSP probably blocks chrome-extension:)",
    "注入异常:": "Injection error:",
    "yt-dlp 解析失败:": "yt-dlp failed to parse:",
    "yt-dlp 解析异常:": "yt-dlp raised:",
    "不是支持的站点": "Not a supported site",
    "视频+音频": "video + audio",
    "默认": "Default",
    "1080P 60帧": "1080P 60fps",
    "1080P 高清": "1080P HD",
    "1080P 高码率": "1080P high bitrate",
    "720P 高清": "720P HD",
    "480P 流畅": "480P",
    "360P 清晰": "360P",
    "240P 极速": "240P",
    "4K 超清": "4K",
    "8K 超高清": "8K",
    "HDR 真彩": "HDR",
    "杜比视界": "Dolby Vision",
    // ---- 后台页（通知与右键菜单）
    "使用 ZDM 下载": "Download with ZDM",
    "使用 ZDM 下载本页资源": "Download this page's media with ZDM",
    "已交给 ZDM": "Handed to ZDM",
    "ZDM 添加失败": "ZDM could not add the task",
    "ZDM 没有启动": "ZDM is not running",
    "请在开始菜单里打开 ZDM 下载管理器，或在设置中重新注册浏览器接管。": "Open ZDM from the Start menu, or re-register browser takeover in settings.",
    "请在设置里重新注册浏览器接管": "Re-register browser takeover in settings",
    "[ZDM] 无法唤醒客户端:": "[ZDM] could not wake the client:",
  };

  const T_RULES = [
    [/ZDM 嗅探到 (\d+) 个媒体/g, "ZDM sniffed $1 media"],
    [/检测到 (\d+) 个媒体/g, "Detected $1 media"],
    [/下载所选 (\d+)/g, "Download $1 selected"],
    [/全选 (\d+)/g, "Select all ($1)"],
    [/图片 \+(\d+)/g, "Images +$1"],
    [/推送中 (\d+)\/(\d+)/g, "Sending $1/$2"],
    [/唤醒失败（([^）]*)）：([^\n]*)/g, "Wake-up failed ($1): $2"],
    [/扩展 ID (\S+)/g, "extension ID $1"],
    [/端口 (\d+)/g, "Port $1"],
    [/还没有嗅探到媒体/g, "No media sniffed yet"],
    // 嗅探按钮改成可拖动之后，提示语是拼在计数文案后面的（「ZDM 嗅探到 3 个媒体
    // （可拖动，双击复位）」），整句在 T_EXACT 里配不上，只能前缀和尾巴各认一条。
    [/ZDM 嗅探到的媒体/g, "Media found by ZDM"],
    [/ZDM 嗅探（暂无媒体）/g, "ZDM sniffer (no media yet)"],
    [/（可拖动，双击复位）/g, " (drag to move · double-click to reset)"],
    // 列表那一行是「类型 · 大小」拼进同一个文本节点（「视频 · 2.0 MB」），整句配不上
    // T_EXACT，只能认前缀。全部用 ^ 锚住，避免把别处的句子从中间切开；
    // 「图片 · 本页扫描 · 1.0 MB」是三段拼的，必须排在通用的 图片 · 那条之前。
    [/^图片 · 本页扫描 · /g, "Images · scanned from this page · "],
    [/^视频 · /g, "Video · "],
    [/^音频 · /g, "Audio · "],
    [/^图片 · /g, "Images · "],
    [/^HLS 流 · /g, "HLS stream · "],
    [/^DASH 流 · /g, "DASH stream · "],
    [/^播放列表分片 · /g, "Playlist segment · "],
    [/^媒体 · /g, "Media · "],
    // 大小未知也只会出现在拼好的那一行尾巴上（整句有自己的 T_EXACT 条目）
    [/大小未知/g, "size unknown"],
    [/播放列表的分片/g, "playlist segments"],
    // 拼出来的整句（中间夹着站点返回的错误文本），整句匹配不了，只能按两段认。
    [/解析失败：([^\n]*?)，可改用上面的「流媒体」/g, 'Parsing failed: $1 — use “Streaming” above'],
    [/，可改用上面的「流媒体」/g, " — use “Streaming” above"],
    // 设置页那句「与本机 ZDM 下载服务通信（默认 <code>…</code>）」被 <code> 切成
    // 两段，各自都是半个句子；两段都要认，否则英文界面里夹一段中文。
    [/与本机 ZDM 下载服务通信（默认/g, "Talks to the ZDM download service on this machine (default"],
    // 清晰度条目是「1080P · 视频+音频」这种拼法：高度部分是数字，只有后缀要翻。
    [/(P) · 视频\+音频/g, "$1 · video + audio"],
    // YouTube 音频轨的标签由 sites.js 拼出来（「音频 · 高音质 · 48 kHz」），
    // 分隔符和档位词都得在整串里换，整句匹配不到。
    [/音频 · ?/g, "Audio · "],
    [/低音质/g, "low quality"],
    [/中音质/g, "medium quality"],
    [/高音质/g, "high quality"],
    [/ · 默认$/g, " · default"],
  ];

  // 只在「这句确实被翻动了」之后才做的一步：残留的中文标点换成半角。
  // 完全没认出的整句中文不进入这里 —— 原样显示中文比半中半英好读。
  const T_PUNCT = [
    [/「/g, "“"],
    [/」/g, "”"],
    [/【/g, "["],
    [/】/g, "]"],
    [/（([^（）]*)）/g, " ($1)"],
    [/：(?=[A-Za-z0-9/])/g, ": "],
    [/。(?=\s|$)/g, "."],
    [/，/g, ", "],
    [/；/g, "; "],
    [/、/g, ", "],
  ];

  let langPref = "auto";
  function browserLang() {
    try {
      if (global.chrome && global.chrome.i18n && global.chrome.i18n.getUILanguage) {
        return global.chrome.i18n.getUILanguage() || "en";
      }
    } catch (_) {}
    return (global.navigator && global.navigator.language) || "en";
  }
  const uiLang = () =>
    langPref !== "auto"
      ? langPref
      : String(browserLang()).toLowerCase().indexOf("zh") === 0
      ? "zh"
      : "en";

  function trStr(raw) {
    if (uiLang() !== "en") return raw;
    const edge = /^(\s*)([\s\S]*?)(\s*)$/.exec(raw);
    const core = edge[2];
    if (!core) return raw;
    if (Object.prototype.hasOwnProperty.call(T_EXACT, core)) return edge[1] + T_EXACT[core] + edge[3];
    let out = core, hit = false;
    for (let i = 0; i < T_RULES.length; i++) {
      const next = out.replace(T_RULES[i][0], T_RULES[i][1]);
      if (next !== out) { out = next; hit = true; }
    }
    if (hit) for (let i = 0; i < T_PUNCT.length; i++) out = out.replace(T_PUNCT[i][0], T_PUNCT[i][1]);
    return out === core ? raw : edge[1] + out + edge[3];
  }

  const TXT_SRC = new WeakMap(), TXT_OUT = new WeakMap();
  const ATTR_SRC = new WeakMap();
  const ATTRS = ["placeholder", "title", "aria-label", "alt"];
  const IS_WEB_PAGE = /^(https?|file):$/.test(global.location ? global.location.protocol : "");

  function skip(el) {
    for (let n = el; n; n = n.parentNode && n.parentNode.host ? n.parentNode.host : n.parentElement) {
      if (n.nodeType === 1) {
        const tag = n.tagName;
        if (tag === "SCRIPT" || tag === "STYLE" || tag === "NOSCRIPT") return true;
        if (n.classList && n.classList.contains("no-i18n")) return true;
      }
    }
    return false;
  }

  // 与网页版同一个套路：记住界面代码写进去的那份原文，切回中文时才有东西可还原。
  function applyText(node, rescan) {
    const cur = node.nodeValue;
    let src = (rescan ? TXT_SRC.has(node) : TXT_OUT.has(node) && cur === TXT_OUT.get(node))
      ? TXT_SRC.get(node) : cur;
    const out = trStr(src);
    TXT_SRC.set(node, src);
    TXT_OUT.set(node, out);
    if (cur !== out) node.nodeValue = out;
  }

  function applyAttr(el, name, rescan) {
    const raw = el.getAttribute(name);
    if (raw === null) return;
    let map = ATTR_SRC.get(el);
    if (!map) { map = {}; ATTR_SRC.set(el, map); }
    const rec = map[name];
    const src = (rescan ? rec && rec.src !== undefined : rec && raw === rec.shown) ? rec.src : raw;
    const out = trStr(src);
    map[name] = { src, shown: out };
    if (raw !== out) el.setAttribute(name, out);
  }

  function walk(root) {
    if (!root || typeof document === "undefined") return;
    const tw = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
    const els = [];
    let n = tw.currentNode;
    while (n) {
      if (n.nodeType === 3) { if (!skip(n.parentElement)) applyText(n, true); }
      else if (n.nodeType === 1 && !skip(n)) els.push(n);
      n = tw.nextNode();
    }
    for (const el of els) {
      if (el.classList && el.classList.contains("no-i18n")) continue;
      for (const a of ATTRS) applyAttr(el, a, true);
    }
    if (root === document.body) applyTitle();
  }

  function applyTitle() {
    if (IS_WEB_PAGE) return;
    const t = document.title;
    const out = trStr(t);
    if (out !== t) document.title = out;
  }

  const watched = [];
  function observe(root) {
    if (typeof MutationObserver === "undefined") return;
    const mo = new MutationObserver((records) => {
      for (const r of records) {
        if (r.type === "characterData") {
          if (!skip(r.target.parentElement)) applyText(r.target, false);
        } else if (r.type === "attributes") {
          if (r.target.nodeType === 1 && !skip(r.target)) applyAttr(r.target, r.attributeName, false);
        } else {
          for (const added of r.addedNodes) {
            if (added.nodeType === 1) { if (!skip(added)) walk(added); }
            else if (added.nodeType === 3) { if (!skip(added.parentElement)) applyText(added, false); }
          }
        }
      }
    });
    mo.observe(root, {
      childList: true, subtree: true, characterData: true,
      attributes: true, attributeFilter: ATTRS,
    });
  }

  const ZDMUI = {
    lang: uiLang,
    t: trStr,
    // 设置页里换语言要立刻见效（否则用户改完还得点保存 + 重开页面才看到）。
    setLang(v) {
      langPref = ["auto", "zh", "en"].includes(v) ? v : "auto";
      ZDMUI.applyAll();
    },
    // 内容脚本把界面挂在 shadow root 里：只观察自己这几棵树，绝不遍历整个页面，
    // 否则在 YouTube 这类节点海量的页面上会白白吃掉一次全树扫描。
    watch(root) {
      if (!root) return;
      watched.push(root);
      walk(root);
      observe(root);
    },
    applyAll() {
      if (typeof document === "undefined") return;
      document.documentElement.lang = uiLang() === "en" ? "en" : "zh-CN";
      for (const r of watched) walk(r);
      if (!IS_WEB_PAGE) walk(document.body);
    },
  };
  global.ZDMUI = ZDMUI;

  if (typeof document !== "undefined") {
    document.documentElement.lang = uiLang() === "en" ? "en" : "zh-CN";
    if (!IS_WEB_PAGE) ZDMUI.watch(document.body);
  }
  // 语言偏好的读取放在 document 判断之外：service worker（background.js）没有 DOM，
  // 但通知和右键菜单同样是给用户看的 —— 在那里 langPref 永远停在 "auto" 的话，
  // 中文浏览器里一切正常，英文浏览器里 background 发出的通知就成了整页英文中
  // 唯一的一条中文。
  try {
    if (global.chrome && global.chrome.storage && global.chrome.storage.sync) {
      global.chrome.storage.sync.get({ ui_lang: "auto" }, (r) => {
        if (r && r.ui_lang && r.ui_lang !== langPref) {
          langPref = r.ui_lang;
          ZDMUI.applyAll();
        }
      });
      if (global.chrome.storage.onChanged) {
        global.chrome.storage.onChanged.addListener((ch, area) => {
          if (area !== "sync" || !ch.ui_lang) return;
          langPref = ch.ui_lang.newValue || "auto";
          ZDMUI.applyAll();
        });
      }
    }
  } catch (_) {}
})(typeof window !== "undefined" ? window : this);
