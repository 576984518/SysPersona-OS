#!/usr/bin/env node
// SysPersona-OS · 本文件：deepseek_sweep.js — 用调试端口把侧边栏里的 DeepSeek 会话逐个抓成 Markdown
/*
 * deepseek_sweep.js — 用调试端口把侧边栏里的 DeepSeek 会话逐个抓成 Markdown
 *
 * 用法：
 *   node deepseek_sweep.js <输出目录> [--limit N] [--dry-run]
 *
 * 做法：复用当前已打开的那个 DeepSeek 页面，依次 Page.navigate 到每个会话，
 *       等消息渲染稳定后抽取「用户 / 助手 / 思考」，写入 deepseek-<会话ID>.md。
 * 注意：会占用那个标签页；抓完停在最后一个会话。
 */

const fs = require('fs');
const path = require('path');

const argv = process.argv.slice(2);
const OUT = path.resolve(argv[0] || 'sessions/2026-11-08/deepseek网页');
let LIMIT = Infinity;
let DRY = false;
// 默认只查最新的 N 个会话：旧会话上下文满了就不再变，往前翻没意义
let RECENT = 10;
let FULL = false;
// 全量模式下，连续 STOP_AFTER 个「末条未变」就提前收工（0 = 不提前停）
let STOP_AFTER = 0;
// 实时/巡检时用来判断"有没有变"的尾部窗口大小（条）
let TAIL_N = 4;
// 只抓当前打开的那个会话：不跳转、不重载，尾部没变就立刻退出
let ACTIVE_ONLY = false;
// 抓之前先重载页面：页面开久了会停止加载更早的消息（实测重载后 132 → 163 条）
let RELOAD = false;
// 每轮把"最新几条"写成一个可读文件，供人和模型随时瞄一眼
let LATEST_PATH = null;
for (let i = 1; i < argv.length; i++) {
  if (argv[i] === '--limit') LIMIT = Number(argv[++i]);
  else if (argv[i] === '--dry-run') DRY = true;
  else if (argv[i] === '--active') ACTIVE_ONLY = true;
  else if (argv[i] === '--reload') RELOAD = true;
  else if (argv[i] === '--latest') LATEST_PATH = argv[++i];
  else if (argv[i] === '--tail-n') TAIL_N = Math.max(1, Number(argv[++i]) || 4);
  else if (argv[i] === '--recent') RECENT = Number(argv[++i]);
  else if (argv[i] === '--full') FULL = true;
  else if (argv[i] === '--stop-after') STOP_AFTER = Number(argv[++i]);
}
const BASE = 'http://127.0.0.1:9222';
fs.mkdirSync(OUT, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const EXTRACT = `(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const vl = document.querySelector('.ds-virtual-list') || document.scrollingElement;
  const cnt = () => document.querySelectorAll('.ds-message').length;
  // 先反复跳顶，把更早的消息全部触发挂载；连续两轮条数不变才算加载完
  let prev = -1, same = 0;
  for (let k = 0; k < 20; k++) {
    vl.scrollTop = 0;
    await sleep(1000);
    const n = cnt();
    if (n === prev) { same++; if (same >= 2) break; } else { same = 0; }
    prev = n;
  }
  const out = [];
  for (const n of document.querySelectorAll('.ds-message')) {
    const isUser = /d29f3d7d/.test((n.className || '').toString());
    const thinkEl = n.querySelector('.ds-think-content');
    const mds = [...n.querySelectorAll('.ds-markdown')].filter((m) => !m.closest('.ds-think-content'));
    const body = mds.length
      ? mds.map((m) => (m.innerText || '').trim()).filter(Boolean).join('\\n\\n')
      : (n.innerText || '').trim();
    if (!body) continue;
    out.push({ role: isUser ? '用户' : '助手', think: thinkEl ? (thinkEl.innerText || '').trim() : '', body });
  }
  const parts = out.map((m) => {
    let s = '## ' + m.role + '\\n\\n';
    if (m.think) s += '> 思考：' + m.think.replace(/\\n+/g, '\\n> ') + '\\n\\n';
    return s + m.body;
  });
  return {
    url: location.href,
    title: document.title || '',
    text: parts.join('\\n\\n---\\n\\n'),
    count: out.length,
    tail: out.slice(-${TAIL_N}).map((m) => m.body).join('\\n---\\n'),
    tailMsgs: out.slice(-${TAIL_N}).map((m) => ({ role: m.role, body: m.body })),
  };
})()`;

const COUNT = `document.querySelectorAll('.ds-message').length`;

class CDP {
  constructor(wsUrl) {
    this.ws = new WebSocket(wsUrl);
    this.id = 0;
    this.pend = new Map();
    this.ready = new Promise((res) => { this.ws.onopen = res; });
    this.ws.onmessage = (ev) => {
      let m;
      try { m = JSON.parse(ev.data); } catch { return; }
      if (m.id && this.pend.has(m.id)) {
        const { ok, no } = this.pend.get(m.id);
        this.pend.delete(m.id);
        m.error ? no(new Error(JSON.stringify(m.error))) : ok(m.result || {});
      }
    };
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((ok, no) => {
      this.pend.set(id, { ok, no });
      this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => { if (this.pend.has(id)) { this.pend.delete(id); no(new Error('timeout ' + method)); } }, 90000);
    });
  }
  async eval(expr) {
    const r = await this.send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error('eval: ' + JSON.stringify(r.exceptionDetails).slice(0, 200));
    return r.result ? r.result.value : undefined;
  }
}

async function listTargets() {
  const r = await fetch(BASE + '/json/list');
  return r.json();
}

async function findChatPage() {
  for (let i = 0; i < 10; i++) {
    const list = await listTargets();
    const p = list.find((t) => t.type === 'page' && /chat\.deepseek\.com/.test(t.url));
    if (p) return p;
    await sleep(800);
  }
  throw new Error('没有打开的 DeepSeek 页面');
}

function loadTail() {
  try { return JSON.parse(fs.readFileSync(path.join(OUT, '_tail.json'), 'utf8')); } catch (_) { return {}; }
}

function saveTail(o) {
  try { fs.writeFileSync(path.join(OUT, '_tail.json'), JSON.stringify(o, null, 1), 'utf8'); } catch (_) {}
}

// 把"最近几条"写成可读文件：每轮都刷，没变化也刷（当作心跳）
function writeLatest(rec, status) {
  if (!LATEST_PATH) return;
  const msgs = rec.tailMsgs || [];
  const body = msgs.length
    ? msgs.map((m) => `### ${m.role}\n\n${String(m.body || '').trim().slice(0, 1500)}`).join('\n\n')
    : '（本轮没有读到消息）';
  const text = [
    '# 最新动态（每分钟自动刷新）',
    '',
    `- 更新时间：${new Date().toLocaleString('sv-SE')}`,
    `- 会话：${rec.title || '?'}`,
    `- 网址：${rec.url || '?'}`,
    `- 消息数：${rec.count || 0}`,
    `- 状态：${status}`,
    '',
    `## 最近 ${msgs.length} 条`,
    '',
    body,
    '',
  ].join('\n');
  try {
    const tmp = LATEST_PATH + '.tmp';
    fs.writeFileSync(tmp, text, 'utf8');
    fs.renameSync(tmp, LATEST_PATH);
  } catch (_) {}
}

function fp(s) {
  const h = require('crypto').createHash('sha1').update(String(s || '')).digest('hex').slice(0, 10);
  return h + ':' + String(s || '').length;
}

function readHead(file) {
  try {
    const old = fs.readFileSync(file, 'utf8');
    const m = /- 末条指纹：(\S+)/.exec(old);
    return { old, tailFp: m ? m[1] : '' };
  } catch (_) {
    return { old: '', tailFp: '' };
  }
}

function writeConv(rec, id) {
  const file = path.join(OUT, `deepseek-${id}.md`);
  const head = [
    `# ${rec.title || 'DeepSeek 对话'}`,
    '',
    `- 会话：${id}`,
    `- 网址：${rec.url}`,
    `- 抓取：${new Date().toLocaleString('sv-SE')}`,
    `- 消息数：${rec.count}`,
    `- 尾部指纹（最近 ${TAIL_N} 条）：${fp(rec.tail)}`,
    '',
    '---',
    '',
  ].join('\n');
  const body = rec.text + '\n';
  // 幂等：正文与已存文件一致就不动它（mtime 不变 → digest 会自动跳过，不重复归档）
  const cur = readHead(file);
  if (cur.old) {
    // 防"残缺快照覆盖完整版"：页面重载时只渲染一部分，条数会从 402 掉到 151。
    // 条数变少一律不写——宁可这轮空转，也不要把已有的完整归档换成残卷。
    const m = /- 消息数：(\d+)/.exec(cur.old);
    const oldCount = m ? parseInt(m[1], 10) : -1;
    if (oldCount >= 0 && rec.count < oldCount) {
      console.log(`[SKIP] ${id} 条数变少 ${oldCount} → ${rec.count}（疑似页面未渲染完，不覆盖）`);
      return null;
    }
    const cut = cur.old.indexOf('\n---\n');
    if (cut >= 0 && cur.old.slice(cut + 5) === body) return null;
  }
  fs.writeFileSync(file, head + body, 'utf8');
  return file;
}

// 只看最下方 N 条：会话按时间追加，末尾这几条没变就说明整段没变，
// 但窗口取 4 而不是 1 —— 能兜住"切回旧会话补了一句"这种只在尾部变的情况
const TAIL = `(() => {
  const nodes = [...document.querySelectorAll('.ds-message')];
  if (!nodes.length) return { url: location.href, title: document.title || '', count: 0, tail: '', tailMsgs: [] };
  const pick = nodes.slice(-${TAIL_N});
  const tailMsgs = [];
  for (const node of pick) {
    const isUser = /d29f3d7d/.test((node.className || '').toString());
    const mds = [...node.querySelectorAll('.ds-markdown')].filter((m) => !m.closest('.ds-think-content'));
    const body = mds.length
      ? mds.map((m) => (m.innerText || '').trim()).filter(Boolean).join('\\n\\n')
      : (node.innerText || '').trim();
    tailMsgs.push({ role: isUser ? '用户' : '助手', body });
  }
  return {
    url: location.href,
    title: document.title || '',
    count: nodes.length,
    tail: tailMsgs.map((m) => m.body).join('\\n---\\n'),
    tailMsgs,
  };
})()`;

async function waitStable(cdp, want = 1) {
  let last = -1, same = 0, lastErr = '';
  for (let i = 0; i < 26; i++) {
    await sleep(500);
    let n = 0;
    try { n = (await cdp.eval(COUNT)) || 0; } catch (e) { n = -1; lastErr = String(e.message).slice(0, 100); }
    if (n >= want && n === last) { same++; if (same >= 2) return n; } else { same = 0; }
    last = n;
  }
  if (last < 1) console.log(`(waitStable 最后状态=${last} ${lastErr})`);
  return last;
}

async function main() {
  const page = await findChatPage();
  const cdp = new CDP(page.webSocketDebuggerUrl);
  await cdp.ready;
  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');
  // 关键：窗口不可见时页面不渲染消息，必须置前 + 模拟焦点
  try { await cdp.send('Emulation.setFocusEmulationEnabled', { enabled: true }); } catch (_) {}
  try { await cdp.send('Page.setWebLifecycleState', { state: 'active' }); } catch (_) {}

  // ── 实时模式：只盯当前这个会话，不跳转（按需刷新）──
  if (ACTIVE_ONLY) {
    const m = /\/a\/chat\/s\/([0-9a-zA-Z-]+)/.exec(page.url || '');
    if (!m) { console.log('当前页面不是会话页，跳过 [SKIP]'); process.exit(0); }
    const id = m[1];
    if (RELOAD) {
      try { await cdp.send('Page.reload', { ignoreCache: false }); } catch (_) {}
      // 等消息重新挂载稳定，否则会抓到空页面
      let prev = -1, same = 0;
      for (let i = 0; i < 40; i++) {
        await sleep(500);
        let n = 0;
        try { n = (await cdp.eval(COUNT)) || 0; } catch (_) { n = -1; }
        if (n > 0 && n === prev) { if (++same >= 2) break; } else { same = 0; }
        prev = n;
      }
      console.log(`已刷新页面（挂载 ${prev} 条）`);
    }
    const tail = await cdp.eval(TAIL);
    if (!tail || !tail.count) {
      writeLatest({ title: tail ? tail.title : '', url: tail ? tail.url : '', count: 0 }, '页面未渲染');
      console.log('页面尚未渲染出消息，跳过 [SKIP]'); process.exit(0);
    }
    const tails = loadTail();
    const prev = tails[id];
    // 指纹 + 总条数都得一致：重载后可能只是把"更早的历史"补进来，尾部没变但内容变多了
    const sameTail = prev && typeof prev === 'object' && prev.fp === fp(tail.tail) && prev.n === tail.count;
    if (sameTail) {
      writeLatest(tail, '尾部未变');
      console.log('尾部未变，跳过 [SKIP]'); process.exit(0);
    }
    // 变了：临时撑高视口 + 滚顶加载全历史，抓完把视口和阅读位置还原
    await cdp.send('Emulation.setDeviceMetricsOverride', {
      width: 1280, height: 30000, deviceScaleFactor: 1, mobile: false,
    });
    const rec = await cdp.eval(EXTRACT);
    try { await cdp.send('Emulation.clearDeviceMetricsOverride'); } catch (_) {}
    try {
      await cdp.eval(`(()=>{const v=document.querySelector('.ds-virtual-list')||document.scrollingElement;v.scrollTop=v.scrollHeight;return 1})()`);
    } catch (_) {}
    if (!rec || !rec.text) { console.log('抓取失败，留待下次 [FAIL]'); process.exit(1); }
    const f = writeConv(rec, id);
    tails[id] = { fp: fp(rec.tail), n: tail.count };
    saveTail(tails);
    writeLatest(rec, f ? '已更新' : '正文未变');
    console.log(`${f ? '已更新 [UPDATED]' : '正文未变 [SKIP]'}：${rec.count} 条 / ${rec.text.length} 字  ← ${rec.title.slice(0, 30)}`);
    process.exit(0);
  }

  // 关键：把视口撑高，虚拟列表就会一次性挂载全部消息，不用滚动
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: 1280, height: 30000, deviceScaleFactor: 1, mobile: false,
  });

  const hrefs = await cdp.eval(
    `[...document.querySelectorAll('a[href*="/a/chat/s/"]')].map((a) => a.getAttribute('href'))`
  );
  const ids = [...new Set((hrefs || []).map((h) => (/\/a\/chat\/s\/([0-9a-zA-Z-]+)/.exec(h || '') || [])[1]).filter(Boolean))];
  console.log(`发现 ${ids.length} 个会话，输出到 ${OUT}`);
  if (DRY) { console.log(ids.join('\n')); return; }

  // 当前打开的会话（可能不止一个标签）最后抓：它们可能正在生成，先抓完其他再回来取最新
  const targets = await listTargets();
  const activeIds = new Set();
  for (const t of targets) {
    if (t.type !== 'page' || !/chat\.deepseek\.com/.test(t.url)) continue;
    const m = /\/a\/chat\/s\/([0-9a-zA-Z-]+)/.exec(t.url);
    if (m) activeIds.add(m[1]);
  }
  let order = ids.filter((x) => !activeIds.has(x));
  order = order.concat(ids.filter((x) => activeIds.has(x)));
  // 默认模式：只看最新的 N 个（侧边栏本身就是按时间倒序）
  const poolSize = order.length;
  if (!FULL) order = order.slice(0, Math.max(RECENT, activeIds.size));
  if (LIMIT !== Infinity) order = order.slice(0, LIMIT);
  console.log(
    FULL
      ? `模式：全量（共 ${poolSize} 个${STOP_AFTER ? `，连续 ${STOP_AFTER} 个未变即提前停` : ''}）`
      : `模式：最近 ${order.length} 个（旧会话不再变化；全量请加 --full）`
  );
  if (activeIds.size) console.log(`当前打开的会话 ${activeIds.size} 个，安排在最后重抓：${[...activeIds].join(', ')}`);

  let ok = 0, same = 0, streak = 0;
  const pending = [];
  const tails = loadTail();
  const t0 = Date.now();
  for (let i = 0; i < order.length; i++) {
    const id = order[i];
    const isActive = activeIds.has(id);
    // 全量模式：连续多个未变就认为更早的也不会变了，直接收工
    if (FULL && STOP_AFTER && streak >= STOP_AFTER && !isActive) {
      console.log(`连续 ${streak} 个未变，提前结束（剩余 ${order.length - i} 个留待下次 --full）`);
      break;
    }
    process.stdout.write(`[${i + 1}/${order.length}] ${id}${isActive ? ' [当前对话]' : ''} ... `);
    try {
      let rec = null, n = 0, prevText = '';
      const file = path.join(OUT, `deepseek-${id}.md`);
      const cur = readHead(file);
      const rounds = isActive ? 4 : 1;
      for (let r = 0; r < rounds; r++) {
        try { await cdp.send('Page.bringToFront'); } catch (_) {}
        await cdp.send('Page.navigate', { url: 'https://chat.deepseek.com/a/chat/s/' + id });
        await sleep(1200);
        n = await waitStable(cdp, 1);
        if (!n || n < 1) { rec = null; break; }
        // 先只看最下方一条：和已存的末条指纹一致 → 整段没变，直接下一个
        const tail = await cdp.eval(TAIL);
        if (!tail || !tail.count) { rec = null; break; }
        const cur2 = tails[id];
        const sameT = cur2 && typeof cur2 === 'object'
          && cur2.fp === fp(tail.tail) && cur2.n === tail.count;
        if (sameT) {
          rec = { unchanged: true, count: tail.count };
          break;
        }
        const got = await cdp.eval(EXTRACT);
        if (!got || !got.text) { rec = null; break; }
        rec = got;
        if (!isActive || got.text === prevText) break; // 稳定了
        prevText = got.text;
        await sleep(1500);
      }
      if (!rec) { console.log('空/超时，留待下次'); pending.push(id); continue; }
      if (rec.unchanged) { same++; streak++; console.log(`${rec.count} 条 · 末条未变`); continue; }
      streak = 0;
      if (rec.text.length < 10) { console.log('无正文，留待下次'); pending.push(id); continue; }
      const out = writeConv(rec, id);
      tails[id] = { fp: fp(rec.tail), n: tail.count };
      saveTail(tails);
      if (out) { ok++; console.log(`${n} 条 / ${rec.text.length} 字  ← ${rec.title.slice(0, 26)}`); }
      else { same++; console.log(`${n} 条 / ${rec.text.length} 字  ← 正文未变`); }
    } catch (e) {
      console.log('失败：' + String(e.message).slice(0, 60));
      pending.push(id);
    }
  }
  const mins = ((Date.now() - t0) / 60000).toFixed(1);
  console.log(`完成：更新 ${ok}，未变 ${same}，异常 ${pending.length}，耗时 ${mins} 分钟`);
  if (pending.length) console.log('异常会话（下次重跑会自动补上）：\n  ' + pending.join('\n  '));
  try { await cdp.send('Emulation.clearDeviceMetricsOverride'); } catch (_) {}
  process.exit(0);
}

main().catch(async (e) => {
  console.error('ERR', e.message);
  process.exit(1);
});
