/* 把页面跳转到 zdm:// —— 扩展页可以触发自定义协议，后台的 tabs.create 不行。
   跳转成功后这个标签页已经没有用了，稍后由 background.js 关掉。 */
(function () {
  const p = new URLSearchParams(location.search);
  const u = p.get("u");
  if (u) {
    // 用 assign 而不是 replace：失败时不要留下一个坏掉的历史记录。
    location.assign(u);
  }
  setTimeout(function () {
    // 关不掉也不影响；background.js 会在 5 秒后再试一次。
    window.close();
  }, 1500);
})();
