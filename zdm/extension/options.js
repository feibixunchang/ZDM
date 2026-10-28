const DEFAULTS = {
  serverUrl: "http://127.0.0.1:9000",
  intercept: true,
  connections: 8,
  directory: "",
  notify: true,
  // 网页里「嗅探」按钮的贴边收纳：松手吸到左右边缘 + 闲置后收成小圆点。
  // 关掉就是纯自由摆放（不吸边、不收起），老用户可以直接关。
  sniff_dock: true,
  // 界面语言：auto = 跟随浏览器。存 sync，内容脚本起来时读同一个键，
  // 不然「设置里选了 English，网页上的按钮还是中文」。
  ui_lang: "auto",
};
const $ = (s) => document.querySelector(s);

chrome.storage.sync.get(DEFAULTS, (cfg) => {
  $("#serverUrl").value = cfg.serverUrl;
  $("#directory").value = cfg.directory;
  $("#connections").value = cfg.connections;
  $("#intercept").checked = cfg.intercept;
  $("#notify").checked = cfg.notify;
  $("#dock").checked = cfg.sniff_dock !== false;
  $("#uiLang").value = ["auto", "zh", "en"].includes(cfg.ui_lang) ? cfg.ui_lang : "auto";
});

// 换语言立刻重排本页（存盘仍由「保存」负责）：否则改完 English 得重开页面才看得到，
// 而这一页正是用户用来设置语言的地方，最不能让它自己不变。
$("#uiLang").onchange = () => {
  try {
    if (typeof ZDMUI !== "undefined") ZDMUI.setLang($("#uiLang").value);
  } catch (_) {}
};

$("#save").onclick = () => {
  chrome.storage.sync.set({
    serverUrl: $("#serverUrl").value.trim() || DEFAULTS.serverUrl,
    directory: $("#directory").value.trim(),
    connections: Number($("#connections").value) || 8,
    intercept: $("#intercept").checked,
    notify: $("#notify").checked,
    sniff_dock: $("#dock").checked,
    ui_lang: $("#uiLang").value,
  }, () => { $("#msg").textContent = "已保存"; });
};

$("#test").onclick = () => {
  // Go through the background page: it holds the API token. A direct fetch
  // from an options tab would now get 401.
  chrome.runtime.sendMessage({ type: "state" }, (j) => {
    const ok = j && j.ok && !chrome.runtime.lastError;
    $("#msg").textContent = ok
      ? "连接成功 ✓"
      : "连接失败：ZDM 未运行（点浏览器里的下载会自动唤醒它，也可手动打开 ZDM 一次）";
    $("#msg").style.color = ok ? "#16a34a" : "#dc2626";
  });
};
