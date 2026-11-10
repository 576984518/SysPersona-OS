#!/usr/bin/env node
/*
 * webdump.js — 通过 CDP 抓 DeepSeek deepseek网页，存成可读 Markdown（零依赖）
 *
 * 前提：浏览器带 --remote-debugging-port=9222 启动。
 *
 * 用法：
 *   node webdump.js sessions/2026-11-08/deepseek网页
 *   node webdump.js <输出目录> --port 9222 --interval 2000
 *
 * 行为：
 *   1) 每 2 秒抓一次页面正文（含 URL / 标题），内容变了才写文件；
 *   2) 每个会话一个文件：deepseek-<会话ID>.md（新对话未分配 ID 时用 draft）；
 *   3) 同时把命中的 JSON 响应原样追加到 _raw/<会话ID>.jsonl，便于回溯原文；
 *   4) 手动切换会话时，自动识别新 URL 并另起文件，互不覆盖。
 */

const fs = require('fs');
const path = require('path');

const args = process.argv.slice(2);
const opt = { out: args[0] || 'sessions/2026-11-08/deepseek网页', port: 9222, interval: 2000 };
for (let i = 0; i < args.length; i++) {
  if (args[i] === '--port') opt.port = Number(args[++i]);
  else if (args[i] === '--interval') opt.interval = Number(args[++i]);
}
const base = `http://127.0.0.1:${opt.port}`;
const OUT = path.resolve(opt.out);
const RAW = path.join(OUT, '_raw');
fs.mkdirSync(OUT, { recursive: true });
fs.mkdirSync(RAW, { recursive: true });

const TARGET = /chat\.deepseek\.com/;
const lastHash = new Map();

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function getJSON(url) {
  const r = await fetch(url);
  return r.json();
}

function convIdOf(url) {
  const m = /\/a\/chat\/s\/([0-9a-zA-Z-]+)/.exec(url || '');
  return m ? m[1] : null;
}

function isChatUrl(url) {
  return TARGET.test(url || '') && !/sign_in|login|signup/i.test(url);
}

function stamp() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

const EXPR = `(() => {
  const nodes = [...document.querySelectorAll('.ds-message')];
  const msgs = [];
  for (const n of nodes) {
    const isUser = /d29f3d7d/.test((n.className || '').toString());
    const thinkEl = n.querySelector('.ds-think-content');
    const mds = [...n.querySelectorAll('.ds-markdown')].filter((m) => !m.closest('.ds-think-content'));
    const body = mds.length
      ? mds.map((m) => (m.innerText || '').trim()).filter(Boolean).join('\\n\\n')
      : (n.innerText || '').trim();
    if (!body) continue;
    msgs.push({
      role: isUser ? '用户' : '助手',
      think: thinkEl ? (thinkEl.innerText || '').trim() : '',
      body,
    });
  }
  const parts = msgs.map((m) => {
    let s = '## ' + m.role + '\\n\\n';
    if (m.think) s += '> 思考：' + m.think.replace(/\\n+/g, '\\n> ') + '\\n\\n';
    return s + m.body;
  });
  return { url: location.href, title: document.title || '', text: parts.join('\\n\\n---\\n\\n'), count: msgs.length };
})()`;

function writeSnapshot(rec, pageId) {
  const id = convIdOf(rec.url) || `draft-${pageId}`;
  const file = path.join(OUT, `deepseek-${id}.md`);
  const body = rec.text || '';
  const head = [
    `# DeepSeek deepseek网页 ${id}`,
    '',
    `- 网址：${rec.url}`,
    `- 标题：${rec.title}`,
    `- 抓取：${stamp()}`,
    '',
    '---',
    '',
  ].join('\n');
  fs.writeFileSync(file, head + body + '\n', 'utf8');
  console.log(`[${stamp()}] 写入 ${path.basename(file)}  ${body.length} 字`);
}

function attach(page) {
  return new Promise((resolve) => {
    let ws;
    try { ws = new WebSocket(page.webSocketDebuggerUrl); } catch (e) { return resolve(); }
    let id = 0;
    const send = (method, params = {}) => {
      try { ws.send(JSON.stringify({ id: ++id, method, params })); } catch (_) {}
      return id;
    };
    const pend = new Map();
    let closed = false;
    const finish = () => { if (!closed) { closed = true; try { ws.close(); } catch (_) {} resolve(); } };

    ws.onopen = () => {
      send('Network.enable');
      send('Runtime.enable');
      console.log('已接入页面:', page.url.slice(0, 90));
      const tick = async () => {
        while (!closed) {
          const rid = send('Runtime.evaluate', {
            expression: EXPR, returnByValue: true, awaitPromise: false,
          });
          const res = await new Promise((ok) => { pend.set(rid, ok); setTimeout(() => ok(null), 8000); });
          const v = res && res.result && res.result.result && res.result.result.value;
          if (v && v.text && isChatUrl(v.url) && v.text.length > 80) {
            const pid = (page.id || 'page').slice(0, 8);
            const key = (convIdOf(v.url) || 'draft-' + pid) + '|' + v.text.length + '|' + v.text.slice(-120);
            if (lastHash.get(pid) !== key) {
              lastHash.set(pid, key);
              writeSnapshot(v, pid);
            }
          }
          await sleep(opt.interval);
        }
      };
      tick();
    };

    ws.onmessage = async (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.id && pend.has(msg.id)) {
        const fn = pend.get(msg.id); pend.delete(msg.id); fn(msg); return;
      }
      if (msg.method === 'Network.responseReceived') {
        const r = msg.params.response;
        if (!TARGET.test(r.url)) return;
        if (!/json|event-stream/.test(r.mimeType || '')) return;
        const rid = send('Network.getResponseBody', { requestId: msg.params.requestId });
        const res = await new Promise((ok) => { pend.set(rid, ok); setTimeout(() => ok(null), 5000); });
        const b = res && res.result;
        if (!b || !b.body) return;
        let body = b.body;
        if (b.base64Encoded) body = Buffer.from(body, 'base64').toString('utf8');
        if (body.length < 20) return;
        const id = convIdOf(r.url) || 'draft';
        const f = path.join(RAW, `deepseek-${id}.jsonl`);
        fs.appendFileSync(f, JSON.stringify({ t: Date.now(), url: r.url, body }) + '\n', 'utf8');
        console.log(`  raw + ${body.length} 字节 ← ${r.url.slice(0, 70)}`);
      }
    };

    ws.onclose = finish;
    ws.onerror = finish;
  });
}

async function main() {
  let v;
  try { v = await getJSON(base + '/json/version'); }
  catch (e) {
    console.error('调试端口没开。用下面方式启动浏览器后重试：');
    console.error('  msedge.exe --remote-debugging-port=9222');
    process.exit(2);
  }
  console.log('浏览器:', v.Browser);
  console.log('输出目录:', OUT);
  console.log(`监控中（每 ${opt.interval}ms 抓一次，切换会话会自动新建文件）Ctrl+C 结束。`);

  const attached = new Set();
  const tick = async () => {
    let list = [];
    try { list = await getJSON(base + '/json/list'); } catch { return; }
    for (const p of list) {
      if (p.type !== 'page' || !TARGET.test(p.url)) continue;
      if (attached.has(p.id)) continue;
      attached.add(p.id);
      attach(p).finally(() => attached.delete(p.id));
    }
  };
  await tick();
  setInterval(tick, 3000);
}

main();
