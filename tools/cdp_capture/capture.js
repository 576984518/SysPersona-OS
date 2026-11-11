#!/usr/bin/env node
// SysPersona-OS · 本文件：cdp_capture.js — 从已登录的 DeepSeek 网页会话里全量抓取消息（参考实现）
/*
 * cdp_capture.js — 从已登录的 DeepSeek 网页会话里全量抓取消息（参考实现）
 *
 * 背景（2026-11-09 实测）：`.ds-virtual-list` 是虚拟列表，
 * 一次 querySelectorAll 只能拿到"当前挂载的那十几条"，
 * 所以旧采集器长期只抓到尾部、把 402 当成总数（真实 1098+）。
 *
 * 三个必须做的动作：
 *   1. 页面不可见时虚拟列表**不重绘** → Emulation.setFocusEmulationEnabled + Page.setWebLifecycleState('active')
 *   2. 刷新后视图锚在底部，直接赋 scrollTop 会被弹回 → 用**真实滚轮事件**（Input.dispatchMouseEvent）
 *   3. 列表总高是**估的**，滚动中会变（998K→1.13M）→ 不能按坐标去重，只能按"相邻窗口重叠行"对齐
 *
 * 用法：
 *   node cdp_capture.js --out session.md                 # 全量（自底向上扫到顶）
 *   node cdp_capture.js --out delta.md --anchor-file a.txt  # 增量：撞到锚点即停，只输出锚点之后
 *
 * 前提：一个带 --remote-debugging-port=9222 且**已登录**的浏览器窗口。
 * 退出码：0 成功；1 环境/页面问题。
 */
const CDP = process.env.CDP_ENDPOINT || 'http://127.0.0.1:9222';
const argv = process.argv.slice(2);
const arg = (k, d) => { const i = argv.indexOf(k); return i >= 0 ? argv[i + 1] : d; };
const OUT = arg('--out', 'capture.md');
const DELTA = parseInt(arg('--delta', '2000'), 10);
const ANCHOR_FILE = arg('--anchor-file', '');
const MAX_STEPS = parseInt(arg('--max-steps', '4000'), 10);

const fs = require('fs');
const crypto = require('crypto');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const ROWS_JS = `(() => {
  const rows = [];
  for (const n of document.querySelectorAll('.ds-message')) {
    const isUser = /d29f3d7d/.test((n.className || '').toString());
    const thinkEl = n.querySelector('.ds-think-content');
    const mds = [...n.querySelectorAll('.ds-markdown')].filter((m) => !m.closest('.ds-think-content'));
    let body = mds.length
      ? mds.map((m) => (m.innerText || '').trim()).filter(Boolean).join('\\n\\n')
      : (n.innerText || '').trim();
    if (!body) body = '[图片×' + n.querySelectorAll('img').length + ']';
    rows.push({
      role: isUser ? '用户' : '助手',
      think: thinkEl ? (thinkEl.innerText || '').trim() : '',
      body,
    });
  }
  const v = [...document.querySelectorAll('.ds-virtual-list')].sort((a, b) => b.scrollHeight - a.scrollHeight)[0];
  const r = (v || document.body).getBoundingClientRect();
  return {
    rows,
    st: v.scrollTop,
    sh: v.scrollHeight,
    x: Math.round(r.x + r.width / 2),
    y: Math.round(r.y + r.height / 2),
  };
})()`;

async function connect() {
  const list = await (await fetch(CDP + '/json/list')).json();
  const page = list.find((t) => t.type === 'page' && t.url.includes('chat.deepseek.com'));
  if (!page) throw new Error('没找到 chat.deepseek.com 页面（调试浏览器没开？）');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let id = 0;
  const pend = new Map();
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); }
  };
  const send = (method, params = {}) =>
    new Promise((res, rej) => {
      const i = ++id;
      pend.set(i, res);
      ws.send(JSON.stringify({ id: i, method, params }));
      setTimeout(() => { if (pend.has(i)) { pend.delete(i); rej(new Error('timeout ' + method)); } }, 120000);
    });
  const ev = async (expr) => {
    const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.result && r.result.exceptionDetails) throw new Error('eval: ' + JSON.stringify(r.result.exceptionDetails).slice(0, 200));
    return r.result ? r.result.result.value : undefined;
  };
  return { ws, send, ev, page };
}

function merge(acc, win, keyOf) {
  let k = 0;
  for (let cand = Math.min(win.length, acc.length); cand > 0; cand--) {
    let ok = true;
    for (let j = 0; j < cand; j++) if (keyOf(win[win.length - cand + j]) !== keyOf(acc[j])) { ok = false; break; }
    if (ok) { k = cand; break; }
  }
  return { acc: k === win.length ? acc : win.slice(0, win.length - k).concat(acc), k };
}

(async () => {
  const { ws, send, ev, page } = await connect();

  await send('Page.enable');
  // ① 让它以为自己可见/聚焦，否则虚拟列表不重绘
  await send('Emulation.setFocusEmulationEnabled', { enabled: true });
  await send('Page.setWebLifecycleState', { state: 'active' });
  // 视口必须回到"正常大小"：撑得太大反而会一次性挂上千条，窗口对齐全乱
  await send('Emulation.setDeviceMetricsOverride', { width: 1280, height: 888, deviceScaleFactor: 1, mobile: false });
  await sleep(1200);

  // 回到底部（刷新后本来就锚在底部，这里保证一致）
  await ev(`(() => { const v=[...document.querySelectorAll('.ds-virtual-list')].sort((a,b)=>b.scrollHeight-a.scrollHeight)[0]; v.scrollTop=v.scrollHeight; return 1; })()`);
  await sleep(1000);

  const anchor = ANCHOR_FILE && fs.existsSync(ANCHOR_FILE)
    ? fs.readFileSync(ANCHOR_FILE, 'utf8').replace(/\s+/g, '').trim()
    : '';
  const norm = (s) => (s || '').replace(/\s+/g, '');
  const keyOf = (r) => r.role + '\u0001' + r.think + '\u0001' + r.body;

  let cur = await ev(ROWS_JS);
  let acc = cur.rows.slice();
  let steps = 0;
  let still = 0;
  let hit = false;
  const t0 = Date.now();

  const foundAnchor = () => anchor && acc.some((r) => norm(r.body).startsWith(anchor));
  if (foundAnchor()) hit = true;

  while (!hit && cur.st > 2 && steps < MAX_STEPS) {
    const before = cur.st;
    // ② 真实滚轮向上，滚动位置才不会被锚定逻辑弹回
    await send('Input.dispatchMouseEvent', { type: 'mouseWheel', x: cur.x, y: cur.y, deltaX: 0, deltaY: -DELTA, modifiers: 0 });
    await sleep(260);
    const g = await ev(ROWS_JS);
    if (g.st === before) { if (++still >= 3) break; continue; }
    still = 0;
    const m = merge(acc, g.rows, keyOf);
    acc = m.acc;
    cur = g;
    steps++;
    if (foundAnchor()) hit = true;
  }

  // 增量模式：截掉锚点及其之前
  let cut = -1;
  if (anchor) for (let i = 0; i < acc.length; i++) if (norm(acc[i].body).startsWith(anchor)) { cut = i; break; }
  const msgs = cut >= 0 ? acc.slice(cut + 1) : acc;

  const parts = msgs.map((m) => {
    let s = '## ' + m.role + '\n\n';
    if (m.think) s += '> 思考：' + m.think.replace(/\n+/g, '\n> ') + '\n\n';
    return s + m.body;
  });
  const text = parts.join('\n\n---\n\n');
  const fp = (s) => crypto.createHash('sha1').update(s || '', 'utf8').digest('hex').slice(0, 10) + ':' + (s || '').length;
  const idm = /\/a\/chat\/s\/([0-9a-zA-Z-]+)/.exec(page.url || '');
  const head = [
    '# ' + ((await ev('document.title')) || 'DeepSeek 对话'),
    '',
    '- 会话：' + (idm ? idm[1] : ''),
    '- 网址：' + page.url,
    '- 抓取：' + new Date().toLocaleString('sv-SE'),
    '- 消息数：' + msgs.length,
    '- 尾部指纹（最近 4 条）：' + fp(msgs.slice(-4).map((m) => m.body).join('\n---\n')),
    '',
    '---',
    '',
  ].join('\n');

  fs.writeFileSync(OUT, head + text + '\n', 'utf8');
  console.log(JSON.stringify({
    out: OUT, messages: msgs.length, users: msgs.filter((m) => m.role === '用户').length,
    chars: text.length, steps, anchorFound: cut >= 0, secs: Math.round((Date.now() - t0) / 1000),
  }));
  ws.close();
})().catch((e) => { console.error('FAIL:', e.message || e); process.exit(1); });
