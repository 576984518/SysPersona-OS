#!/usr/bin/env node
// SysPersona-OS · 本文件：webwatch.js — 跟着浏览器自动抓（CDP，零依赖）
/*
 * webwatch.js — 跟着浏览器自动抓（CDP，零依赖）
 *
 * 原理：浏览器带 --remote-debugging-port 启动后，脚本通过 CDP 监听网络响应，
 *       把聊天接口返回的 JSON 抽成纯文本，逐条追加到本地 jsonl。
 *       抓的是网络层，不受网页虚拟列表影响——没滚到的地方也能拿到。
 *
 * 一次性准备：给浏览器快捷方式加参数
 *   msedge.exe --remote-debugging-port=9222
 *
 * 用法：
 *   node webwatch.js --port 9222 --out sessions/web.jsonl --match "/api/"
 *   node webwatch.js --check          # 只看浏览器有没有开调试端口
 */

const fs = require('fs');
const path = require('path');

const args = process.argv.slice(2);
const opt = { port: 9222, out: 'sessions/web.jsonl', match: '/api/', check: false };
for (let i = 0; i < args.length; i++) {
  if (args[i] === '--check') opt.check = true;
  else if (args[i].startsWith('--')) opt[args[i].slice(2)] = args[++i];
}
const base = `http://127.0.0.1:${opt.port}`;

async function getJSON(url) {
  const r = await fetch(url);
  return r.json();
}

function walkText(node, out, depth = 0) {
  if (depth > 12 || node == null) return;
  if (typeof node === 'string') {
    if (node.length >= 8 && !/^https?:\/\//.test(node)) out.push(node);
    return;
  }
  if (Array.isArray(node)) { for (const v of node) walkText(v, out, depth + 1); return; }
  if (typeof node === 'object') {
    for (const [k, v] of Object.entries(node)) {
      if (/^(id|uuid|createdAt|updatedAt|role|type|model|index|status|finish_reason)$/i.test(k)) continue;
      walkText(v, out, depth + 1);
    }
  }
}

const seen = new Set();
function append(rec) {
  const key = rec.url + '|' + rec.text.slice(0, 200);
  if (seen.has(key)) return false;
  seen.add(key);
  fs.mkdirSync(path.dirname(opt.out), { recursive: true });
  fs.appendFileSync(opt.out, JSON.stringify(rec) + '\n', 'utf8');
  return true;
}

function connect(page) {
  return new Promise((resolve) => {
    const ws = new WebSocket(page.webSocketDebuggerUrl);
    let id = 0;
    const send = (method, params = {}) => ws.send(JSON.stringify({ id: ++id, method, params }));
    const pend = new Map();

    ws.onopen = () => {
      send('Network.enable');
      send('Runtime.enable');
      console.log('attached:', page.title || page.url);
    };
    ws.onmessage = async (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.id && pend.has(msg.id)) { pend.get(msg.id)(msg.result || {}); pend.delete(msg.id); return; }
      if (msg.method === 'Network.responseReceived') {
        const r = msg.params.response;
        if (!/json/.test(r.mimeType || '')) return;
        if (opt.match && !r.url.includes(opt.match)) return;
        const res = await new Promise((ok) => { pend.set(++id, ok); send('Network.getResponseBody', { requestId: msg.params.requestId }); });
        if (!res.body) return;
        let body = res.body;
        if (res.base64Encoded) body = Buffer.from(body, 'base64').toString('utf8');
        let json;
        try { json = JSON.parse(body); } catch { return; }
        const out = [];
        walkText(json, out);
        const text = out.join('\n').trim();
        if (text.length < 20) return;
        if (append({ t: Date.now(), url: r.url, text })) {
          console.log('saved %d chars <- %s', text.length, r.url.slice(0, 80));
        }
      }
    };
    ws.onclose = () => resolve();
    ws.onerror = () => resolve();
  });
}

async function main() {
  let version;
  try { version = await getJSON(base + '/json/version'); }
  catch (e) {
    console.error('浏览器调试端口没有开（%s）。用带 --remote-debugging-port=%d 的方式启动浏览器。', base, opt.port);
    process.exit(2);
  }
  console.log('browser:', version.Browser);
  if (opt.check) process.exit(0);

  const attached = new Set();
  const tick = async () => {
    let list = [];
    try { list = await getJSON(base + '/json/list'); } catch { return; }
    for (const p of list) {
      if (p.type !== 'page' || attached.has(p.id)) continue;
      attached.add(p.id);
      connect(p);
    }
  };
  await tick();
  setInterval(tick, 5000);
  console.log('watching… 新开的页面会自动接入；Ctrl+C 结束。');
}

main();
