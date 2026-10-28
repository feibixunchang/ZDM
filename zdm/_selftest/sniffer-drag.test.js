/* 「嗅探」按钮可拖动 —— 无排版环境的自测（jsdom）
 *
 * 跑法：node _selftest/sniffer-drag.test.js
 *
 * jsdom 没有排版引擎（offsetWidth / getBoundingClientRect 全是 0），所以这里给
 * 宿主盒子补一个「按 inline style 反算矩形」的替身，让拖动、夹紧、面板展开方向
 * 这些算术能被真的检一遍。长得好不好看仍要在浏览器里看，本用例只判坐标与存档。
 */
"use strict";

const fs = require("fs");
const path = require("path");
const { JSDOM } = require(path.join(__dirname, "node_modules", "jsdom"));

const EXT = path.join(__dirname, "..", "extension");
const UI18N = fs.readFileSync(path.join(EXT, "ui18n.js"), "utf8");
const SNIFFER = fs.readFileSync(path.join(EXT, "sniffer.js"), "utf8");

const FAB_W = 96; // 展开态宽度（与 sniffer 里 hostSize() 的兜底值一致）
const FAB_H = 40;
const DOCK_W = 42; // 收起态：CSS 把 .fab.docked 收成 42x42 的圆胶囊
const DOCK_H = 42;
const EDGE = 8; // 与 sniffer 里「离边最近只能到 8px」的夹紧值一致
const SNAP = EDGE; // 吸边后实际落点（SNAP_X=6 会被夹到 8）
const VW0 = 1024;
const VH0 = 768;
const DEF_L = VW0 - FAB_W - 18; // 旧版 right:18 的等效 left
const DEF_T = VH0 - FAB_H - 18; // 旧版 bottom:18 的等效 top

let pass = 0;
const fails = [];
function ok(cond, label) {
  if (cond) {
    pass++;
    console.log("  PASS  " + label);
  } else {
    fails.push(label);
    console.log("  FAIL  " + label);
  }
}
function eq(a, b, label) {
  ok(
    JSON.stringify(a) === JSON.stringify(b),
    `${label}    [期望 ${JSON.stringify(b)} | 实际 ${JSON.stringify(a)}]`
  );
}
const tick = (ms = 40) => new Promise((r) => setTimeout(r, ms));
const num = (s) => (s === "" || s == null ? null : parseFloat(s));
const hasCJK = (s) => /[\u4e00-\u9fff]/.test(String(s || ""));

/* ---------- chrome 替身 ---------- */
function makeChrome(store) {
  const onChanged = [];
  const chrome = {
    runtime: {
      lastError: null,
      sendMessage: (msg, cb) => {
        if (typeof cb !== "function") return;
        setTimeout(() => cb({ ok: true, items: store.items || [] }), 0);
      },
      onMessage: { addListener: () => {} },
    },
    storage: {
      sync: {
        get: (def, cb) => setTimeout(() => cb(Object.assign({}, def, store.sync)), 0),
        set: (obj, cb) => {
          const snap = JSON.parse(JSON.stringify(obj));
          store.sets.push(snap);
          Object.assign(store.sync, snap);
          if (cb) setTimeout(cb, 0);
        },
      },
      local: {
        get: (d, cb) => setTimeout(() => cb(Object.assign({}, d)), 0),
        set: (o, cb) => cb && cb(),
      },
      onChanged: { addListener: (f) => onChanged.push(f) },
    },
    i18n: { getUILanguage: () => store.lang || "zh-CN" },
  };
  chrome.__fire = (area, changes) => onChanged.forEach((f) => f(changes, area));
  return chrome;
}

/* ---------- 起一个「网页」，按 manifest 的顺序把 ui18n + sniffer 灌进去 ---------- */
async function boot(store) {
  const dom = new JSDOM(
    `<!doctype html><html><head><title>测试页</title></head><body><div id="app">x</div></body></html>`,
    { runScripts: "outside-only", url: "https://example.com/watch", pretendToBeVisual: true }
  );
  const w = dom.window;
  Object.defineProperty(w, "innerWidth", { value: VW0, configurable: true });
  Object.defineProperty(w, "innerHeight", { value: VH0, configurable: true });
  w.chrome = makeChrome(store);
  w.eval(UI18N);
  w.eval(SNIFFER);

  const host = w.document.getElementById("zdm-sniffer-host");
  const root0 = host.shadowRoot;
  const fab0 = root0.querySelector(".fab");
  // jsdom 不排版：按 inline style 反算矩形，并且让宽高跟着「是否收起」变化 ——
  // 这样「收起之后还贴不贴着边」这种算术才检得出真值（收起态 CSS 是 42x42）。
  const rectOf = () => {
    const isDocked = fab0.classList.contains("docked");
    const wd = isDocked ? DOCK_W : FAB_W;
    const hh = isDocked ? DOCK_H : FAB_H;
    const left = parseFloat(host.style.left) || 0;
    const top = parseFloat(host.style.top) || 0;
    return { left, top, right: left + wd, bottom: top + hh, width: wd, height: hh, x: left, y: top };
  };
  host.getBoundingClientRect = rectOf;
  fab0.getBoundingClientRect = rectOf;
  for (const [name, pick] of [["offsetWidth", () => rectOf().width], ["offsetHeight", () => rectOf().height]]) {
    Object.defineProperty(host, name, { get: pick, configurable: true });
  }

  const root = root0;
  const fab = fab0;
  const panel = root.querySelector(".panel");

  const fire = (el, type, x, y) =>
    el.dispatchEvent(new w.MouseEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0 }));

  // 按住某点 ->（先给一个 2px 抖动，验证死区）-> 移到目标 -> 松手
  async function grabTo(downX, downY, toX, toY) {
    fire(fab, "pointerdown", downX, downY);
    fire(fab, "pointermove", downX + 2, downY + 2);
    fire(fab, "pointermove", toX, toY);
    fire(fab, "pointerup", toX, toY);
    await tick(0);
  }
  async function resizeTo(vw, vh) {
    Object.defineProperty(w, "innerWidth", { value: vw, configurable: true });
    Object.defineProperty(w, "innerHeight", { value: vh, configurable: true });
    w.dispatchEvent(new w.Event("resize"));
    await tick(220); // 监听里有 120ms 防抖
  }

  await tick(60); // 等 storage.sync.get（位置 + 收纳开关 + 语言）与首轮 refresh 回来
  const isDocked = () => fab.classList.contains("docked");
  return { dom, w, host, root, fab, panel, fire, grabTo, resizeTo, isDocked };
}

(async () => {
  console.log("\n[1] 默认落位 / 拖动 / 存档 / 复位（收纳关掉＝纯自由摆放）");
  {
    const store = { sync: { sniff_dock: false }, sets: [], items: [], lang: "zh-CN" };
    const { host, fab, panel, root, fire, grabTo, w } = await boot(store);
    const st = () => host.style;

    ok(st().position === "fixed", "按钮仍是 position:fixed（不随页面滚动跑掉）");
    ok(!st().right && !st().bottom, "写死的 right/bottom 已换成 left/top");
    eq([num(st().left), num(st().top)], [DEF_L, DEF_T], "初始落位＝旧版右下角");
    eq(store.sets.length, 0, "刚挂上时一次都不写盘");

    // 死区：只挪 2px 不算拖动（手抖不该把面板闪没，也不该写盘）
    fire(fab, "pointerdown", DEF_L + 50, DEF_T + 10);
    fire(fab, "pointermove", DEF_L + 52, DEF_T + 12);
    eq([num(st().left), num(st().top)], [DEF_L, DEF_T], "4px 死区内的抖动不移动按钮");
    fire(fab, "pointerup", DEF_L + 52, DEF_T + 12);
    eq(store.sets.length, 0, "没真拖动就不写盘");
    await tick(0);

    // 真拖：右下 → 屏幕中间
    await grabTo(DEF_L + 50, DEF_T + 10, 600, 400);
    eq([num(st().left), num(st().top)], [550, 390], "按住偏移保持不变（拖到 600,400 → left 550 / top 390）");
    eq(store.sets.length, 1, "整次拖动只在松手写一次盘（storage.sync 有每秒配额）");
    eq(
      store.sync.sniff_pos,
      { ax: "right", x: VW0 - FAB_W - 550, ay: "bottom", y: VH0 - FAB_H - 390 },
      "存的是「离右/离下多远」，不是绝对坐标"
    );

    // 拖动后浏览器补发的 click 必须被吞掉
    fire(fab, "click", 600, 400);
    eq(panel.hidden, true, "松手补发的那次 click 不会顺手开关面板");

    // 单击照常打开面板
    fire(fab, "click", 600, 400);
    eq(panel.hidden, false, "单击照常打开面板");
    ok(num(panel.style.top) < 0, "按钮在下半屏 → 面板朝上开（相对 top 为负）");
    const pl = num(st().left) + num(panel.style.left);
    ok(pl >= 8 && pl + 336 <= VW0 - 7, `面板横向夹在可视区内（视口左缘 ${pl}px）`);

    // 别的标签页改了位置 → 这边同步过来，同时面板翻向
    w.chrome.__fire("sync", { sniff_pos: { newValue: { ax: "left", x: 20, ay: "top", y: 20 } } });
    await tick(0);
    eq([num(st().left), num(st().top)], [20, 20], "storage.onChanged 把按钮挪到别处放的位置");
    ok(num(panel.style.top) > 0, "按钮到了上半屏 → 面板自动翻到下方开");
    const pt = num(st().top) + num(panel.style.top);
    const pl2 = num(st().left) + num(panel.style.left);
    ok(pt >= 8 && pl2 >= 8, `翻向后面板仍在屏内（视口 ${pl2}px,${pt}px）`);

    // 面板标题栏的「↺」复位
    root.querySelector(".hd .rst").dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
    await tick(0);
    eq(store.sync.sniff_pos, null, "点「↺」把存档写回 null（＝默认位）");
    eq([num(st().left), num(st().top)], [DEF_L, DEF_T], "复位后按钮回到旧版右下角");

    // 双击复位
    await grabTo(DEF_L + 74, DEF_T + 28, 300, 300);
    const dragged = JSON.stringify(store.sync.sniff_pos);
    ok(dragged !== "null", `拖远之后存档确实变了（${dragged}）`);
    fire(fab, "dblclick", 226, 272);
    await tick(0);
    eq(store.sync.sniff_pos, null, "双击按钮等于复位");
    eq([num(st().left), num(st().top)], [DEF_L, DEF_T], "双击复位把按钮挪回默认位");

    // 拖动必须的样式（写错一条就变成「一拖就滚页面」或「一拖就划选中」）
    const css = root.querySelector("style").textContent.replace(/\s+/g, "");
    ok(/touch-action:none/.test(css), ".fab 带 touch-action:none（触屏拖动不会滚页面）");
    ok(/user-select:none/.test(css), ".fab 禁选文字（拖动不会划出一片蓝）");
    ok(!/\.panel\{[^}]*right:0;bottom:48px/.test(css), "面板不再写死 right:0;bottom:48px");
    ok(/flex-direction:column/.test(css), "面板改成纵向 flex（列表跟着 max-height 内滚）");
    ok(/\.grabbing|grabbing\{/.test(css), "拖动中有 .grabbing 态（光标与阴影变化）");

    // 外部调用（playerbtn.js 会走 __zdmSniffer.open/close）不能因为面板变量改造而报错
    w.__zdmSniffer.open();
    await tick(0);
    eq(panel.hidden, false, "__zdmSniffer.open() 仍然能打开面板");
    w.__zdmSniffer.close();
    eq(panel.hidden, true, "__zdmSniffer.close() 仍然能关闭面板");
    w.__zdmSniffer.resetPosition();
    eq([num(st().left), num(st().top)], [DEF_L, DEF_T], "新增的 __zdmSniffer.resetPosition() 可复位");
  }

  console.log("\n[2] 重新打开网页时按存档落位");
  {
    const store = { sync: { sniff_pos: { ax: "right", x: 240, ay: "bottom", y: 120 } }, sets: [], items: [], lang: "zh-CN" };
    const { host } = await boot(store);
    eq([parseFloat(host.style.left), parseFloat(host.style.top)], [VW0 - FAB_W - 240, VH0 - FAB_H - 120], "「离右 240 / 离下 120」原样还原");
  }
  {
    const store = { sync: { sniff_pos: { ax: "left", x: 4000, ay: "top", y: 4000 } }, sets: [], items: [], lang: "zh-CN" };
    const { host } = await boot(store);
    const l = parseFloat(host.style.left);
    const t = parseFloat(host.style.top);
    ok(l <= VW0 - FAB_W - 8 && t <= VH0 - FAB_H - 8, `越界存档被夹回屏内 (${l},${t})`);
  }
  {
    const store = { sync: { sniff_pos: { x: "abc" } }, sets: [], items: [], lang: "zh-CN" };
    const { host } = await boot(store);
    eq([parseFloat(host.style.left), parseFloat(host.style.top)], [DEF_L, DEF_T], "形状不对的存档退回默认右下角（不把按钮丢出屏幕）");
  }

  console.log("\n[3] 窗口变化时按钮跟着回到屏内");
  {
    const store = { sync: { sniff_pos: { ax: "right", x: 18, ay: "bottom", y: 18 } }, sets: [], items: [], lang: "zh-CN" };
    const { host, resizeTo } = await boot(store);
    await resizeTo(600, 500);
    eq([parseFloat(host.style.left), parseFloat(host.style.top)], [600 - FAB_W - 18, 500 - FAB_H - 18], "窗口变小：贴右边的按钮继续贴右边");
  }
  {
    const store = { sync: { sniff_pos: { ax: "left", x: 900, ay: "top", y: 10 } }, sets: [], items: [], lang: "zh-CN" };
    const { host, resizeTo } = await boot(store);
    await resizeTo(500, 800);
    const l = parseFloat(host.style.left);
    ok(l >= 8 && l <= 500 - FAB_W - 8, `靠左存档超出新宽度时夹紧（${l}px ≤ ${500 - FAB_W - 8}px）`);
  }

  console.log("\n[4] 英文界面：面板整棵树零中文残留 + 切回中文逐字还原");
  {
    // 覆盖列表行的每一种拼法（文件名故意用 ASCII：真实标题里的中文属于内容，不该翻）
    const store = {
      sync: { ui_lang: "en" },
      sets: [],
      items: [
        { url: "https://cdn.example.com/a.mp4", kind: "video", size: 2097152, name: "a.mp4" },
        { url: "https://cdn.example.com/b.m4a", kind: "audio", size: 0, name: "b.m4a" },
        { url: "https://cdn.example.com/c.jpg", kind: "image", from: "dom-img", size: 150000, name: "c.jpg" },
        { url: "https://cdn.example.com/master.m3u8", kind: "hls", size: 0, name: "master.m3u8" },
        { url: "https://cdn.example.com/manifest.mpd", kind: "dash", size: 88, name: "manifest.mpd" },
        { url: "https://cdn.example.com/seg-1.m4s", kind: "video", seg: true, size: 4096, name: "seg-1.m4s" },
        { url: "https://cdn.example.com/x", kind: "unknown-kind", size: 1024, name: "x.bin" },
        { url: "https://cdn.example.com/y.mp4", kind: "video", size: 5000000, approx: true, name: "y.mp4" },
      ],
      lang: "en",
    };
    const { root, fab, panel, fire, w } = await boot(store);
    await tick(120); // 等 refresh 写进 DOM 之后 MutationObserver 再翻一遍
    ok(w.document.documentElement.lang === "en", "documentElement.lang 切到 en");

    const t1 = fab.getAttribute("title");
    ok(!hasCJK(t1), `「有 N 个媒体」＋拖动提示整句是英文：${t1}`);
    ok(/drag to move/.test(t1) && /double-click to reset/.test(t1), "提示语含 drag / double-click 两个关键词");

    const rst = root.querySelector(".hd .rst").getAttribute("title");
    ok(!hasCJK(rst), `复位按钮提示译出：${rst}`);

    // 打开面板，把整棵 shadow 树（含列表行）过一遍
    fire(fab, "click", 960, 720);
    await tick(160);
    // 必须走 childNodes：走 children 会把叶子文本节点全跳过，断言就成了空转
    const residual = [];
    const walk = (n) => {
      for (const c of n.childNodes || []) {
        if (c.nodeType === 3) {
          if (hasCJK(c.nodeValue)) residual.push(c.nodeValue.trim());
        } else if (c.nodeType === 1 && c.tagName !== "STYLE" && c.tagName !== "SCRIPT") {
          walk(c);
        }
      }
    };
    walk(root);
    for (const el of root.querySelectorAll("[title]")) {
      const v = el.getAttribute("title");
      if (hasCJK(v)) residual.push("@title:" + v.slice(0, 40));
    }
    ok(residual.length === 0, "面板整棵 shadow 树零中文残留" + (residual.length ? `；残留 ${residual.length} 处：${JSON.stringify(residual.slice(0, 12))}` : ""));
    ok(!hasCJK(root.querySelector(".fab").textContent), `按钮本体两个字也翻了：${root.querySelector(".fab").textContent.trim()}`);
    // 列表行的拼法要真的被拼出来（不是没渲染）
    const rows = [...root.querySelectorAll(".row .sub")].map((e) => e.textContent.trim());
    ok(rows.length === 8, `8 种类型行都渲染出来了（实际 ${rows.length} 行）`);
    console.log("      列表行英文样式：" + JSON.stringify(rows));

    // 没有媒体时的那句（尾巴拼在「ZDM 嗅探（暂无媒体）」后面）
    store.items = [];
    await w.__zdmSniffer.refresh();
    await tick(140);
    const t2 = fab.getAttribute("title");
    ok(!hasCJK(t2), `「暂无媒体」＋提示尾巴也整句英文：${t2}`);

    // 切回中文：界面代码自己写进去的原文要逐字还原
    w.chrome.__fire("sync", { ui_lang: { newValue: "zh" } });
    await tick(140);
    ok(hasCJK(fab.getAttribute("title")), "切回中文后标题恢复中文");
    ok(/（可拖动，双击复位）/.test(fab.getAttribute("title")), `尾巴逐字还原：${fab.getAttribute("title")}`);
    eq(panel.hidden, false, "语言切换不会把已开的面板关掉");
  }

  console.log("\n[6] 贴边收纳（默认开）：吸边 → 收成圆点 → 经过/点击展开");
  {
    const SNAP_LEFT_RIGHT = VW0 - FAB_W - EDGE; // 展开态吸在右边时的 left
    const DOCK_LEFT_RIGHT = VW0 - DOCK_W - EDGE; // 收起态（42x42）吸在右边时的 left
    const store = { sync: {}, sets: [], items: [], lang: "zh-CN" }; // sniff_dock 不给 → 默认开
    const { host, fab, panel, root, fire, grabTo, isDocked, w } = await boot(store);
    const st = () => host.style;

    eq([num(st().left), num(st().top)], [DEF_L, DEF_T], "默认落位照旧（右下角，离边 18px）");
    await tick(2800); // 等收纳计时（守卫 1.5s + 延时）
    ok(!isDocked(), "默认位置不算贴边 → 不收（刚装上的人不会看到按钮自己变小）");

    // 拖到屏幕中间偏右：松手应当吸到右边，纵向位置照用户摆的
    await grabTo(DEF_L + 50, DEF_T + 10, 600, 400);
    eq([num(st().left), num(st().top)], [SNAP_LEFT_RIGHT, 390], "松手吸附到右边（纵向保持 390）");
    eq(store.sync.sniff_pos, { ax: "right", x: EDGE, ay: "bottom", y: VH0 - FAB_H - 390 }, "存档记下「贴边」的离边距离");
    eq(store.sets.length, 1, "吸附不额外写第二次盘");

    await tick(2800);
    ok(isDocked(), "闲置一会儿之后收成圆点");
    eq(num(st().left), DOCK_LEFT_RIGHT, "收起态按 42px 宽重新贴边（不会往屏幕里缩一截）");
    ok(host.getBoundingClientRect().right <= VW0, "整颗圆点仍在屏内（不做伸出屏幕外的悬浮球）");

    // 鼠标经过 → 展开
    fire(fab, "pointerenter", DOCK_LEFT_RIGHT + 10, 400);
    ok(!isDocked(), "鼠标移上去立刻展开");
    eq(num(st().left), SNAP_LEFT_RIGHT, "展开后按 96px 宽重新贴边");

    // 离开 → 再次收起
    fire(fab, "pointerleave", 200, 200);
    await tick(2800);
    ok(isDocked(), "光标离开之后又收回去");

    // 收起态点一下：先展开，并且照常打开面板
    fire(fab, "click", DOCK_LEFT_RIGHT + 10, 400);
    ok(!isDocked(), "收起态点击先展开");
    eq(panel.hidden, false, "收起态点击同时打开面板");
    await tick(2800);
    ok(!isDocked(), "面板开着的时候绝不收起（否则面板还露着按钮先没了）");

    // 关面板之后又能收
    root.querySelector(".hd .x").dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
    await tick(2800);
    ok(isDocked(), "面板关掉之后恢复收纳");

    // 复位 → 回到默认位 → 不再参与收纳
    fire(fab, "dblclick", DOCK_LEFT_RIGHT + 10, 400);
    eq([num(st().left), num(st().top)], [DEF_L, DEF_T], "双击复位回到默认右下角");
    await tick(2800);
    ok(!isDocked(), "复位后不贴边 → 不收");

    // 设置页关掉开关：不吸边也不收起
    w.chrome.__fire("sync", { sniff_dock: { newValue: false } });
    await grabTo(DEF_L + 50, DEF_T + 10, 600, 400);
    eq(num(st().left), 550, "关了收纳就是纯自由摆放（550 而不是吸到 " + SNAP_LEFT_RIGHT + "）");
    await tick(2800);
    ok(!isDocked(), "开关关掉之后绝不会自己缩");

    // 收起态需要的样式都在（少一条就会变成「文字还露着」或「绿点亮不起来」）
    const css = root.querySelector("style").textContent.replace(/\s+/g, "");
    ok(/\.fab\.docked\{[^}]*width:42px/.test(css), ".fab.docked 收成 42px 圆胶囊");
    ok(/\.fab\.docked\.lbl\{display:none\}|\.fab\.docked\.lbl\{[^}]*display:none/.test(css), "收起时藏掉「嗅探」文字");
    ok(/\.fab\.docked\.badge/.test(css), "收起时角标换成小圆点");
    ok(/\.fab\.docked\{[^}]*opacity:\.55/.test(css), "收起态半透明（.55）");
    ok(/\.fab\.docked:hover/.test(css), "hover 展开有对应样式");
  }

  {
    // 上个版本已经把它放在贴边位置（存档 x<=12）：新扩展一加载就该参与收纳
    const store = {
      sync: { sniff_pos: { ax: "right", x: 4, ay: "bottom", y: 120 } },
      sets: [],
      items: [],
      lang: "zh-CN",
    };
    const { host, isDocked } = await boot(store);
    eq(num(host.style.left), VW0 - FAB_W - SNAP, "读回贴边存档后按最小离边距离落位（多余的 4px 被夹到 8px）");
    await tick(3000);
    ok(isDocked(), "本来就贴在边上的存档，闲置之后照样收（不用用户再拖一次）");
  }

  {
    // 空列表时绿点不该亮着骗人：data-empty=1 + docked 时角标要隐藏（CSS 判断）
    const store = { sync: {}, sets: [], items: [], lang: "zh-CN" };
    const { root, grabTo, isDocked, fab } = await boot(store);
    await grabTo(DEF_L + 50, DEF_T + 10, 600, 400);
    await tick(2800);
    ok(isDocked(), "先确认能收起来");
    eq(fab.dataset.empty, "1", "没嗅到媒体时 data-empty=1（配合 CSS 把绿点藏掉）");
    const css = root.querySelector("style").textContent.replace(/\s+/g, "");
    ok(/\.fab\.docked\[data-empty="1"\]\.badge\{display:none\}/.test(css), "空列表 + 收起态：绿点不显示");
  }

  console.log("\n[5] 清单与权限");
  {
    const mf = JSON.parse(fs.readFileSync(path.join(EXT, "manifest.json"), "utf8"));
    ok(mf.permissions.includes("storage"), "manifest 早就带 storage（存位置不需要新权限）");
    ok(mf.version === "1.5.44", `扩展版本递增到 1.5.44（当前 ${mf.version}）`);
    const cs = mf.content_scripts[0];
    ok(cs.js.indexOf("ui18n.js") < cs.js.indexOf("sniffer.js"), "嗅探脚本仍排在 ui18n 之后（词条先就位）");
    ok(!/right:\s*18px/.test(SNIFFER), "sniffer.js 里已经没有写死的 right:18px");
  }

  console.log(`\n合计：${pass} 条通过，${fails.length} 条失败`);
  if (fails.length) console.log("失败清单：\n - " + fails.join("\n - "));
  // sniffer 里有个 3 秒轮询的 setInterval，不主动退出会把 node 吊住
  process.exit(fails.length ? 1 : 0);
})().catch((e) => {
  console.error("用例自身抛错：", e && e.stack ? e.stack : e);
  process.exit(2);
});
