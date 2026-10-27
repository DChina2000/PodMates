/* 面板基础：元素构造、接口、事件推送、路由、布局、弹窗与提示。 */
'use strict';

// ---------- 元素 ----------
function h(sel, attrs, ...kids) {
  const m = sel.match(/^([a-z0-9]+)?((?:\.[\w-]+)*)$/i) || [];
  const el = document.createElement(m[1] || 'div');
  if (m[2]) el.className = m[2].slice(1).replace(/\./g, ' ');
  if (attrs && (typeof attrs !== 'object' || attrs instanceof Node || Array.isArray(attrs))) { kids.unshift(attrs); attrs = null; }
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'class') el.className += (el.className ? ' ' : '') + v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k === 'html') el.innerHTML = v;
    else if (k in el && k !== 'list' && k !== 'form') el[k] = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  add(el, kids);
  return el;
}
function add(el, ...kids) {
  for (const k of kids.flat(10)) {
    if (k == null || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return el;
}
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
function clear(el, ...kids) { el.replaceChildren(); return add(el, kids); }
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

// ---------- 图标（自绘线条图标）----------
const ICONS = {
  home: 'M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z',
  plug: 'M9 2v6M15 2v6M6 8h12v4a6 6 0 0 1-12 0zM12 18v4',
  bot: 'M12 3v3M5 9h14a1 1 0 0 1 1 1v8a3 3 0 0 1-3 3H7a3 3 0 0 1-3-3v-8a1 1 0 0 1 1-1zM9 14h.01M15 14h.01M2 13v3M22 13v3',
  mask: 'M4 5c3 1 5 1 8 0s5-1 8 0v6c0 5-4 9-8 9s-8-4-8-9zM8.5 11h2M13.5 11h2M9.5 15.5c1.5 1 3.5 1 5 0',
  users: 'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM22 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8',
  group: 'M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2zM8 9h8M8 13h5',
  chat: 'M7 8h10M7 12h6M21 12a9 9 0 0 1-13.4 7.9L3 21l1.1-4.6A9 9 0 1 1 21 12z',
  brain: 'M9.5 3a3 3 0 0 0-3 3v.2A3.5 3.5 0 0 0 4 9.5c0 1 .4 1.9 1 2.5a3.5 3.5 0 0 0 1.5 6A3 3 0 0 0 12 19.5V5.5A2.5 2.5 0 0 0 9.5 3zM14.5 3a3 3 0 0 1 3 3v.2A3.5 3.5 0 0 1 20 9.5c0 1-.4 1.9-1 2.5a3.5 3.5 0 0 1-1.5 6A3 3 0 0 1 12 19.5',
  list: 'M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01',
  chart: 'M3 3v18h18M7 15l4-4 3 3 6-6',
  gear: 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z',
  play: 'M6 4l14 8-14 8z', stop: 'M6 6h12v12H6z', plus: 'M12 5v14M5 12h14', x: 'M18 6 6 18M6 6l12 12',
  trash: 'M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14', copy: 'M9 9h11v11H9zM5 15H4V4h11v1',
  edit: 'M12 20h9M16.5 3.5a2.1 2.1 0 1 1 3 3L7 19l-4 1 1-4z', grip: 'M9 5h.01M9 12h.01M9 19h.01M15 5h.01M15 12h.01M15 19h.01',
  check: 'M20 6 9 17l-5-5', refresh: 'M21 12a9 9 0 1 1-2.6-6.4M21 3v6h-6', history: 'M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5M12 7v5l3 3',
  eye: 'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z', send: 'M22 2 11 13M22 2l-7 20-4-9-9-4z',
  undo: 'M3 7v6h6M3 13a9 9 0 1 0 3-7', download: 'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3',
  upload: 'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12', search: 'M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.3-4.3',
  more: 'M5 12h.01M12 12h.01M19 12h.01', info: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 16v-4M12 8h.01',
  alert: 'M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0zM12 9v4M12 17h.01',
  chev: 'M9 18l6-6-6-6', back: 'M15 18l-6-6 6-6', qr: 'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h3v3h-3zM20 14v.01M14 20h.01M17 20h4v-3',
  link: 'M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7',
  sun: 'M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10zM12 1v2M12 21v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M1 12h2M21 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4',
  moon: 'M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z', lock: 'M5 11h14v10H5zM8 11V7a4 4 0 0 1 8 0v4',
  shield: 'M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z', calendar: 'M4 5h16v16H4zM4 10h16M9 3v4M15 3v4M8 14h2M14 14h2M8 18h2', star: 'M12 2l3 7 7 .6-5.3 4.6 1.6 7.1L12 17.6 5.7 21.3l1.6-7.1L2 9.6 9 9z',
};
function icon(name, cls = '') {
  const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('class', 'i ' + cls);
  const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  p.setAttribute('d', ICONS[name] || ICONS.info); s.append(p);
  return s;
}

// ---------- 提示、弹窗 ----------
function toast(text, kind = '') {
  const t = h('div.toast', { class: kind }, text);
  $('#toasts').append(t);
  setTimeout(() => t.remove(), kind === 'err' ? 5000 : text.length > 40 ? 8000 : 2200);
}
function modal(title, body, { foot = null, wide = false, onclose = null } = {}) {
  const close = () => { ov.remove(); onclose && onclose(); };
  const ov = h('div.overlay', { onmousedown: e => { if (e.target === ov) close(); } },
    h('div.modal', { class: wide ? 'wide' : '' },
      h('div.modal-h', h('h3', title), h('button.ghost.icon', { onclick: close, title: '关闭' }, icon('x'))),
      h('div.modal-b', body),
      foot && h('div.modal-f', typeof foot === 'function' ? foot(close) : foot)));
  document.body.append(ov);
  return close;
}
function confirmBox(text, okText = '确定', danger = false) {
  return new Promise(res => {
    let done = false;
    const close = modal('请确认', h('div', text), {
      foot: c => [h('button', { onclick: () => c() }, '取消'),
        h('button', { class: danger ? 'danger' : 'primary', onclick: () => { done = true; c(); } }, okText)],
      onclose: () => res(done),
    });
  });
}
function promptBox(title, label, value = '') {
  return new Promise(res => {
    let val = null;
    const inp = h('input', { value });
    const close = modal(title, h('label.field', h('span', label), inp), {
      foot: c => [h('button', { onclick: () => c() }, '取消'), h('button.primary', { onclick: () => { val = inp.value; c(); } }, '确定')],
      onclose: () => res(val),
    });
    setTimeout(() => inp.focus(), 50);
    inp.addEventListener('keydown', e => { if (e.key === 'Enter') { val = inp.value; close(); } });
  });
}

// ---------- 接口 ----------
async function api(method, url, body) {
  const opt = { method, headers: {} };
  if (body instanceof FormData) opt.body = body;
  else if (body !== undefined) { opt.body = JSON.stringify(body); opt.headers['Content-Type'] = 'application/json'; }
  const r = await fetch(url, opt);
  if (r.status === 401) { App.showLogin(); throw new Error('请先登录'); }
  const ct = r.headers.get('content-type') || '';
  const data = ct.includes('json') ? await r.json() : ct.includes('text') ? await r.text() : await r.blob();
  if (!r.ok) { const m = (data && data.error) || (typeof data === 'string' ? data : '出错了'); toast(m, 'err'); throw new Error(m); }
  return data;
}
const GET = u => api('GET', u), PUT = (u, b) => api('PUT', u, b), POST = (u, b) => api('POST', u, b ?? {}), DEL = u => api('DELETE', u);

// ---------- 全局状态 ----------
const S = { boot: null, page: 'home', sub: [], listeners: {} };
function on(kind, fn) { (S.listeners[kind] ||= new Set()).add(fn); return () => S.listeners[kind].delete(fn); }
function fire(kind, data) { (S.listeners[kind] || []).forEach(fn => { try { fn(data); } catch (e) { console.error(e); } }); }
const B = () => S.boot;
const byId = (arr, id) => (arr || []).find(x => x.id === id);
const PLAT_MAIN = { qq: 'QQ', wechat: '微信', discord: 'Discord' };
const PLAT = { ...PLAT_MAIN, telegram: 'Telegram', line: 'LINE', reddit: 'Reddit', youtube: 'YouTube', x: 'X', bilibili: 'B站', tieba: '贴吧', weibo: '微博', demo: '体验' };
const PLAT_LETTER = { qq: 'Q', wechat: '微', discord: 'D', telegram: 'T', line: 'L', reddit: 'R', youtube: 'Y', x: 'X', bilibili: 'B', tieba: '贴', weibo: '博', demo: '试' };
const plat = p => h('span.plat', { class: p, title: PLAT[p] }, PLAT_LETTER[p] || '?');
const STATUS = { online: '在线', starting: '连接中', waiting_login: '等待扫码', confirm: '等待确认', error: '出错', stopped: '已停止', authorized: '已授权' };
const fmtTime = ts => { const d = new Date(ts * 1000); const p = n => String(n).padStart(2, '0'); return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`; };
const fmtNum = n => n >= 10000 ? (n / 10000).toFixed(n >= 1e5 ? 0 : 1) + '万' : String(n || 0);
const debounce = (fn, ms = 500) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
// 主模型实际用的那家服务商有没有密钥：不是固定查 default；环境变量提供的密钥、本机服务（不需要密钥）也算有
function mainKeyMissing(endpoints, items) {
  const id = ((items || []).find(x => x.key === 'main_endpoint') || {}).value || 'default';
  const ep = byId(endpoints, id);
  return !ep || !(ep.has_key || ep.env_set || ep.keyless);
}
async function reloadBoot() {
  S.boot = await GET('/api/bootstrap');
  S.boot.people = (S.boot.people || []).slice().sort((a, b) => (b.self ? 1 : 0) - (a.self ? 1 : 0));  // 用户卡排在最前面（Array.sort 是稳定的，别的卡顺序不变）
  fire('boot', S.boot); return S.boot;
}
// 「用户」是系统里固定的一张人物卡：凡是列出人物的地方显示成「用户 · 名字」（名字没填时只显示「用户」）。只改显示，不改数据。
function pLabel(p) { return p.self ? '用户' + ((p.name || '').trim() ? ' · ' + p.name.trim() : '') : (p.name || ''); }
function dispText(s) {  // 消息和日志里的「〔名字本人〕」，名字是用户卡的名字时显示成「〔用户 · 名字〕」
  const u = ((S.boot || {}).people || []).find(p => p.self && (p.name || '').trim());
  return u && typeof s === 'string' ? s.split(`〔${u.name.trim()}本人〕`).join(`〔用户 · ${u.name.trim()}〕`) : s;
}
// 用户卡按「身份」给出可点选的常用称呼，也可以自己添加；其他身份没有预设
const CALL_PRESETS = { '她妈': ['妈', '妈妈', '老妈', '母上', '母上大人'], '她爸': ['爸', '爸爸', '老爸', '父上', '父上大人'] };
function callPresets(identity) { return CALL_PRESETS[(identity || '').trim()] || []; }
function roomName(key) { const c = byId(B().chats, key); return (c && c.name) || key; }
function scopeLabel(sc) {
  if (!sc) return '内置默认';
  if (sc === 'global') return '全局默认';
  const parts = sc.split('|').map(p => {
    const [t, ...rest] = p.split(':');
    if (t === 'platform') return '平台 ' + PLAT[rest[0]];
    if (t === 'role') return '角色「' + ((byId(B().roles, rest.join(':')) || {}).name || '?') + '」';
    if (t === 'chat') return '群「' + roomName(rest.join(':')) + '」';
    if (t === 'acct') return '账号「' + accountTitle(byId(B().accounts, rest.join(':')) || { name: rest.join(':'), platform: '' }) + '」';
    return p;
  });
  return parts.join(' · ');
}

// ---------- 主题 ----------
function applyTheme(t) {
  const dark = t === 'dark' || (t !== 'light' && matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
}
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => applyTheme(S.theme));

// ---------- 事件推送（WebSocket，不轮询）----------
function connectEvents() {
  const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/api/events');
  ws.onmessage = e => { const ev = JSON.parse(e.data); fire(ev.type, ev.data); };
  ws.onclose = () => setTimeout(connectEvents, 3000);
}
on('account', d => {
  const a = byId(B()?.accounts, d.id);
  if (a) Object.assign(a, { status: d.status, detail: d.detail, self_id: d.self_id, self_name: d.self_name, pending: d.pending || null });
});

// ---------- 页面与路由 ----------
const PAGES = [];
function page(id, title, iconName, render, opt = {}) { PAGES.push({ id, title, icon: iconName, render, ...opt }); }
function go(path) { location.hash = '#/' + path; }

const App = {
  async start() {
    let st;
    try { st = await GET('/api/state'); } catch (e) { return; }
    S.theme = st.theme; applyTheme(st.theme);
    await I18N.init(st.lang);
    if (st.has_password && !st.authed) return this.showLogin(st);  // 没设密码时本机直接进（外部访问已被后台挡住）
    await reloadBoot();
    if (B().needs_setup) return Wizard.open();
    this.layout();
    connectEvents();
    setTimeout(() => typeof updateBanner === 'function' && updateBanner(), 1500);
    window.addEventListener('hashchange', () => this.route());
    this.route();
    // 有微信账号在等你确认「这是机器人小号吗」（比如面板重开之前就弹过）：补弹
    setTimeout(() => typeof wechatConfirm === 'function' && B().accounts.filter(a => a.status === 'confirm' && a.pending).forEach(wechatConfirm), 800);
  },
  showLogin() {
    const pw = h('input', { type: 'password', placeholder: '面板密码', autocomplete: 'current-password' });
    const submit = async () => {
      try { await POST('/api/login', { password: pw.value }); location.reload(); } catch (e) { pw.select(); }
    };
    pw.addEventListener('keydown', e => e.key === 'Enter' && submit());
    clear($('#root'), h('div.center-page', h('div.card.login.col.gap16',
      h('div.row.gap12', h('img', { src: 'icon-192.png', width: 40, height: 40 }), h('div', h('h2', '群聊机器人面板'), h('div.muted.small', '请输入面板密码'))),
      pw, h('button.primary.big', { onclick: submit }, '登录'),
      h('div.faint.small', '忘了密码：在电脑上从桌面图标打开面板（本机窗口免密码），到「设置 → 访问与安全」改密码。'))));
    setTimeout(() => pw.focus(), 50);
  },
  layout() {
    PAGES.sort((a, b) => (a.order || 0) - (b.order || 0));
    const nav = h('nav.nav'), bnav = h('nav.bottomnav');
    for (const p of PAGES.filter(p => !p.hidden)) nav.append(h('a', { href: '#/' + p.id, 'data-p': p.id }, icon(p.icon), p.title));
    for (const id of ['home', 'chat', 'roles', 'people', 'more']) {
      const p = PAGES.find(x => x.id === id);
      bnav.append(h('a', { href: '#/' + id, 'data-p': id }, icon(p.icon), p.short || p.title));
    }
    this.title = h('h1');
    this.statusPill = h('div.row');
    this.view = h('section.view');
    clear($('#root'), h('div.shell',
      h('aside.side', h('div.brand', h('img', { src: 'icon-192.png' }), '群聊机器人面板'), nav,
        h('div.foot.faint.small', 'v' + B().version)),
      h('div.main', h('header.top', this.title, h('div.grow'), this.statusPill), this.view)), bnav);
    this.renderStatus();
    on('account', () => this.renderStatus());
  },
  renderStatus() {
    const acc = B().accounts;
    const online = acc.filter(a => a.status === 'online').length;
    const btn = h('button', { class: B().running ? '' : 'primary', onclick: async () => {
      btn.disabled = true;
      try { await POST(B().running ? '/api/engine/stop' : '/api/engine/start'); B().running = !B().running; await reloadBoot(); }
      finally { btn.disabled = false; this.renderStatus(); }
    } }, icon(B().running ? 'stop' : 'play'), h('span.desktop-only', B().running ? '停止机器人' : '启动机器人'));
    clear(this.statusPill,
      h('span.badge', { class: B().running ? (online ? 'ok' : 'warn') : '' }, h('span.dot', { class: B().running ? (online ? 'online' : 'starting') : '' }),
        B().running ? `运行中 ${online}/${acc.length}` : '已停止'), btn);
  },
  async route() {
    const parts = (location.hash.replace(/^#\/?/, '') || 'home').split('/').map(decodeURIComponent);
    const p = PAGES.find(x => x.id === parts[0]) || PAGES[0];
    S.page = p.id; S.sub = parts.slice(1);
    $$('[data-p]').forEach(a => a.classList.toggle('on', a.dataset.p === p.id || (a.dataset.p === 'more' && p.more)));
    this.title.textContent = p.title;
    (S.cleanup || []).forEach(f => f()); S.cleanup = [];
    const v = this.view;
    clear(v, h('div.empty.faint', '加载中…'));
    try { await p.render(v, S.sub); } catch (e) { console.error(e); clear(v, h('div.notice.bad', icon('alert'), '页面出错：' + e.message)); }
    window.scrollTo(0, 0);
  },
};
function live(kind, fn) { S.cleanup.push(on(kind, fn)); }
// 向导里打开的弹窗做完事后刷新向导，平时刷新当前页
function refresh() { return S.wizard ? Wizard.render() : App.route(); }
