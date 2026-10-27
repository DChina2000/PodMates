/* 总览、账号、角色、群、消息（人工接管）、更多。 */
'use strict';

// ---------- 账号卡片与扫码 ----------
function statusBadge(a) {
  return h('span.row.gap4.small', h('span.dot', { class: a.status }), h('span', STATUS[a.status] || a.status));
}
function accountTitle(a) {
  return a.name || (a.platform === 'qq' ? 'QQ ' + (a.qq || '') : a.self_name || PLAT[a.platform] + ' 账号');
}
async function accountAct(a, act) {
  const r = await POST(`/api/accounts/${a.id}/${act}`);
  Object.assign(a, r);
  fire('account', r);
  if (act === 'start') B().running = true;
}
function showQR(a) {
  const img = h('img', { alt: '二维码' });
  const tip = h('div.muted.small', '用手机 QQ 扫码登录（扫完在手机上点确认）');
  const refresh = (url) => {
    img.src = url ? '/api/qr.svg?text=' + encodeURIComponent(url) : '/api/napcat/qrcode.png?ts=' + Date.now();
  };
  refresh(a.qr);
  const close = modal('扫码登录 ' + accountTitle(a), h('div.col.gap12', h('div.qrbox', img), tip,
    h('div.faint.small', '二维码一会儿就过期，过期会自动换新的。扫不出来时可以用备用网页：', a.webui ? h('a', { href: a.webui, target: '_blank' }, a.webui) : '')));
  const offs = [on('qr', d => { if (d.id === a.id) refresh(d.url); }),
    on('account', d => { if (d.id === a.id && d.status === 'online') { close(); toast('登录成功'); } })];
  const t = setInterval(() => { if (!document.body.contains(img)) { offs.forEach(f => f()); clearInterval(t); } }, 1000);
}
// 微信小号：首次启动读出当前登录者，请用户确认「这是机器人小号吗」；之后每次启动、运行中都核对
const wxAsking = new Set();
function wechatWho(i) {
  return i.nick && i.wxid ? `昵称 ${i.nick}（微信号 ${i.wxid}）` : i.wxid ? `微信号 ${i.wxid}` : `昵称 ${i.nick || ''}（读不到微信号）`;
}
function wechatConfirm(a) {
  const p = a.pending;
  if (!p || wxAsking.has(a.id)) return;
  wxAsking.add(a.id);
  const hits = p.hits || [];
  const answer = async (ok, close, again = false) => {
    try {
      const r = await POST(`/api/accounts/${a.id}/wechat_confirm`, { ok, again });
      if (r.account) { Object.assign(a, r.account); fire('account', r.account); }
      if (r.changed) toast('确认的时候发现微信已经换了号，没有绑定', 'err');
      else toast(ok ? '已绑定，开始工作' : '已取消启动');
    } catch (e) { toast(e.message || '操作失败', 'err'); }
    close();
  };
  // 读到的微信对上了人物表里某个人的信息：醒目警告，要点两次才能绑定，默认按钮是「取消」
  const warn = () => h('div.col.gap4', hits.map(x => h('div.notice.bad', icon('alert'), h('div', x.admin
    ? h('b', { style: { color: 'var(--bad)' } }, `这是人物表里「${x.name}」的号（管理员），确定要让机器人用它说话吗？`)
    : `这是人物表里「${x.name}」的号，确定要让机器人用它说话吗？`,
    h('div.small.muted', `（对上的是：${x.by.join('、')}）`)))));
  const body = h('div.col.gap12');
  const foot = h('div.row.grow');
  let step = 1;
  const show = close => {
    clear(body,
      hits.length ? warn() : null,
      step === 2 ? h('div', hits.length ? '再确认一次：真的要把它绑定为机器人小号吗？' : '') : h('div', p.reason === 'nick_changed'
        ? `绑定的机器人小号昵称是「${p.old_nick}」，现在登录的昵称是「${p.nick}」，昵称变了就对不上了。现在登录的还是机器人小号吗？`
        : `检测到当前登录的微信是：${wechatWho(p)}，这是机器人小号吗？`),
      h('div.notice.warn', icon('alert'), h('div.small', '如果这是你自己平时用的微信（大号），请点「不是」，换成小号登录后再启动。点「是」之后，机器人就会用这个微信在你设好的聊天里发言，并且之后每次启动、运行中都会核对，换了号会立刻停止。')));
    clear(foot, h('div.grow'),
      step === 2
        ? [h('button.danger', { onclick: () => answer(true, close, true) }, '仍然绑定'), h('button.primary', { onclick: () => answer(false, close) }, '取消')]
        : hits.length
          ? [h('button', { onclick: () => { step = 2; show(close); } }, '是，继续…'), h('button.primary', { onclick: () => answer(false, close) }, '取消')]
          : [h('button', { onclick: () => answer(false, close) }, '不是，取消启动'), h('button.primary', { onclick: () => answer(true, close) }, '是，这是机器人小号')]);
    if (hits.length) setTimeout(() => { const b = foot.querySelector('button.primary'); b && b.focus(); }, 30);  // 命中人物表：默认停在「取消」上
  };
  const close = modal('确认微信小号', body, { foot: () => foot, onclose: () => wxAsking.delete(a.id) });
  show(close);
}
function wechatBind(a) {
  const b = a.bound;
  if (!b) return h('div.small.faint', '还没绑定机器人小号：第一次启动时会请你确认。');
  return h('div.small', h('span.badge.ok', '已绑定：' + (b.nick || '') + (b.wxid ? `（${b.wxid}）` : '（读不到微信号，按昵称核对）')));
}
on('account', d => {
  const a = byId(B() && B().accounts, d.id);
  if (a && d.status === 'confirm' && d.pending) wechatConfirm(a);
});

function accountCard(a, { compact = false } = {}) {
  const role = byId(B().roles, a.role_id);
  const el = h('div.card.col', { style: { gap: '10px' } });
  const render = () => clear(el,
    h('div.row', plat(a.platform), h('div.grow', h('div.ellipsis', { style: { fontWeight: 650 } }, accountTitle(a)),
      h('div.small.muted.ellipsis', role ? '默认角色：' + role.name : '还没设默认角色')), statusBadge(a)),
    a.detail && h('div.small.muted', a.detail),
    a.platform === 'wechat' && wechatBind(a),
    h('div.row.wrap',
      a.status === 'stopped' || a.status === 'error'
        ? h('button.sm.primary', { onclick: () => accountAct(a, 'start') }, icon('play'), '启动')
        : h('button.sm', { onclick: () => accountAct(a, 'stop') }, icon('stop'), '停止'),
      a.platform === 'qq' && a.status === 'waiting_login' ? h('button.sm', { onclick: () => showQR(a) }, icon('qr'), '扫码') : null,
      a.platform === 'wechat' && a.status === 'confirm' ? h('button.sm.primary', { onclick: () => wechatConfirm(a) }, icon('check'), '确认身份') : null,
      a.platform === 'wechat' && a.bound ? h('button.sm.ghost', { onclick: async () => {
        if (!await confirmBox('重新绑定会清掉现在记着的小号身份，并重启这个账号，然后按这会儿登录的微信重新请你确认。继续？', '重新绑定')) return;
        const r = await POST(`/api/accounts/${a.id}/wechat_rebind`);
        Object.assign(a, r); fire('account', r);
      } }, icon('refresh'), '重新绑定') : null,
      !compact && h('button.sm.ghost', { onclick: () => editAccount(a) }, icon('edit'), '设置')));
  render();
  const off = on('account', d => { if (d.id === a.id) { Object.assign(a, d); if (document.body.contains(el)) render(); else off(); } });
  return el;
}

// ---------- 总览 ----------
page('home', '总览', 'home', async v => {
  await reloadBoot();
  const st = await GET('/api/status');
  const b = B();
  const warn = [];
  const gset = (await GET('/api/settings')).items;
  const admit = gset.find(x => x.key === 'admit_ai');
  if (mainKeyMissing(b.endpoints, gset)) warn.push(['bad', '还没填 API Key，机器人没法说话。', 'settings/model', '去填']);
  const wn = b.wechat_notice;
  if (wn) warn.push([wn.level === 'verified' ? '' : 'warn', `微信 ${wn.version}：${wn.note}`, 'accounts', '去看看']);
  if (b.wechat_lib_update) warn.push(['warn', b.wechat_lib_update.text, 'accounts', '去升级']);
  const plats = Object.entries(b.platforms).filter(([, on]) => on).map(([p]) => p);
  if (!plats.length) warn.push(['warn', '还没打开任何平台。', 'accounts', '去开']);
  for (const p of plats) if (!b.accounts.some(a => a.platform === p)) warn.push(['warn', `${PLAT[p]} 已打开，但还没有账号。`, 'accounts', '添加']);
  for (const a of b.accounts.filter(a => a.status === 'error')) warn.push(['bad', `${accountTitle(a)}：${a.detail}`, 'accounts', '查看']);
  for (const a of b.accounts.filter(a => a.status === 'confirm')) warn.push(['warn', `${accountTitle(a)}：${a.detail}`, 'accounts', '去确认']);
  if (!b.roles.length) warn.push(['warn', '还没有角色。', 'roles', '新建']);
  if (b.hard_cap && b.hard_cap.active) warn.push(['warn', '今天的用量已到上限（用量硬上限）：被@和私聊只回一句固定的话，记忆整理、看图、搜索等暂停，明天自动恢复。', 'usage', '看用量']);

  for (const bn of await GET('/api/bans')) warn.push(['warn', `账号「${bn.account_name}」在群「${bn.room_name}」被禁言，${bn.until ? '到 ' + fmtTime(bn.until) : '结束时间未知'}：这段时间她在这个群里不发言，解除后自动恢复。`, 'chats/' + encodeURIComponent(bn.room), '看看这个群']);
  const notices = (await GET('/api/notices')).filter(n => !n.read);
  const noticeBox = notices.length ? h('div.card.col.gap8.mb8', h('div.row', h('b.grow', `未读通知（${notices.length}）`),
    h('button.sm', { onclick: async ev => { await POST('/api/notices/read', {}); ev.currentTarget.closest('.card').remove(); } }, '全部标为已读')),
    notices.slice(0, 8).map(n => h('div.small', h('time.muted', fmtTime(n.ts)), ' ', n.text, n.via ? h('span.faint', `（已同时发到 ${PLAT[n.via] || n.via}）`) : null))) : null;

  const logs = h('div.list');
  const addLog = (l, top = true) => {
    const row = h('div.log', { class: l.level }, h('time', fmtTime(l.ts)), h('span.badge', catName(l.cat)), h('span.txt', dispText(l.text)));
    top ? logs.prepend(row) : logs.append(row);
    while (logs.children.length > 12) logs.lastChild.remove();
  };
  (await GET('/api/logs?limit=12')).filter(l => l.level !== 'debug').forEach(l => addLog(l, false));
  live('log', l => { if (l.level !== 'debug') addLog(l); });

  clear(v,
    h('div.card.row.mb8', h('div.grow', h('b', '承认是AI'), h('div.small.muted', '开：被问是不是AI就认。关：提示词里不写「是AI」这类句子。角色页可以给单个角色设成开 / 关（默认跟随这里），群里也能单独设。')),
      toggle(admit ? admit.value : true, async val => {
        await PUT('/api/settings', { scope: 'global', key: 'admit_ai', value: val });
        (B().settings.global ||= {}).admit_ai = val;
        toast(val ? '她们被问到会承认是AI' : '提示词里不再写「是AI」');
      }, '承认是AI（总开关）')),
    noticeBox,
    warn.length ? h('div.col.mb8', warn.map(([k, t, to, btn]) => h('div.notice', { class: k }, icon('alert'), h('div.grow', t),
      h('button.sm', { onclick: () => go(to) }, btn)))) : null,
    h('div.grid', { style: { marginTop: warn.length ? '12px' : 0 } },
      h('div.card.stat', h('span.muted', '机器人'), h('b', b.running ? '运行中' : '已停止'),
        h('span.small.muted', `${b.accounts.filter(a => a.status === 'online').length} 个账号在线 · ${st.rooms} 个群`)),
      h('div.card.stat', h('span.muted', '今天调用模型'), h('b', st.today.calls + ' 次'),
        h('span.small.muted', `输入 ${fmtNum(st.today.prompt)} · 输出 ${fmtNum(st.today.completion)} tokens`)),
      h('div.card.stat', h('span.muted', '角色'), h('b', b.roles.length + ' 个'), h('span.small.muted', b.roles.map(r => r.name).join('、') || '—'))),
    h('div.section-title', h('h2', '账号'), h('button.sm', { onclick: () => go('accounts') }, icon('plus'), '管理账号')),
    b.accounts.length ? h('div.grid', b.accounts.map(a => accountCard(a, { compact: true })))
      : h('div.card.empty', icon('plug'), h('div', '还没有账号'), h('button.primary.mt8', { onclick: () => go('accounts') }, '添加账号')),
    h('div.section-title', h('h2', '最近动态'), h('button.sm.ghost', { onclick: () => go('logs') }, '全部日志', icon('chev'))),
    h('div.card', { style: { padding: '6px 12px' } }, logs));
}, { order: 1 });

const CATS = { reply: '回复', gatekeeper: '守门员', command: '指令', shadow: '影子', memory: '记忆', model: '模型', system: '系统', resource: '资源', napcat: 'NapCat', notify: '通知' };
const catName = c => CATS[c] || c;

// ---------- 账号 ----------
page('accounts', '账号', 'plug', async v => {
  await reloadBoot();
  const b = B();
  const nap = await GET('/api/napcat/info').catch(() => null);
  const section = p => {
    const accs = b.accounts.filter(a => a.platform === p);
    const on_ = !!b.platforms[p];
    const note = h('div');
    if (p === 'qq' && on_) {
      add(note, nap && nap.found
        ? h('div.notice.ok', icon('check'), h('div.grow', `NapCat 已就绪（${nap.kind === 'onekey' ? '一键包，自带 QQ' : '借用电脑上的 QQ'}${nap.version ? '，' + nap.version : ''}）`, h('div.faint.small', nap.dir)),
          h('button.sm.ghost', { onclick: () => napcatSetup() }, '更换'))
        : h('div.notice.warn', icon('alert'), h('div.grow', '还没有 NapCat（QQ 机器人要靠它登录）。'), h('button.sm.primary', { onclick: () => napcatSetup() }, '准备 NapCat')));
    }
    if (p === 'wechat' && on_) GET('/api/wechat/check').then(w => add(note, wechatNotice(w)));
    return h('div.card',
      h('div.card-h', plat(p), h('h3', PLAT[p]), h('span.small.muted', on_ ? '已开启' : '未开启'),
        toggle(on_, async val => { await PUT('/api/platforms', { [p]: val }); b.platforms[p] = val; App.route(); }, '开关这个平台'),
        h('button.sm.ghost', { onclick: () => platformSettings(p) }, icon('gear'), '平台设置'),
        h('button.sm', { onclick: () => editAccount({ platform: p }) }, icon('plus'), '添加')),
      note,
      accs.length ? h('div.grid.mt8', accs.map(a => accountCard(a))) : h('div.faint.small.mt8', '还没有账号'));
  };
  clear(v, h('div.notice.mb8', icon('info'), h('div', '每个账号挂一个角色；同一个角色可以挂在好几个平台的账号上（同一个她）。改了账号设置后点「停止」再「启动」生效。')),
    section('qq'), section('wechat'), section('discord'), ...(await moreSections()));
}, { order: 3 });

function platformSettings(p) {
  const body = h('div');
  settingsForm('platform:' + p).then(f => add(body, h('div.notice.mb8', icon('info'), `这里是 ${PLAT[p]} 上所有角色的默认值；角色、群里还能再单独改。`), f));
  modal(PLAT[p] + ' 平台设置', body, { wide: true });
}

function wechatNotice(w) {
  const lib = w.lib ? null : h('button.sm.primary', { onclick: async e => {
    e.target.disabled = true; e.target.textContent = '下载中…';
    try { await POST('/api/wechat/download'); toast('组件已下载'); refresh(); } catch (err) { e.target.disabled = false; e.target.textContent = '下载组件'; }
  } }, '下载组件');
  return h('div.col.gap4',
    h('div.notice', { class: w.ok ? 'ok' : w.level === 'verified' ? '' : 'warn' }, icon(w.ok ? 'check' : w.level === 'verified' ? 'info' : 'alert'), h('div.grow', w.installed ? `本机微信 ${w.version}：${w.note}` : w.note)),
    w.lib_update && h('div.notice.warn', icon('download'), h('div.grow', w.lib_update.text),
      h('button.sm.primary', { onclick: async e => {
        if (!await confirmBox(`把 wxauto4 从 ${w.lib_update.installed} 升级到 ${w.lib_update.latest}？升级前要先停掉微信账号；升级失败会自动回退到原来的版本；升级好之后重启程序才生效。`, '升级')) return;
        e.target.disabled = true;
        try { const r = await POST('/api/wechat/upgrade'); toast(`已升级到 ${r.new}，重启程序后生效`); refresh(); } catch (err) { e.target.disabled = false; toast(err.message || '升级失败', 'err'); }
      } }, '升级 wxauto4')),
    w.lib && w.lib_version && h('div.small.faint', `wxauto4 组件版本：${w.lib_version}`),
    !w.lib && h('div.notice.warn', icon('download'), h('div.grow', '还没下载微信自动化组件 wxauto4（约 2MB，从 PyPI 官方下载）。'), lib),
    w.lib_error && h('div.notice.bad', icon('alert'), h('div.grow', '微信组件加载失败：' + w.lib_error)),
    h('div.notice', icon('info'), h('div.grow small',
      '微信靠界面自动化：小号的微信窗口要一直开着（可以挡在别的窗口后面，别最小化、别关），所以和「后台不弹窗」做不到兼顾。',
      '认人靠「小号给对方设的秘密备注」，到人物表里填同样的备注。微信不能撤回、不能由程序改昵称头像。一台电脑一般只能登录一个微信（见说明文档）。')),
    h('div.notice', icon('info'), h('div.grow small', '「平台设置」里可以打开「把微信窗口移到屏幕外」：运行时微信窗口放到显示器之外，不挡你的屏幕。注意：运行中别用小号手动聊天；切换会话、发送时窗口会闪一下；仍然不能最小化。窗口留在屏幕外了（比如程序被强行结束），点右边的按钮找回来；想一直看着窗口，先把这个开关关掉。'),
      h('button.sm', { onclick: async e => {
        e.target.disabled = true;
        try {
          const r = await POST('/api/wechat/recover');
          toast(!r.found ? '没找到微信窗口（微信开着吗？）' : r.moved ? '已把微信窗口放回屏幕里' : '微信窗口本来就在屏幕里');
        } finally { e.target.disabled = false; }
      } }, icon('refresh'), '找回微信窗口')));
}

async function napcatSetup() {
  const info = await GET('/api/napcat/info');
  const dir = h('input', { value: B().napcat_dir || '', placeholder: '例如 D:\\NapCat\\NapCat.52230.Shell' });
  const prog = h('div.small.muted');
  const dl = h('button.primary', { onclick: async () => {
    dl.disabled = true; prog.textContent = '开始下载…';
    await POST('/api/napcat/download');
  } }, icon('download'), '从官方 GitHub 下载');
  const off = on('download', d => {
    if (d.what !== 'napcat') return;
    if (d.error) { prog.textContent = '下载失败：' + d.error; dl.disabled = false; }
    else if (d.finished) { prog.textContent = '下载完成'; toast('NapCat 已就绪'); off(); close(); refresh(); }
    else prog.textContent = `已下载 ${(d.done / 1048576).toFixed(1)} MB` + (d.total ? ` / ${(d.total / 1048576).toFixed(1)} MB` : '');
  });
  const close = modal('准备 NapCat', h('div.col.gap12',
    h('div', 'NapCat 让程序能登录 QQ 小号。它的许可证不允许随意打包，所以由你从官方渠道下载，程序自动放好、自动写配置。'),
    h('div.card.col', h('h4', '方法一：自动下载（推荐）'),
      h('div.small.muted', '下载 NapCat.Shell（约 36MB）。它要借用电脑上装的 QQ：', info.system_qq ? h('span.badge.ok', '已找到 QQ') : h('span', h('span.badge.warn', '没找到 QQ'), ' 请先到 ', h('a', { href: info.qq_download, target: '_blank' }, 'QQ 官网'), ' 安装。')),
      h('div.row', dl, prog)),
    h('div.card.col', h('h4', '方法二：用已有的 NapCat 文件夹'),
      h('div.small.muted', '比如一键包解压出的 NapCat.xxxxx.Shell 文件夹（里面有 NapCatWinBootMain.exe）。'),
      h('div.row', dir, h('button', { onclick: async () => { await PUT('/api/kv', { k: 'napcat_dir', v: dir.value.trim() }); toast('已设置'); close(); refresh(); } }, '使用这个文件夹'))),
    h('div.faint.small', '当前：' + info.dir + (info.found ? '（可用）' : '（没找到）'))), { onclose: () => off() });
}

function editAccount(a) {
  const isNew = !a.id;
  const d = JSON.parse(JSON.stringify(a));
  d.platform = d.platform || 'qq';
  if (isNew) d.role_id = d.role_id || (B().roles[0] || {}).id;
  const body = h('div.form');
  const token = h('input', { type: 'password', placeholder: a.has_token ? '已保存（不显示），要换就重新粘贴' : '粘贴机器人令牌', autocomplete: 'off' });
  const listen = h('div.col.gap4');
  const renderListen = () => clear(listen, (d.listen || []).map((c, i) => h('div.row',
    h('input', { value: c.name, placeholder: '群名或好友备注（和微信里显示的一致）', oninput: e => c.name = e.target.value }),
    select([['group', '群聊'], ['private', '私聊']], c.type || 'group', v => c.type = v),
    h('button.ghost.icon', { onclick: () => { d.listen.splice(i, 1); renderListen(); } }, icon('trash')))),
    h('button.sm', { onclick: () => { (d.listen ||= []).push({ name: '', type: 'group' }); renderListen(); } }, icon('plus'), '添加要监听的聊天'));
  renderListen();
  const roleSel = select([['', '（不绑定）'], ...B().roles.map(r => [r.id, r.name])], d.role_id || '', v => d.role_id = v);
  add(body,
    h('div.form2', field('显示名', h('input', { value: d.name || '', placeholder: '只在面板里显示', oninput: e => d.name = e.target.value })),
      field('默认角色', roleSel, '没有排班规则命中时，这个账号说话时是谁（规则在「排班」页）')),
    d.platform === 'qq' && [
      field('QQ 号', h('input', { value: d.qq || '', inputmode: 'numeric', placeholder: '机器人小号的 QQ 号', oninput: e => d.qq = e.target.value.trim() }),
        '建议用专门的小号。第一次启动会弹二维码，用手机 QQ 扫码。'),
      h('details.adv', h('summary', icon('chev'), '高级'),
        h('div.form2.mt8',
          field('由程序启动 NapCat', toggle(d.manage_napcat !== false, v => d.manage_napcat = v), '关掉 = 自己启动 NapCat，程序只去连'),
          field('WebSocket 端口', h('input', { type: 'number', value: d.ws_port || '', placeholder: '自动分配', oninput: e => d.ws_port = parseInt(e.target.value) || undefined })),
          field('NapCat 备用网页端口', h('input', { type: 'number', value: d.webui_port || '', placeholder: '自动分配', oninput: e => d.webui_port = parseInt(e.target.value) || undefined })))),
    ],
    d.platform === 'discord' && [
      discordGuide(a),
      field('机器人令牌（Token）', token, '只存在这台电脑的数据里，不会进日志和导出文件（除非导出时选了带密钥）'),
    ],
    d.platform === 'wechat' && [
      h('div.notice', icon('info'), h('div.small', '先在这台电脑上打开微信并登录小号。下面填要监听的群和私聊（名字和微信里显示的一模一样）。第一次启动时程序会读出当前登录的微信，请你确认「这是机器人小号吗」，确认后才开始工作；之后每次启动、运行中都会核对，登录的不是这个号就停止。')),
      !isNew && wechatBind(a),
      field('监听的聊天', listen),
    ],
    !isNew && a.status === 'online' && profileEditor(a));
  modal(isNew ? '添加' + PLAT[d.platform] + '账号' : '账号设置', body, {
    wide: d.platform === 'discord',
    foot: close => [
      !isNew && h('button.danger', { onclick: async () => {
        if (await confirmBox(`删除账号「${accountTitle(a)}」？（不会删除角色和记忆）`, '删除', true)) { await DEL('/api/docs/account/' + a.id); close(); refresh(); }
      } }, icon('trash'), '删除'),
      h('div.grow'), h('button', { onclick: close }, '取消'),
      h('button.primary', { onclick: async () => {
        if (d.platform === 'qq' && !/^\d{5,12}$/.test(d.qq || '')) return toast('QQ 号不对', 'err');
        if (d.platform === 'wechat') d.listen = (d.listen || []).filter(c => c.name.trim());
        const saved = await PUT('/api/docs/account', d);
        if (token.value.trim()) await PUT('/api/secret', { k: 'discord:' + saved.id, v: token.value.trim() });
        close(); toast('已保存'); refresh();
      } }, '保存')],
  });
}

function discordGuide(a) {
  const inv = h('span');
  const mk = async () => {
    if (!a.id) return toast('先保存令牌，再生成邀请链接', 'err');
    const r = await GET('/api/discord/invite/' + a.id);
    clear(inv, r.url ? h('a', { href: r.url, target: '_blank' }, '打开邀请链接') : '先填令牌');
  };
  return h('div.card', h('h4.mb8', '在 Discord 开发者后台开一个机器人（照着做，约 5 分钟）'),
    h('ol.guide',
      h('li', h('div', '打开 ', h('a', { href: 'https://discord.com/developers/applications', target: '_blank' }, '开发者后台'), '，点右上角「New Application」，起个名字，勾选同意，点「Create」。')),
      h('li', h('div', '左边点「Bot」。在「Token」那里点「Reset Token」→ 确认 →「Copy」，粘贴到下面的「机器人令牌」。')),
      h('li', h('div', '同一页往下找到「Privileged Gateway Intents」，打开「MESSAGE CONTENT INTENT」，点底部「Save Changes」。（不开的话她看不到消息内容）')),
      h('li', h('div', '可选：同一页的头像、名字就是机器人在 Discord 上的样子，也可以在这里改。')),
      h('li', h('div', '保存本页后点 ', h('button.sm', { onclick: mk }, icon('link'), '生成邀请链接'), ' ', inv, '，在打开的页面选服务器 →「授权」，机器人就进群了。')),
      h('li', h('div', '在国内：先到「平台设置」里填代理地址（例如 http://127.0.0.1:7890），不然连不上 Discord。'))));
}

function profileEditor(a) {
  const nick = h('input', { placeholder: '新昵称（留空不改）' });
  const sig = h('input', { placeholder: '新个性签名（留空不改）' });
  const file = h('input', { type: 'file', accept: 'image/*' });
  const chats = B().chats.filter(c => c.platform === a.platform);
  let chat = chats[0] ? chats[0].id : '';
  const card = h('input', { placeholder: '在这个群里的名片' });
  if (a.platform === 'wechat') return h('div.notice', icon('info'), '微信的昵称、头像、群昵称请在微信里手动改。');
  return h('details.adv', h('summary', icon('chev'), '账号资料（昵称、头像、签名、群名片）'),
    h('div.form.mt8',
      h('div.form2', nick, a.platform === 'qq' ? sig : h('div.faint.small', 'Discord 的「关于我」要在开发者后台改')),
      field('头像', file),
      h('button', { onclick: async () => {
        let avatar = '';
        if (file.files[0]) avatar = await new Promise(r => { const fr = new FileReader(); fr.onload = () => r(fr.result); fr.readAsDataURL(file.files[0]); });
        await POST(`/api/accounts/${a.id}/profile`, { nickname: nick.value.trim(), signature: sig.value.trim(), avatar });
        toast('已提交修改');
      } }, '修改资料'),
      chats.length ? h('div.row.wrap', select(chats.map(c => [c.id, c.name || c.id]), chat, v => chat = v), card,
        h('button', { onclick: async () => { await POST(`/api/accounts/${a.id}/profile`, { chat_id: chat.split(':').slice(1).join(':'), card: card.value }); toast('已修改群名片'); } }, '改群名片')) : null));
}

// ---------- 角色 ----------
page('roles', '角色', 'bot', async (v, [rid]) => {
  await reloadBoot();
  if (rid) return roleEditor(v, byId(B().roles, rid));
  const usage = await GET('/api/usage?by=role&days=1');
  const list = h('div.grid');
  for (const r of B().roles) {
    const accs = B().accounts.filter(a => a.role_id === r.id);
    const u = usage.find(x => x.k === r.id);
    list.append(h('div.card.col', { style: { cursor: 'pointer', gap: '8px' }, onclick: () => go('roles/' + r.id) },
      h('div.row', h('h3.grow', r.name), h('span.badge.blue', (byId(B().personas, r.persona) || {}).name || '未选人设')),
      h('div.small.muted', r.summary || '（还没写一句话摘要）'),
      h('div.row.wrap', accs.map(a => h('span.badge', plat(a.platform), accountTitle(a))), !accs.length && h('span.faint.small', '还没挂到账号上')),
      h('div.small.faint', `今天 ${u ? u.calls : 0} 次调用 · ${fmtNum(u ? u.prompt + u.completion : 0)} tokens`)));
  }
  const library = templateLibrary(r => go('roles/' + r.id));
  clear(v, h('div.section-title', h('h2', '我的角色'), h('button', { onclick: () => library.scrollIntoView({ behavior: 'smooth', block: 'start' }) }, icon('star'), '从模板新建'), h('button.primary', { onclick: async () => {
    const name = await promptBox('新建角色', '角色名字');
    if (!name) return;
    // 新角色自己带一套空白人设（不再默认挂第一套，免得改它连带改到别的角色）
    const p = await PUT('/api/docs/persona', { name, mode: 'modules', items: [{ m: '@people', on: true }, { m: '@memory', on: true }], whole: '', _note: '新建角色时自动建的空白人设' });
    const r = await PUT('/api/docs/role', { name, persona: p.id, summary: '', attitudes: {}, relations: {} });
    await reloadBoot();
    go('roles/' + r.id);
  } }, icon('plus'), '新建角色')),
    h('div.notice.mb8', icon('info'), h('div', '角色 = 一个机器人人物：用哪套人设、给守门员的一句话摘要、插话力度、记忆。每项没单独改就沿用全局默认；他对不同的人是什么态度，在「人物」页各人物卡的关系方案里定。')),
    list, h('div.mt16', library));
}, { order: 4 });

async function roleEditor(v, r) {
  if (!r) return clear(v, h('div.empty', '没有这个角色'));
  const save = debounce(async () => { await PUT('/api/docs/role', r); toast('已保存'); }, 600);
  const others = B().roles.filter(x => x.id !== r.id);
  const shared = h('div.small.warn');  // 共用这套人设的其他角色：改人设会连带改到它们
  const shareNote = () => {
    const names = others.filter(x => x.persona === r.persona).map(x => x.name);
    shared.textContent = names.length ? `共用这套人设的还有：${names.join('、')}（改人设会连带改到它们；想各改各的，先在人设页复制一份再换过来）` : '';
  };
  shareNote();
  // 这个角色面对每个人用的是哪套关系方案、来自哪条规则（方案在「人物」页的人物卡里编辑）
  const attBox = h('div.col.gap4');
  GET('/api/people/effective?role=' + encodeURIComponent(r.id)).then(eff => clear(attBox, B().people.filter(p => (p.name || '').trim()).map(p => {
    const e = eff[p.id];
    return e ? h('div.row.wrap.small', h('b', pLabel(p)), h('span.badge.blue', e.scheme), h('span.faint', `（${e.rule}）`),
      e.identity ? h('span.muted', `当作「${e.identity}」`) : null, e.calls.length ? h('span.muted', `称呼：${e.calls.join('、')}`) : null,
      e.conflict.length ? h('span.badge.orange', `冲突：还有「${e.conflict.join('」「')}」也命中，没用`) : null) : null;
  })));
  const scopes = [[`role:${r.id}`, '角色默认（所有平台、所有群）'],
    ...Object.keys(PLAT).map(p => [`role:${r.id}|platform:${p}`, `在 ${PLAT[p]} 上`]),
    ...B().chats.map(c => [`role:${r.id}|chat:${c.id}`, `在群「${c.name || c.id}」`])];
  const usage = (await GET('/api/usage?by=role&days=30')).find(x => x.k === r.id);
  const aiOwn = (B().settings[`role:${r.id}`] || {}).admit_ai;
  clear(v,
    h('div.row.mb8', h('button.ghost', { onclick: () => go('roles') }, icon('back'), '全部角色'), h('div.grow'),
      h('button.danger.sm', { onclick: async () => {
        if (await confirmBox(`删除角色「${r.name}」？挂在它上面的账号会变成未绑定。`, '删除', true)) { await DEL('/api/docs/role/' + r.id); go('roles'); }
      } }, icon('trash'), '删除角色')),
    h('div.grid2',
      h('div.card.form', h('h3', '基本'),
        field('名字', h('input', { value: r.name, oninput: e => { r.name = e.target.value; save(); } })),
        field('人设', select(B().personas.map(p => [p.id, p.name]), r.persona, val => { r.persona = val; shareNote(); save(); }),
          h('span', '到 ', h('a', { href: '#/personas/' + r.persona }, '人设'), ' 页编辑内容、试聊', shared)),
        field('承认是AI', select([['', '跟随总开关'], ['on', '开'], ['off', '关']], aiOwn === undefined ? '' : aiOwn ? 'on' : 'off', async val => {
          const sc = `role:${r.id}`;
          await PUT('/api/settings', val ? { scope: sc, key: 'admit_ai', value: val === 'on' } : { scope: sc, key: 'admit_ai', reset: true });
          const d = (B().settings[sc] ||= {});
          if (val) d.admit_ai = val === 'on'; else delete d.admit_ai;
          toast('已保存');
        }), '「跟随」时随总览页的总开关一起变；单独设了开或关，就不受总开关影响。'),
        field('标签', tagsInput(r.tags || [], x => { r.tags = x; save(); }, '输入后回车'),
          '给角色打标签（从模板新建的会带上模板的类别，比如「原创角色」「学术讲解」）。人物卡的关系方案可以「按标签」选用：比如所有带「学术讲解」的角色，都把某人当作学生'),
        field('QQ 群名片', h('input', { value: r.card || '', placeholder: r.name, oninput: e => { r.card = e.target.value; save(); } }),
          '开了「群名片随角色自动改」时，换班后改成这个（空着用角色名）'),
        field('一句话摘要（给守门员）', h('textarea', { rows: 2, value: r.summary || '', oninput: e => { r.summary = e.target.value; save(); } }),
          '守门员判断要不要插话时只看这句，比如：贴吧出身、嘴毒能对线的群友，看人用拉康的眼光。'),
        h('div.small.muted', `近 30 天：${usage ? usage.calls : 0} 次调用，${fmtNum(usage ? usage.prompt + usage.completion : 0)} tokens（详见「用量」）`)),
      h('div.card.form', h('h3', '同群的其他机器人'),
        h('div.small.muted', '同一个群里有别的机器人时，它们会自动进入她的人物表。这里设她和它们是什么关系（默认姐妹）。'),
        others.length ? others.map(o => field(o.name, h('input', { value: (r.relations || {})[o.id] || '', placeholder: '姐妹',
          oninput: e => { (r.relations ||= {})[o.id] = e.target.value.trim(); save(); } }))) : h('div.faint.small', '只有这一个角色'),
        h('h3.mt8', '她把每个人当作什么人'),
        h('div.small.muted', '每个人对这个角色用的是人物卡上的哪套「关系方案」，以及是按哪条规则选中的（指定角色 > 按标签 > 全体角色）。要改，到「人物」页打开那张人物卡的「关系方案」。'), attBox)),
    h('div.card', h('div.card-h', h('h3', '行为设置')), scopedSettings(`role:${r.id}`, scopes, { exclude: ['系统'], top: ['persona', 'wake_words', 'proactive', 'eagerness', 'shadow'], hide: ['admit_ai'], hideAt: `role:${r.id}` })));
}

// ---------- 群 ----------
page('chats', '群', 'group', async (v, [key]) => {
  await reloadBoot();
  if (key) return chatEditor(v, byId(B().chats, key));
  const rooms = await GET('/api/rooms');
  let filter = 'all', q = '';
  const list = h('div.list');
  const render = () => clear(list, rooms.filter(r => (filter === 'all' || r.platform === filter) && (!q || (r.name || r.id).includes(q))).map(r => {
    const en = ((B().settings['chat:' + r.id] || {}).enabled);
    return h('div.item.click', { onclick: () => go('chats/' + encodeURIComponent(r.id)) }, plat(r.platform),
      h('div.grow', h('div.ellipsis', { style: { fontWeight: 600 } }, r.name || r.chat_id),
        h('div.small.muted.ellipsis', r.desc || (r.last ? `${r.last.name}: ${r.last.text}` : '还没有消息'))),
      (r.cgroups || []).map(g => h('span.badge.desktop-only', (byId(B().cgroups, g) || {}).name || '?')),
      r.bots.map(b => h('span.badge.blue.desktop-only', b.role)),
      en === false ? h('span.badge', '不说话') : null, icon('chev'));
  }), !rooms.length && h('div.empty', icon('group'), h('div', '机器人进过的群会出现在这里')));
  render();
  const editGroup = g => {
    const d = { ...g };
    modal(g.id ? '编辑群分组' : '新群分组', field('名字', h('input', { value: d.name || '', placeholder: '比如：学习群', oninput: e => d.name = e.target.value })), {
      foot: c => [g.id && h('button.danger', { onclick: async () => { if (await confirmBox('删除这个群分组？（排班规则里用到它的条件会失效）', '删除', true)) { await DEL('/api/docs/cgroup/' + g.id); c(); App.route(); } } }, '删除'),
        h('div.grow'), h('button', { onclick: c }, '取消'), h('button.primary', { onclick: async () => { if (!(d.name || '').trim()) return; await PUT('/api/docs/cgroup', d); c(); App.route(); } }, '保存')],
    });
  };
  const gbar = h('div.row.wrap', { style: { gap: '6px' } }, B().cgroups.map(g => h('span.chip', { 'data-id': g.id },
    h('span.handle', { style: { padding: 0 } }, icon('grip')), g.name, h('span.x', { title: '编辑', onclick: () => editGroup(g) }, '✎'))));
  sortable(gbar, async ids => { await POST('/api/docs/cgroup/order', { ids }); });
  clear(v, h('div.card.mb8', h('div.row.wrap', h('span.muted', '群分组：'), gbar,
      h('button.sm.ghost', { onclick: () => editGroup({}) }, icon('plus'), '新分组'),
      h('span.faint.small', '排班规则里可以直接选整组。在群设置里把群加进分组。'))),
    h('div.row.wrap.mb8', seg([['all', '全部'], ['qq', 'QQ'], ['wechat', '微信'], ['discord', 'Discord']], filter, f => { filter = f; render(); }),
    h('div.grow'), h('input', { placeholder: '搜索群名', style: { maxWidth: '220px' }, oninput: e => { q = e.target.value; render(); } })),
    h('div.card', { style: { padding: '6px' } }, list));
}, { order: 7 });

async function chatEditor(v, c) {
  if (!c) return clear(v, h('div.empty', '没有这个群'));
  const save = debounce(async () => { await PUT('/api/docs/chat', c); toast('已保存'); }, 600);
  const sched = h('div.col');
  const bots = B().accounts.filter(a => a.platform === c.platform);
  const renderSched = () => clear(sched, (c.schedules || []).map((s, i) => h('div.card.col', { style: { gap: '8px' } },
    h('div.row.wrap', toggle(s.on !== false, x => { s.on = x; save(); }),
      h('input', { type: 'time', value: s.time || '08:00', style: { width: '120px' }, onchange: e => { s.time = e.target.value; save(); } }),
      chips([[0, '一'], [1, '二'], [2, '三'], [3, '四'], [4, '五'], [5, '六'], [6, '日']], s.days || [], d => { s.days = d.map(Number); save(); }),
      h('div.grow'), h('button.ghost.icon', { onclick: () => { c.schedules.splice(i, 1); save(); renderSched(); } }, icon('trash'))),
    h('div.row.wrap', seg([['text', '固定内容'], ['ai', '交给她写']], s.mode || 'text', m => { s.mode = m; save(); renderSched(); }),
      select([['', '群里任一机器人'], ...bots.map(a => [a.id, accountTitle(a)])], s.account || '', a => { s.account = a; save(); })),
    h('textarea', { rows: 2, value: s.content || '', placeholder: s.mode === 'ai' ? '告诉她写什么，比如：跟大家道个早安，顺便聊聊今天的天气' : '要发的内容',
      oninput: e => { s.content = e.target.value; save(); } }))),
    h('button.sm', { onclick: () => { (c.schedules ||= []).push({ time: '08:00', days: [], mode: 'text', content: '', on: true }); save(); renderSched(); } }, icon('plus'), '添加定时消息'),
    h('div.faint.small', '不选星期 = 每天。'));
  renderSched();
  const dutyBox = h('div.small.muted', '加载中…');
  GET('/api/duty').then(d => {
    const row = d.now.find(x => x.chat === c.id);
    clear(dutyBox, row ? row.items.map(it => h('div', h('b', d.names.accounts[it.account] || it.account), '：',
      h('span', { style: { color: roleColor(it.role), fontWeight: 650 } }, roleName(it.role)), `（${srcText(it.src)}）`,
      it.next_at ? `，下次 ${hhmm(it.next_at)} 换成「${roleName(it.next_role)}」` : '，一周内不换')) : '这个群还没有机器人账号');
  });
  const scopes = [[`chat:${c.id}`, '本群（所有角色）'], ...B().roles.map(r => [`role:${r.id}|chat:${c.id}`, `角色「${r.name}」在本群`]),
    ...bots.map(a => [`acct:${a.id}|chat:${c.id}`, `账号「${accountTitle(a)}」在本群（群里的插话指令设在这里）`])];
  // 群里「@她 本群插话关 / 插话力度」设的是「这个账号在本群」：不管谁在岗都生效，比下面所有面板设置都优先；这里看得到、清得掉
  const ovBox = h('div.col.gap8');
  const renderOv = () => {
    const rows = Object.entries(B().settings).filter(([sc]) => sc.startsWith('acct:') && sc.endsWith('|chat:' + c.id))
      .flatMap(([sc, d]) => Object.entries(d).map(([k, v]) => ({ sc, k, v })));
    clear(ovBox, rows.map(({ sc, k, v }) => {
      const s = B().schema.find(x => x.key === k) || { label: k };
      const shown = s.type === 'bool' ? (v ? '开' : '关') : ((s.options || []).find(o => o[0] === v) || [0, v])[1];
      const acc = byId(B().accounts, sc.slice(5, sc.indexOf('|')));
      return h('div.notice.warn', icon('alert'), h('div.grow', `账号「${acc ? accountTitle(acc) : '?'}」在本群的「${s.label}」被群里的指令设成了「${shown}」：不管谁在岗都生效，比下面的面板设置优先。`),
        h('button.sm', { onclick: async () => { await PUT('/api/settings', { scope: sc, key: k, reset: true }); delete B().settings[sc][k]; renderOv(); toast('已清除，恢复按面板设置'); } }, '清除'));
    }));
  };
  renderOv();
  const banBox = h('div.col.gap8.mb8', (await GET('/api/bans')).filter(bn => bn.room === c.id).map(bn => h('div.notice.warn', icon('alert'),
    h('div.grow', `账号「${bn.account_name}」在本群被禁言，${bn.until ? '到 ' + fmtTime(bn.until) : '结束时间未知'}：这段时间它在本群不跑守门员、不写回复、不发任何东西（照常收消息、记记忆、执行管理指令），解除后自动恢复；别的账号不受影响。`))));
  clear(v,
    h('div.row.mb8', h('button.ghost', { onclick: () => go('chats') }, icon('back'), '全部群'), h('div.grow'),
      h('button.sm', { onclick: () => go('chat/' + encodeURIComponent(c.id)) }, icon('chat'), '看消息 / 接管')),
    banBox,
    h('div.grid2',
      h('div.card.form', h('div.row', plat(c.platform), h('h3.grow', c.name || c.chat_id)),
        field('群档案：一句话说明', h('textarea', { rows: 2, value: c.desc || '', placeholder: '这是什么群、能疯到什么程度，比如：考研互助群，正经为主，玩笑别过火',
          oninput: e => { c.desc = e.target.value; save(); } }), '会写进她回复时看到的「当前群」那一行'),
        field('已登记', toggle(!!c.listed, x => { c.listed = x; save(); }), '配合「只在登记过的群说话」使用'),
        field('群分组', B().cgroups.length ? chips(B().cgroups.map(g => [g.id, g.name]), c.cgroups || [], x => { c.cgroups = x; save(); })
          : h('div.faint.small', '在「群」列表页上方建群分组'), '排班规则可以按分组指定'),
        field('本群对所有人的默认态度', attitudeEditor(c.attitude, a => { c.attitude = a; save(); }), '态度四层：所有人 → 本群 → 分组 → 个人，越具体越优先')),
      h('div.col', tuneCard(c.id), await styleCard(c.id), h('div.card.form', h('div.row', h('h3.grow', '现在是谁'), h('button.sm.ghost', { onclick: () => go('duty/' + encodeURIComponent(c.id)) }, icon('calendar'), '排班')), dutyBox),
        h('div.card.form', h('h3', '定时消息'), sched))),
    h('div.card', h('div.card-h', h('h3', '本群设置'), h('span.small.muted', '插话力度、记忆等')), ovBox,
      scopedSettings(`chat:${c.id}`, scopes, { exclude: ['系统', '平台'], top: ['enabled', 'proactive', 'eagerness', 'memory_enabled', 'persona', 'shadow'] })));
}

// ---------- 消息（人工接管、影子模式）----------
let chatFilter = 'all';  // 消息页左侧筛选：全部 / 群聊 / 私聊
page('chat', '消息', 'chat', async (v, [key]) => {
  const rooms = await GET('/api/rooms?all=1');
  const wrap = h('div.chat-wrap', { class: key ? 'in-room' : '' });
  const roomList = h('div.rooms');
  const msgs = h('div.msgs');
  const head = h('div.row', { style: { padding: '10px 14px', borderBottom: '1px solid var(--border)' } });
  const composer = h('div.composer');
  const renderRooms = () => {
    const shown = rooms.filter(r => chatFilter === 'all' || (chatFilter === 'private') === (r.type === 'private'));
    clear(roomList, shown.length ? shown.map(r => h('div.item.click', { class: r.id === key ? 'on' : '', onclick: () => go('chat/' + encodeURIComponent(r.id)) },
      plat(r.platform), h('div.grow', h('div.row.gap4', h('div.ellipsis', { style: { fontWeight: 600 } }, r.name || r.chat_id),
        r.type === 'private' ? h('span.badge', '私聊') : null),
      h('div.small.ellipsis', { class: r.last ? 'muted' : 'faint' }, r.last ? dispText(`${r.last.name}: ${r.last.text}`) : '暂无消息'))))
      : h('div.empty', chatFilter === 'private' ? '还没有私聊' : chatFilter === 'group' ? '还没有群消息' : '还没有消息'));
  };
  renderRooms();
  add(wrap, h('div.card.rooms-card', h('div', { style: { padding: '10px 14px', borderBottom: '1px solid var(--border)' } },
    seg([['all', '全部'], ['group', '群聊'], ['private', '私聊']], chatFilter, f => { chatFilter = f; renderRooms(); })), roomList),
    h('div.card.msgs-card', head, msgs, composer));
  clear(v, wrap);
  if (!key) { add(msgs, h('div.empty', icon('chat'), h('div', '选一个群或私聊，看消息、用某个角色手动发言、或点某条消息让她回'))); return; }
  const room = rooms.find(r => r.id === key) || { id: key, bots: [] };
  const isDm = room.type === 'private' || key.startsWith('dm:');
  const accounts = room.bots;
  let acc = accounts[0] ? accounts[0].id : '';
  const bubble = m => {
    const who = m.kind === 'shadow' ? '影子：' + (byId(B().roles, m.role_id) || {}).name + ' 本来会说' : dispText(m.name);
    return h('div.msg', { class: (m.kind === 'bot' || m.kind === 'shadow' ? 'bot ' : '') + (m.kind === 'shadow' ? 'shadow' : '') },
      h('div.who', who, h('span.faint', fmtTime(m.ts))),
      h('div.bubble', { onclick: e => e.currentTarget.parentElement.classList.toggle('show-acts') }, m.text),
      m.kind === 'human' || m.kind === 'import' ? h('div.acts', accounts.map(b => h('button.sm.ghost', { onclick: async () => {
        await POST(`/api/rooms/${encodeURIComponent(key)}/reply`, { account: b.id, msg_id: m.id }); toast(`已让${b.role}回这条`);
      } }, icon('send'), `让${b.role}回`))) : null);
  };
  const list = await GET(`/api/rooms/${encodeURIComponent(key)}/messages`);
  list.forEach(m => msgs.append(bubble(m)));
  msgs.scrollTop = msgs.scrollHeight;
  live('msg', m => {
    if (m.room !== key) return;
    const near = msgs.scrollHeight - msgs.scrollTop - msgs.clientHeight < 80;
    msgs.append(bubble({ ...m, role_id: m.role }));
    if (near) msgs.scrollTop = msgs.scrollHeight;
  });
  clear(head, h('button.ghost.icon.mobile-only', { onclick: () => go('chat') }, icon('back')), plat(room.platform || 'qq'),
    h('b.grow.ellipsis', room.name || key), isDm ? h('span.badge', '私聊') : h('button.sm.ghost', { onclick: () => go('chats/' + encodeURIComponent(key)) }, icon('gear'), h('span.desktop-only', '群设置')));
  const ta = h('textarea', { placeholder: accounts.length ? '以选中的角色身份发言（回车发送，Shift+回车换行）' : (isDm ? '这个私聊的机器人账号现在不在线' : '这个群里现在没有在线的机器人') });
  const send = async () => {
    if (!ta.value.trim() || !acc) return;
    await POST(`/api/rooms/${encodeURIComponent(key)}/send`, { account: acc, text: ta.value.trim() });
    ta.value = '';
  };
  ta.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });
  clear(composer, accounts.length > 0 ? select(accounts.map(b => [b.id, b.role]), acc, x => acc = x) : null, ta,
    h('button.primary', { onclick: send, disabled: !accounts.length }, icon('send'), '发送'),
    isDm ? null : h('button', { disabled: !accounts.length, title: '撤回这个角色在本群的上一条', onclick: async () => {
      const r = await POST(`/api/rooms/${encodeURIComponent(key)}/recall`, { account: acc }); toast(r.msg);
    } }, icon('undo'), h('span.desktop-only', '撤回上一条')));
}, { order: 2, short: '消息' });

// ---------- 更多（手机底栏）----------
page('more', '更多', 'more', async v => {
  clear(v, h('div.card', { style: { padding: '6px' } }, PAGES.filter(p => !['more'].includes(p.id)).map(p =>
    h('div.item.click', { onclick: () => go(p.id) }, icon(p.icon), h('div.grow', p.title), icon('chev')))));
}, { order: 99, hidden: true });
for (const id of ['accounts', 'duty', 'personas', 'chats', 'memory', 'logs', 'usage', 'demo', 'settings']) {
  // 这些页面在手机上从「更多」进入
  setTimeout(() => { const p = PAGES.find(x => x.id === id); if (p) p.more = true; });
}
