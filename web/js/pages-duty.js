/* 排班：角色分配规则（分群、分时段、自由组合）、现在是谁、排班视图、问题提醒。 */
'use strict';

const WEEK = ['一', '二', '三', '四', '五', '六', '日'];
function roleColor(id) {
  if (!id) return 'var(--border2)';
  let h = 0;
  for (const c of id) h = (h * 31 + c.charCodeAt(0)) % 360;
  return `hsl(${h} 62% 55%)`;
}
const roleName = id => (byId(B().roles, id) || {}).name || (id ? id : '（没有角色）');
const srcText = src => src === 'override' ? '临时换的' : src === 'default' ? '账号默认角色' : '排班规则';
const hhmm = iso => iso ? iso.slice(5, 16).replace('T', ' ') : '';

page('duty', '排班', 'calendar', async (v, [chatSel, accSel]) => {
  await reloadBoot();
  const d = await GET('/api/duty');
  const list = h('div');
  const renderRules = () => clear(list, d.rules.length ? sortable(h('div.list', d.rules.map((r, i) => h('div.item', { 'data-id': r.id, style: { alignItems: 'flex-start' } },
    h('span.handle', icon('grip')), h('span.badge', `#${i + 1}`),
    toggle(r.on !== false, async on => { r.on = on; await PUT('/api/docs/rule', r); toast(on ? '已启用' : '已停用'); }),
    h('div.grow', { style: { cursor: 'pointer' }, onclick: () => editRule(r) },
      h('b', r.name || '未命名规则'), h('div.small.muted', r.desc),
      h('div.small', '→ ', r.mode === 'rotate'
        ? `在 ${(r.roles || []).map(roleName).join('、')} 之间${r.order === 'random' ? '随机' : '按顺序'}轮换（${r.every === 'slot' ? '每 ' + (r.slot_minutes || 120) + ' 分钟' : '每天'}）`
        : h('span', { style: { color: roleColor(r.role), fontWeight: 650 } }, roleName(r.role)))),
    h('button.ghost.icon', { title: '删除', onclick: async () => { if (await confirmBox(`删除规则「${r.name || '未命名'}」？`, '删除', true)) { await DEL('/api/docs/rule/' + r.id); App.route(); } } }, icon('trash'))))),
    async ids => { await POST('/api/docs/rule/order', { ids }); App.route(); })
    : h('div.empty', '还没有规则：每个账号都用自己的默认角色（账号页设）'));
  renderRules();

  const nowBox = h('div.list', d.now.map(c => h('div.item', { style: { alignItems: 'flex-start' } }, plat(c.platform),
    h('div.grow', h('b', c.name), c.items.map(it => h('div.small.row.wrap', { style: { gap: '6px', marginTop: '3px' } },
      h('span.muted', d.names.accounts[it.account] || it.account, '：'),
      h('span', { style: { color: roleColor(it.role), fontWeight: 650 } }, roleName(it.role)), h('span.faint', `（${srcText(it.src)}）`),
      it.next_at ? h('span.muted', `下次 ${hhmm(it.next_at)} 换成 `, h('b', roleName(it.next_role))) : h('span.faint', '一周内不换'),
      it.override_until ? h('button.sm.ghost', { onclick: async () => { await POST('/api/duty/clear', { account: it.account, chat: c.chat }); App.route(); } }, '取消临时换') : null,
      h('button.sm.ghost', { onclick: () => go(`duty/${encodeURIComponent(c.chat)}/${it.account}`) }, '排班视图')))))));

  const chats = d.now;
  let chat = chatSel || (chats[0] || {}).chat;
  let acc = accSel || ((chats.find(c => c.chat === chat) || chats[0] || { items: [] }).items[0] || {}).account;
  let mode = 'week';
  const viewBox = h('div');
  const schedule = async () => {
    if (!chat || !acc) return clear(viewBox, h('div.empty', '还没有群和账号'));
    const now = new Date();
    const start = new Date(now.getFullYear(), now.getMonth(), mode === 'week' ? now.getDate() - ((now.getDay() + 6) % 7) : 1);
    const days = mode === 'week' ? 7 : new Date(start.getFullYear(), start.getMonth() + 1, 0).getDate();
    const ds = `${start.getFullYear()}-${String(start.getMonth() + 1).padStart(2, '0')}-${String(start.getDate()).padStart(2, '0')}`;
    const r = await GET(`/api/duty/schedule?account=${acc}&chat=${encodeURIComponent(chat)}&start=${ds}&days=${days}`);
    clear(viewBox, scheduleGrid(r.segments, start, days, mode));
  };
  const accBox = h('span');
  const renderAcc = () => clear(accBox, select(((chats.find(c => c.chat === chat) || {}).items || []).map(it => [it.account, d.names.accounts[it.account]]), acc, x => { acc = x; schedule(); }));
  const chatPick = select(chats.map(c => [c.chat, c.name]), chat, x => { chat = x; acc = ((chats.find(c => c.chat === x) || {}).items || [])[0]?.account; renderAcc(); schedule(); });
  renderAcc();

  clear(v,
    d.warnings.length ? h('div.col.mb8', d.warnings.map(w => h('div.notice', { class: w.level === 'bad' ? 'bad' : 'warn' }, icon('alert'), h('div', w.text)))) : null,
    h('div.notice.mb8', icon('info'), h('div.small', '同一个账号可以在不同群、不同时段、不同日期当不同角色。规则从上往下判定，先命中的算；都没命中用账号的默认角色。',
      '群里 @她「换角色名 2小时」是临时覆盖（不带时长到当天结束），到期回到这里的安排。头像和昵称整个账号共用，不随群或时段变；QQ 群名片可以随角色自动改（群设置 → 多机器人）。')),
    h('div.card', h('div.card-h', h('h3', '规则表'), h('span.small.muted', '拖动排序'),
      h('button.sm', { onclick: async () => { await POST('/api/duty/autosort'); toast('已按具体程度排序'); App.route(); } }, icon('list'), '按具体程度自动排序'),
      h('button.sm.primary', { onclick: () => editRule({ on: true, mode: 'fixed', platforms: [], accounts: [], chats: [], chat_groups: [], weekdays: [] }) }, icon('plus'), '新规则')),
      h('div.small.faint.mb8', '自动排序：指定日期 > 星期加时段 > 只限星期 > 每天时段 > 不限；同一档里，指定了群的在前。'),
      list),
    h('div.card', h('div.card-h', h('h3', '现在是谁')), chats.length ? nowBox : h('div.empty', '机器人进过群后，这里显示每个群现在是谁、下次几点换成谁')),
    h('div.card', h('div.card-h', h('h3', '排班视图'), chatPick, accBox,
      seg([['week', '本周'], ['month', '本月']], mode, x => { mode = x; schedule(); })), viewBox));
  schedule();
}, { order: 4.5 });

function scheduleGrid(segs, start, days, mode) {
  const dayMs = 86400000, t0 = start.getTime();
  const pieces = segs.map(s => ({ ...s, a: new Date(s.start).getTime(), b: new Date(s.end).getTime() }));
  const legend = [...new Set(segs.map(s => s.role))];
  const legendEl = h('div.row.wrap.mb8', legend.map(r => h('span.row.gap4.small', h('i', { style: { width: '12px', height: '12px', borderRadius: '3px', background: roleColor(r), display: 'inline-block' } }), roleName(r))));
  const dayPieces = i => pieces.filter(p => p.b > t0 + i * dayMs && p.a < t0 + (i + 1) * dayMs).map(p => ({
    ...p, from: Math.max(0, (p.a - t0 - i * dayMs) / dayMs), to: Math.min(1, (p.b - t0 - i * dayMs) / dayMs) }));
  const label = i => { const d = new Date(t0 + i * dayMs); return `${d.getMonth() + 1}/${d.getDate()} 周${WEEK[(d.getDay() + 6) % 7]}`; };
  const title = p => `${hhmm(p.start)} - ${hhmm(p.end)}　${roleName(p.role)}（${srcText(p.src)}）`;
  if (mode === 'week') {
    const H = 432;
    return h('div', legendEl, h('div.row', { style: { alignItems: 'stretch', gap: '6px', overflowX: 'auto' } },
      h('div', { style: { position: 'relative', width: '34px', height: H + 22 + 'px', flex: 'none' } },
        [0, 3, 6, 9, 12, 15, 18, 21, 24].map(hr => h('div.small.faint', { style: { position: 'absolute', top: 20 + hr / 24 * H - 7 + 'px', right: '4px' } }, hr + ':00'))),
      Array.from({ length: days }, (_, i) => h('div', { style: { flex: '1', minWidth: '64px' } },
        h('div.small.muted', { style: { textAlign: 'center', height: '20px' } }, label(i)),
        h('div', { style: { position: 'relative', height: H + 'px', borderRadius: '8px', overflow: 'hidden', background: 'var(--surface2)', border: '1px solid var(--border)' } },
          dayPieces(i).map(p => h('div', { title: title(p), style: { position: 'absolute', left: 0, right: 0, top: p.from * H + 'px', height: Math.max(1, (p.to - p.from) * H) + 'px', background: roleColor(p.role), opacity: p.src === 'override' ? 0.6 : 0.9, color: '#fff', fontSize: '11px', padding: '2px 4px', overflow: 'hidden', borderBottom: '1px solid rgba(255,255,255,.4)' } },
            (p.to - p.from) * H > 16 ? roleName(p.role) : '')))))));
  }
  return h('div', legendEl, h('div.col', { style: { gap: '4px' } },
    h('div.row.small.faint', h('span', { style: { width: '78px' } }), h('div.grow.row.between', [0, 6, 12, 18, 24].map(hr => h('span', hr + '点')))),
    Array.from({ length: days }, (_, i) => h('div.row', { style: { gap: '6px' } }, h('span.small.muted', { style: { width: '78px', flex: 'none' } }, label(i)),
      h('div.grow', { style: { position: 'relative', height: '18px', borderRadius: '5px', overflow: 'hidden', background: 'var(--surface2)' } },
        dayPieces(i).map(p => h('div', { title: title(p), style: { position: 'absolute', top: 0, bottom: 0, left: p.from * 100 + '%', width: (p.to - p.from) * 100 + '%', background: roleColor(p.role), opacity: p.src === 'override' ? 0.6 : 0.9 } })))))));
}

function editRule(r) {
  const d = JSON.parse(JSON.stringify(r));
  const b = B();
  const body = h('div.form');
  const render = () => clear(body,
    field('规则名字', h('input', { value: d.name || '', placeholder: '比如：周末晚上温和版群友值班', oninput: e => d.name = e.target.value })),
    h('div.small.muted', '下面的条件都可以不填，不填就是不限。'),
    field('平台', chips(PLAT_PICK, d.platforms || [], x => d.platforms = x)),
    field('账号', chips(b.accounts.map(a => [a.id, accountTitle(a)]), d.accounts || [], x => d.accounts = x)),
    field('群', b.chats.length ? chips(b.chats.map(c => [c.id, (PLAT[c.platform] || '') + ' ' + (c.name || c.chat_id)]), d.chats || [], x => d.chats = x) : h('div.faint.small', '机器人进过群后才能选')),
    field('群分组', b.cgroups.length ? chips(b.cgroups.map(g => [g.id, g.name]), d.chat_groups || [], x => d.chat_groups = x) : h('div.faint.small', '在「群」页建群分组')),
    h('div.form2', field('从哪天', h('input', { type: 'date', value: d.date_from || '', onchange: e => d.date_from = e.target.value })),
      field('到哪天', h('input', { type: 'date', value: d.date_to || '', onchange: e => d.date_to = e.target.value }))),
    field('星期', chips(WEEK.map((w, i) => [i, '周' + w]), d.weekdays || [], x => d.weekdays = x.map(Number))),
    h('div.form2', field('每天从', h('input', { type: 'time', value: d.time_from || '', onchange: e => d.time_from = e.target.value })),
      field('到', h('input', { type: 'time', value: d.time_to || '', onchange: e => d.time_to = e.target.value }), '可以跨午夜，比如 22:00 到 02:00；过了 0 点那一截算前一天的星期和日期')),
    h('h4.mt8', '结果'),
    seg([['fixed', '固定一个角色'], ['rotate', '几个角色轮换']], d.mode || 'fixed', x => { d.mode = x; render(); }),
    d.mode === 'rotate'
      ? [field('参与轮换的角色', chips(b.roles.map(x => [x.id, x.name]), d.roles || [], x => d.roles = x)),
        h('div.row.wrap', seg([['seq', '按顺序'], ['random', '随机']], d.order || 'seq', x => d.order = x),
          seg([['day', '每天换'], ['slot', '按时段换']], d.every || 'day', x => { d.every = x; render(); }),
          d.every === 'slot' ? h('label.row.small', '每', h('input', { type: 'number', value: d.slot_minutes || 120, style: { width: '80px' }, onchange: e => d.slot_minutes = parseInt(e.target.value) || 120 }), '分钟') : null),
        h('div.small.faint', '随机也是「一段时间内固定」：同一天（或同一个时段）里不会每条消息都换人。')]
      : field('角色', select([['', '选一个角色…'], ...b.roles.map(x => [x.id, x.name])], d.role || '', x => d.role = x)));
  render();
  modal(r.id ? '编辑规则' : '新规则', body, {
    wide: true,
    foot: c => [h('div.grow'), h('button', { onclick: c }, '取消'), h('button.primary', { onclick: async () => {
      if (d.mode === 'rotate' ? !(d.roles || []).length : !d.role) return toast('先选结果里的角色', 'err');
      if (!!d.time_from !== !!d.time_to) return toast('时段要填全（从几点到几点）', 'err');
      await PUT('/api/docs/rule', d); c(); toast('已保存'); App.route();
    } }, '保存')],
  });
}
