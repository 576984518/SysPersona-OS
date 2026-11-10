// 从底部往上小步采集，直到撞上"归档末条"锚点为止；输出新增消息
const CDP = 'http://127.0.0.1:9222';
const OUT = process.argv[2];
const ANCHOR = (() => {
  const a = process.argv[3] || '';
  try { if (a.endsWith('.txt')) return require('fs').readFileSync(a, 'utf8').trim(); } catch (_) {}
  return a;
})();   // 归档末条的前 80 字（去空白）
const MAXSTEPS = parseInt(process.argv[4] || '400', 10);
const DELTA = parseInt(process.argv[5] || '3000', 10);
const sleep = ms => new Promise(r => setTimeout(r, ms));

// ── 选择器表（2026-11-10「空块」事故后加）──────────────────────────────
// 规矩：旧选择器**排在第一位**，命中就继续用旧的；只有旧的不命中才往下兜底。
// 是「兼容」不是替换——每次读都把命中来源带回 Node（输出 JSON 的 selector 字段），
// 下次改版一眼就能看出这次用的是旧结构还是兜底结构。
const MSG_SELS = [
  ['.ds-message', 'ds-message'],
  ['[class~="ds-message"]', 'css-ds-message'],
  ['[class*="ds-message"]', 'css-contains-ds-message'],
  ['[data-testid*="message"]', 'data-testid-message'],
];
const MD_SELS = [
  ['.ds-markdown', 'ds-markdown'],
  ['[class*="ds-markdown"]', 'css-ds-markdown'],
  ['[class*="markdown-body"]', 'css-markdown-body'],
];
const THINK_SELS = [
  ['.ds-think-content', 'ds-think-content'],
  ['[class*="think-content"]', 'css-think-content'],
];
// 图片/附件：虚拟列表里 <img> 只有进视口才挂上，离屏时只剩占位 div（2026-11-10 实测：
// 用户发截图的那条消息 innerText 为空、DOM 里没有 <img>，只剩 div.cc804671 / div.eafda4ae）。
// 只数 <img> 就会得到「图片×0」，再被当成空块——这就是「空块」事故的真身。
const MEDIA_SELS = [
  ['img, picture, canvas, video', 'media-tag'],
  ['[class*="cc804671"]', 'ds-image-box'],
  ['[class*="eafda4ae"]', 'ds-image-wrap'],
  ['[style*="background-image"], [class*="image"], [class*="Image"], [class*="attach"], [class*="file"]', 'media-class'],
];

const ROWJS = `(() => {
  const MSG = ${JSON.stringify(MSG_SELS)};
  const MD = ${JSON.stringify(MD_SELS)};
  const THINK = ${JSON.stringify(THINK_SELS)};
  const MEDIA = ${JSON.stringify(MEDIA_SELS)};
  const clean = (s) => (s || '').replace(/\\u200b/g, '').replace(/\\u00a0/g, ' ').replace(/\\r/g, '').trim();
  // innerText 依赖排版：后台标签页 / 还没布局的节点会返回空串，退回 textContent
  const txt = (el) => clean(el.innerText) || clean(el.textContent);
  const pick = (root, table) => {
    for (const [s, name] of table) {
      const q = [...root.querySelectorAll(s)];
      if (q.length) return { list: q, src: name };
    }
    return { list: [], src: 'none' };
  };
  const pickCount = (root, table) => {
    for (const [s, name] of table) {
      const q = root.querySelectorAll(s);
      if (q.length) return { n: q.length, src: name };
    }
    return { n: 0, src: 'none' };
  };
  const msg = pick(document, MSG);
  const rows = [];
  let mdSrc = 'none';
  for (const n of msg.list) {
    const isUser = /d29f3d7d/.test((n.className || '').toString());
    const thinkEl = pick(n, THINK).list[0] || null;
    const mdPicked = pick(n, MD);
    if (mdPicked.list.length && mdSrc === 'none') mdSrc = mdPicked.src;
    const mds = mdPicked.list.filter((m) => !m.closest('.ds-think-content') && !m.closest('[class*="think-content"]'));
    let src = 'none';
    let body = mds.length ? mds.map(txt).filter(Boolean).join('\\n\\n') : '';
    if (body) src = 'markdown';
    if (!body) { body = txt(n); if (body) src = 'message-text'; }
    // 图片/附件：<img> 挂不上时（离屏懒挂载）退回占位容器的数量
    const media = pickCount(n, MEDIA);
    const imgs = n.querySelectorAll('img, picture, canvas, video').length;
    const weak = imgs ? 0
      : n.querySelectorAll('[style*="background-image"], [class*="image"], [class*="Image"], [class*="attach"], [class*="file"]').length;
    const hasMedia = imgs > 0 || media.n > 0;
    let empty = false;
    if (!body) {
      body = hasMedia ? '[图片×' + (imgs || media.n) + ']' : '[空块]';
      src = hasMedia ? (imgs ? 'img' : 'img-placeholder') : 'nothing';
      empty = !hasMedia;   // 只有「既没字也没图」才是真空块，图片占位不算空
    }
    rows.push({ role: isUser ? '用户' : '助手', think: thinkEl ? txt(thinkEl) : '', body,
                empty, imgs, weak, mediaSrc: media.src, src });
  }
  const v = [...document.querySelectorAll('.ds-virtual-list')].sort((a, b) => b.scrollHeight - a.scrollHeight)[0];
  const q = (v || document.body).getBoundingClientRect();
  return { rows, msgSelector: msg.src, mdSelector: mdSrc, vis: document.visibilityState,
           st: v ? v.scrollTop : 0, sh: v ? v.scrollHeight : 0,
           x: Math.round(q.x + q.width / 2), y: Math.round(q.y + q.height / 2) };
})()`;

(async () => {
  const list = await (await fetch(CDP + '/json/list')).json();
  // 优先选**已经选中会话**的页签：停在根地址的页签正文是空的（2026-11-10 踩，抓到 0 条）
  const cands = list.filter(t => t.type === 'page' && t.url.includes('chat.deepseek.com'));
  const page = cands.find(t => /\/a\/chat\/s\//.test(t.url)) || cands[0];
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let id = 0; const pend = new Map();
  ws.onmessage = e => { const m = JSON.parse(e.data); if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); } };
  const send = (m, p = {}) => new Promise(r => { const i = ++id; pend.set(i, r); ws.send(JSON.stringify({ id: i, method: m, params: p })); });
  // tmo>0 时调用方不会被挂死：页面停在后台时 Runtime.evaluate 里的 awaitPromise 可能永远不回包
  const ev = async (expr, tmo = 0) => {
    const p = send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true })
      .then(r => {
        if (r.result?.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails).slice(0, 200));
        return r.result?.result?.value;
      });
    p.catch(() => {});   // 超时后的迟到拒绝别变成 unhandled rejection（会直接杀掉进程）
    if (!tmo) return p;
    return Promise.race([p, sleep(tmo).then(() => null)]);
  };

  await send('Page.enable');
  // 2026-11-10 空块事故真因之一：**标签页在后台时 Chrome 会停掉排版与懒加载**——
  // 实测 visibilityState=hidden 时 requestAnimationFrame 永不触发、图不会挂上、innerText 为空。
  // Emulation.setFocusEmulationEnabled / Page.setWebLifecycleState 都不改 visibilityState，必须 bringToFront。
  const ensureVisible = async () => {
    try { await send('Page.bringToFront'); } catch (_) {}
    try { await send('Emulation.setFocusEmulationEnabled', { enabled: true }); } catch (_) {}
    try { await send('Page.setWebLifecycleState', { state: 'active' }); } catch (_) {}
  };
  // 等渲染：rAF 两次 + 320ms 兜底。**必须和定时器赛跑**：页面 hidden 时 rAF 永不来，
  // 光等 rAF 会把抓取永久挂住（2026-11-10 实测，诊断脚本卡死 9 分钟）。
  const waitRendered = async (ms = 320) => {
    try {
      await ev(`new Promise((r) => {
        let n = 0, done = false;
        const fin = () => { if (!done) { done = true; r(1); } };
        const tick = () => { if (++n >= 2) fin(); else requestAnimationFrame(tick); };
        requestAnimationFrame(tick);
        setTimeout(fin, ${ms});
      })`, ms + 1200);
    } catch (_) {}
    await sleep(120);
  };
  const emptyCount = r => (r && r.rows ? r.rows.filter(x => x.empty).length : 1e9);
  // 读一窗行；若有「空块」（无正文、无 <img>），等一拍再读，取更全的那次
  const stats = { emptySeen: 0, msgSelector: 'none', bodySelector: 'none', vis: '', mounted: 0, repick: 0 };
  const readRows = async () => {
    await waitRendered();
    let best = await ev(ROWJS, 20000);
    if (!best) return null;
    if (best.vis && best.vis !== 'visible') {   // 中途被别的页签顶到后台：拉回前台重读
      stats.repick++;
      await ensureVisible();
      await waitRendered(500);
      const again = await ev(ROWJS, 20000);
      if (again && emptyCount(again) < emptyCount(best)) best = again;
    }
    for (let i = 0; i < 2 && emptyCount(best) > 0; i++) {
      await sleep(360);
      const again = await ev(ROWJS, 20000);
      if (again && emptyCount(again) < emptyCount(best)) best = again; else break;
    }
    stats.emptySeen += emptyCount(best) === 1e9 ? 0 : emptyCount(best);
    if (best.msgSelector && best.msgSelector !== 'none') stats.msgSelector = best.msgSelector;
    if (best.mdSelector && best.mdSelector !== 'none') stats.bodySelector = best.mdSelector;
    stats.vis = best.vis || '';
    stats.mounted += (best.rows || []).length;
    stats.mountedMax = Math.max(stats.mountedMax || 0, (best.rows || []).length);
    return best;
  };

  await ensureVisible();
  // 就绪判定必须走同一张选择器表：只认 .ds-message 的话，改版后兜底选择器根本没机会上场
  const countMsgs = async () => {
    const expr = `(() => { const sels = ${JSON.stringify(MSG_SELS.map((x) => x[0]))};
      for (const s of sels) { const n = document.querySelectorAll(s).length; if (n) return n; }
      return 0; })()`;
    return (await ev(expr, 8000)) || 0;
  };
  // 规则（2026-11-09 定）：读之前一律整页重载，别信旧 DOM
  const reloadAndWait = async () => {
    await send('Page.reload', { ignoreCache: false });
    let n = 0;
    for (let t = 0; t < 40; t++) {
      await sleep(400);
      n = await countMsgs();
      if (n > 0) break;
    }
    if (n > 0) { await ensureVisible(); await waitRendered(); }   // 挂上 ≠ 渲染完
    return n;
  };
  // 侧栏导航（页面停在根地址 / 重载后落到落地页时会用到）：一般是最上面一条，不行再试第二条
  const openFromSidebar = async () => {
    const links = (await ev(`(() => [...document.querySelectorAll('a[href*="/a/chat/s/"]')].slice(0,2).map(a => a.href))()`, 8000)) || [];
    for (let i = 0; i < links.length; i++) {
      console.error('  页面没选中会话：开侧栏第 ' + (i + 1) + ' 条 ' + links[i]);
      await send('Page.navigate', { url: links[i] });
      await sleep(2000);
      const n = await reloadAndWait();
      if (n > 0) return n;
    }
    return 0;
  };
  // 页面开着 ≠ 选中了会话（他 2026-11-10 指出的）
  let msgCount = 0;
  if (/\/a\/chat\/s\//.test(page.url)) {
    msgCount = await reloadAndWait();
  }
  // 地址里有会话 id 不等于 DOM 里有正文：重载后落到「开启新对话」落地页也见过（2026-11-10 实测）。
  // 这种情况跟「没选中会话」一样处理——走侧栏，而不是直接报 no-conversation。
  if (!msgCount) msgCount = await openFromSidebar();
  if (!msgCount) {   // 一条都没有 = 没选中会话，交空抓取不如报错
    console.log(JSON.stringify({ anchorFound: false, scanned: 0, collected: 0, new: 0, chars: 0, error: 'no-conversation' }));
    process.exit(1);
  }
  await ensureVisible();
  // 强制回到正常视口：否则虚拟列表可能一次性挂载上千条，窗口对齐全乱
  await send('Emulation.setDeviceMetricsOverride', { width: 1280, height: 888, deviceScaleFactor: 1, mobile: false });
  await sleep(1500);
  // 滚到最底：用真实滚轮往下推，直到位置不再变化
  const geom = async () => ev(`(() => { const v=[...document.querySelectorAll('.ds-virtual-list')].sort((a,b)=>b.scrollHeight-a.scrollHeight)[0]; if(!v) return null; const q=v.getBoundingClientRect(); return {st:v.scrollTop, sh:v.scrollHeight, x:Math.round(q.x+q.width/2), y:Math.round(q.y+q.height/2)}; })()`, 8000);
  let lastSt = -1;
  for (let t = 0; t < 20; t++) {
    const g = await geom();
    if (!g) { await sleep(500); continue; }
    if (g.st === lastSt && g.st + 900 >= g.sh) break;
    lastSt = g.st;
    await send('Input.dispatchMouseEvent', { type: 'mouseWheel', x: g.x, y: g.y, deltaX: 0, deltaY: 12000, modifiers: 0 });
    await sleep(350);
  }
  await waitRendered(500);

  const norm = s => (s || '').replace(/\s+/g, '');
  const anchor = norm(ANCHOR);
  const key = r => r.role + '\u0001' + r.think + '\u0001' + r.body;
  const merge = (acc, W) => {
    let best = 0;
    for (let cand = Math.min(W.length, acc.length); cand > 0; cand--) {
      let ok = true;
      for (let j = 0; j < cand; j++) if (key(W[W.length - cand + j]) !== key(acc[j])) { ok = false; break; }
      if (ok) { best = cand; break; }
    }
    if (best === W.length) return { acc, hit: true };
    return { acc: W.slice(0, W.length - best).concat(acc), hit: best > 0 };
  };

  let cur = await readRows();
  if (!cur) { console.log(JSON.stringify({ anchorFound: false, scanned: 0, collected: 0, new: 0, chars: 0, error: 'read-failed' })); process.exit(1); }
  let acc = cur.rows.slice();
  let steps = 0, found = false, miss = 0;

  if (anchor) {
    for (const r of acc) if (norm(r.body).startsWith(anchor)) { found = true; break; }
  }

  // 历史批量装入的瞬间页面会把 scrollTop 归零再恢复（2026-11-10 实测：st 200256→0、
  // sh 222084→789892、一次挂 501 条）。把这种「归零」当到底，就只抓得到一个窗口。
  const isLoadBurst = (g) => !!(g && cur && cur.st > 0 && g.st === 0 && g.sh > cur.sh * 1.05);

  while (!found && steps < MAXSTEPS) {
    await send('Input.dispatchMouseEvent', { type: 'mouseWheel', x: cur.x, y: cur.y, deltaX: 0, deltaY: -DELTA, modifiers: 0 });
    await sleep(150);
    let g = await readRows();   // 里面已含「等渲染 + 空块重读」
    if (isLoadBurst(g)) {
      for (let i = 0; i < 6; i++) {   // 等它把位置恢复；恢复了就继续往上滚
        await sleep(700);
        const g2 = await readRows();
        if (!g2 || !g2.rows) continue;
        g = g2;
        if (g2.st > 0) break;
      }
    }
    if (!g || !g.rows) { if (++miss >= 3) break; continue; }
    if (g.st === cur.st) {
      await sleep(700);              // 位置没动再等一拍：可能正在装历史
      const g2 = await readRows();
      if (g2 && g2.rows && g2.st !== cur.st) g = g2;
      else { if (++miss >= 3) break; continue; }
    }
    miss = 0;
    cur = g;
    const m = merge(acc, g.rows);
    acc = m.acc;
    steps++;
    if (anchor) for (const r of acc) if (norm(r.body).startsWith(anchor)) { found = true; break; }
    if (!m.hit) console.error('  警告：窗口无重叠，可能跳条 @step', steps);
  }

  // 从锚点处截断：锚点及其之前的是旧内容
  let cut = -1;
  if (anchor) for (let i = 0; i < acc.length; i++) if (norm(acc[i].body).startsWith(anchor)) { cut = i; break; }
  const news0 = cut >= 0 ? acc.slice(cut + 1) : acc;

  // 去重：窗口无重叠时 merge 会把整窗前插，DOM 重排也会重复渲染同一块。
  // 只删「连续 ≥2 条、且与前面某处完全相同的块」，单条重复（用户连发两个"1"）保留。
  const dedupeRuns = (arr) => {
    const keys = arr.map((m) => m.role + '\u0001' + m.think + '\u0001' + m.body);
    const out = [], outKeys = [], seen = new Map();
    let i = 0;
    while (i < arr.length) {
      // 相邻完全相同的长消息：DOM 重排时会连挂多次，直接丢；短消息（用户连发"1"）保留
      if (outKeys.length && outKeys[outKeys.length - 1] === keys[i] && (arr[i].body || '').length > 80) {
        i++;
        continue;
      }
      let matched = 0;
      const maxL = Math.min(12, arr.length - i);
      for (let L = maxL; L >= 2; L--) {
        const cands = seen.get(keys[i]) || [];
        let hit = -1;
        for (const j of cands) {
          if (j + L > outKeys.length) continue;
          let ok = true;
          for (let x = 0; x < L; x++) if (outKeys[j + x] !== keys[i + x]) { ok = false; break; }
          if (ok) { hit = j; break; }
        }
        if (hit >= 0) { matched = L; break; }
      }
      if (matched) { i += matched; continue; }
      seen.set(keys[i], (seen.get(keys[i]) || []).concat(out.length));
      out.push(arr[i]); outKeys.push(keys[i]);
      i++;
    }
    return { list: out, removed: arr.length - out.length };
  };
  const dd = dedupeRuns(news0);
  const news = dd.list;
  if (dd.removed) console.error('  去重：删除重复块 ' + dd.removed + ' 条');

  const crypto = require('crypto');
  const fp = (s) => crypto.createHash('sha1').update(s || '', 'utf8').digest('hex').slice(0, 10) + ':' + (s || '').length;
  const parts = news.map((m) => {
    let s = '## ' + m.role + '\n\n';
    if (m.think) s += '> 思考：' + m.think.replace(/\n+/g, '\n> ') + '\n\n';
    return s + m.body;
  });
  const text = parts.join('\n\n---\n\n');
  // 用**当前真实地址**取会话 id：页面是刚才从侧栏导航过来的，page.url 还是开场那一份（会写成空会话）
  const liveUrl = (await ev('location.href', 8000)) || page.url;
  const idm = /\/a\/chat\/s\/([0-9a-zA-Z-]+)/.exec(liveUrl) || /\/a\/chat\/s\/([0-9a-zA-Z-]+)/.exec(page.url);
  // 选择器来源：命中第一张表 = 旧结构；命中其它 = 兜底。输出成 ds-message|ds-markdown 或 fallback|fallback，
  // 一眼能看出这次抓的是新旧结构（2026-11-10 空块事故后定）。
  const kindOf = (s, primary) => (s === primary ? primary : 'fallback');
  const selTag = kindOf(stats.msgSelector, 'ds-message') + '|' + kindOf(stats.bodySelector, 'ds-markdown');
  const head = [
    `# ${(await ev('document.title')) || 'DeepSeek 对话'}`, '',
    `- 会话：${idm ? idm[1] : ''}`,
    `- 网址：${liveUrl}`,
    `- 抓取：${new Date().toLocaleString('sv-SE')}`,
    `- 消息数：${news.length}`,
    `- 选择器：${selTag}`,
    `- 空块：${news.filter(x => x.empty).length}`,
    `- 尾部指纹（最近 4 条）：${fp(news.slice(-4).map(x => x.body).join('\n---\n'))}`, '', '---', '',
  ].join('\n');
  require('fs').writeFileSync(OUT, head + text + '\n', 'utf8');
  console.log(JSON.stringify({
    anchorFound: cut >= 0, scanned: steps, collected: acc.length, new: news.length,
    dupRemoved: dd.removed, chars: text.length,
    selector: selTag, msgSelector: stats.msgSelector, bodySelector: stats.bodySelector,
    fallback: selTag.includes('fallback'),
    rowsEmpty: stats.emptySeen, emptyInNews: news.filter(x => x.empty).length,
    mountedMax: stats.mountedMax || 0, reachedTop: (cur.st <= 2),
    scrollTop: cur.st, scrollHeight: cur.sh,
    visibility: stats.vis, repick: stats.repick, out: OUT
  }));
  ws.close();
})().catch(e => { console.error('FATAL', e.message || e); process.exit(1); });
