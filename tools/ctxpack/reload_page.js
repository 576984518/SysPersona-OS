// SysPersona-OS · 本文件：reload_page.js — CDP 强制刷新网页会话（读会话铁律的"重试"那一步）
// reload_page.js — CDP 强制刷新网页会话（读会话铁律的"重试"那一步）
// 用法：node reload_page.js [url子串]      默认 chat.deepseek.com
// 退出码：0 = 至少刷了一页；1 = 没找到页面或 CDP 不通
const http = require('http');

const port = process.env.CDP_PORT || 9222;
const want = process.argv[2] || 'chat.deepseek.com';

function get(u) {
  return new Promise((res, rej) => {
    http.get(u, (r) => { let d = ''; r.on('data', (c) => d += c); r.on('end', () => res(d)); }).on('error', rej);
  });
}

(async () => {
  let list;
  try {
    list = JSON.parse(await get(`http://127.0.0.1:${port}/json`));
  } catch (e) {
    console.log('CDP 不通：' + e.message);
    process.exit(1);
  }
  const pages = list.filter((t) => t.type === 'page' && t.url.includes(want));
  if (!pages.length) { console.log('没有匹配的页面：' + want); process.exit(1); }
  for (const p of pages) {
    await new Promise((done) => {
      const ws = new WebSocket(p.webSocketDebuggerUrl);
      ws.onopen = () => {
        ws.send(JSON.stringify({ id: 1, method: 'Page.reload', params: { ignoreCache: true } }));
        setTimeout(() => { try { ws.close(); } catch (e) { } ; done(); }, 900);
      };
      ws.onerror = () => done();
      setTimeout(done, 3000);
    });
    console.log('已刷新：' + p.title);
  }
  process.exit(0);
})();
