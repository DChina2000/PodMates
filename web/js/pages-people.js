/* 人物表：人物卡、分组、态度四层（所有人 → 某个群 → 分组 → 个人）、批量设置。 */
'use strict';

page('people', '人物', 'users', async v => {
  await reloadBoot();
  let q = '', group = '', ctxChat = '';
  const sel = new Set();
  let eff = await GET('/api/people/effective');
  const listBox = h('div.grid');
  const groupBar = h('div.row.wrap');
  const batchBar = h('div.card.row.wrap.hide', { style: { position: 'sticky', top: '64px', zIndex: 5 } });

  const renderGroups = () => {
    const bar = h('div.row.wrap', { style: { gap: '6px' } }, B().pgroups.map(g => h('span.chip', { 'data-id': g.id, class: group === g.id ? 'on' : '', onclick: e => { if (!e.target.closest('.handle')) { group = group === g.id ? '' : g.id; renderGroups(); renderList(); } } },
      h('span.handle', { style: { padding: 0 } }, icon('grip')), g.name, attText(g.attitude) ? h('span.faint.small', '·' + attText(g.attitude)) : null,
      h('span.x', { title: '编辑分组', onclick: e => { e.stopPropagation(); editGroup(g); } }, '✎'))));
    sortable(bar, async ids => { await POST('/api/docs/pgroup/order', { ids }); await reloadBoot(); eff = await GET('/api/people/effective'); renderList(); });
    clear(groupBar, h('span.muted', '分组：'), bar, h('button.sm.ghost', { onclick: () => editGroup({ name: '', attitude: null }) }, icon('plus'), '新分组'),
      h('span.faint.small', '拖动分组排序：一个人在多个组且态度冲突时，取排在前面的组。'));
  };
  const editGroup = g => {
    const d = JSON.parse(JSON.stringify(g));
    modal(g.id ? '编辑分组' : '新分组', h('div.form', field('名字', h('input', { value: d.name, oninput: e => d.name = e.target.value })),
      field('这个分组的态度', attitudeEditor(d.attitude, a => d.attitude = a))), {
      foot: c => [g.id && h('button.danger', { onclick: async () => { if (await confirmBox('删除这个分组？')) { await DEL('/api/docs/pgroup/' + g.id); c(); App.route(); } } }, '删除'),
        h('div.grow'), h('button', { onclick: c }, '取消'),
        h('button.primary', { onclick: async () => { if (!d.name.trim()) return; await PUT('/api/docs/pgroup', d); c(); App.route(); } }, '保存')],
    });
  };
  const renderBatch = () => {
    batchBar.classList.toggle('hide', !sel.size);
    clear(batchBar, h('b', `已选 ${sel.size} 人`),
      h('button.sm', { onclick: () => {
        let att = null;
        modal('批量设置态度', attitudeEditor(null, a => att = a), { foot: c => [h('button', { onclick: c }, '取消'),
          h('button.primary', { onclick: async () => { await POST('/api/people/batch', { ids: [...sel], attitude: att }); c(); App.route(); } }, '应用到所选的人')] });
      } }, '设态度'),
      B().pgroups.length ? select([['', '加入分组…'], ...B().pgroups.map(g => [g.id, g.name])], '', async x => { if (x) { await POST('/api/people/batch', { ids: [...sel], add_group: x }); App.route(); } }) : null,
      B().pgroups.length ? select([['', '移出分组…'], ...B().pgroups.map(g => [g.id, g.name])], '', async x => { if (x) { await POST('/api/people/batch', { ids: [...sel], remove_group: x }); App.route(); } }) : null,
      h('div.grow'), h('button.sm.ghost', { onclick: () => { sel.clear(); renderBatch(); renderList(); } }, '取消选择'));
  };
  const renderList = () => clear(listBox, B().people.filter(p => (!group || (p.groups || []).includes(group)) &&
    (!q || JSON.stringify(p).includes(q))).map(p => {
    const e = eff[p.id] || {};
    const ids = [...(p.qq || []).map(x => 'QQ ' + x), ...(p.discord || []).map(x => 'Discord ' + x), ...(p.wechat_remark || []).map(x => '微信备注 ' + x)];
    return h('div.card.col', { style: { gap: '6px', cursor: 'pointer' }, onclick: ev => { if (!ev.target.closest('input')) editPerson(p); } },
      h('div.row', h('input', { type: 'checkbox', checked: sel.has(p.id), onchange: ev => { ev.target.checked ? sel.add(p.id) : sel.delete(p.id); renderBatch(); } }),
        p.self && h('span.badge.ok', '用户'), h('b.grow', pLabel(p)), p.identity && h('span.badge.orange', p.identity), p.admin && h('span.badge.blue', icon('shield'), '管理员')),
      h('div.small.muted.ellipsis', ids.join(' · ') || '还没填账号（填了才认得出）'),
      h('div.small', '最终生效：', e.attitude ? [h('b', attText(e.attitude)), h('span.faint', `（来自${e.from}）`)] : h('span.faint', '未设置（她自己拿捏分寸）')),
      (p.groups || []).length ? h('div.row.wrap', p.groups.map(gid => h('span.badge', (byId(B().pgroups, gid) || {}).name || '?'))) : null);
  }), !B().people.length && h('div.card.empty', '人物表是空的'));

  const allAtt = h('div');
  add(allAtt, attitudeEditor(B().attitude_all, debounce(async a => { await PUT('/api/kv', { k: 'attitude_all', v: a }); eff = await GET('/api/people/effective'); renderList(); toast('已保存'); }, 600)));
  renderGroups(); renderList();
  clear(v,
    h('div.notice.mb8', icon('info'), h('div', '她只认账号，不认昵称：别人改名或自称表里某人，都按冒充处理。只有当前对话里出现的人，人物卡才会放进提示词。',
      h('div.small.faint', '态度四层：所有人 → 某个群 → 分组 → 个人，越具体越优先。同一个人对不同角色的关系和态度，在他人物卡的「关系方案」里定。'))),
    h('div.row.wrap.mb8', h('input', { placeholder: '搜索名字、账号', style: { maxWidth: '240px' }, oninput: e => { q = e.target.value; renderList(); } }),
      h('div.grow'), h('button.primary', { onclick: () => editPerson({}) }, icon('plus'), '新人物卡')),
    h('div.card.mb8', groupBar),
    batchBar, listBox,
    h('details.card.mt16', h('summary', { style: { cursor: 'pointer', fontWeight: 600 } }, '所有人的默认态度（第一层）'), h('div.mt8', allAtt)));
}, { order: 6 });

/* 关系方案编辑：一张人物卡上的多套「关系 / 称呼 / 态度 + 作用范围」。冲突提示按角色现有的标签算（和后端同一套规则）。 */
function schemeRank(s, role) {
  const sc = s.scope || {}, mode = sc.mode || 'all';
  if (mode === 'roles') return (sc.roles || []).includes(role.id) ? 3 : 0;
  if (mode === 'tags') return (sc.tags || []).some(t => (role.tags || []).includes(t)) ? 2 : 0;
  return 1;
}
function schemeConflicts(schemes) {
  const out = [];
  for (const r of B().roles) {
    for (const rank of [3, 2, 1]) {
      const hits = schemes.filter(s => schemeRank(s, r) === rank);
      if (hits.length) { if (hits.length > 1) out.push({ role: r.name, schemes: hits.map(s => s.name || '未命名') }); break; }
    }
  }
  return out;
}
function schemeEditor(d) {
  const box = h('div.col.gap8');
  const tagOptions = [...new Set(B().roles.flatMap(r => r.tags || []))].map(t => [t, t]);
  const roleOptions = B().roles.map(r => [r.id, r.name]);
  const render = () => {
    const conflicts = schemeConflicts(d.schemes);
    clear(box,
      d.schemes.map((s, i) => {
        s.calls ||= []; s.attitude ||= { presets: [], custom: '' }; s.scope ||= { mode: 'all', tags: [], roles: [] };
        const callsBox = h('div.col.gap4');
        const renderCalls = () => clear(callsBox, tagsInput(s.calls, x => { s.calls = x; renderCalls(); }, '输入后回车'),
          callPresets(s.identity).length ? h('div.row.wrap.small', h('span.muted', '常用称呼（点一下加上或去掉）：'), callPresets(s.identity).map(c => h('span.chip', { 'data-raw': '1', class: s.calls.includes(c) ? 'on' : '',
            onclick: () => { s.calls = s.calls.includes(c) ? s.calls.filter(x => x !== c) : [...s.calls, c]; renderCalls(); } }, c))) : null);
        renderCalls();
        const scopeBox = h('div.col.gap4');
        const renderScope = () => clear(scopeBox,
          select([['all', '全体角色'], ['tags', '按标签'], ['roles', '指定角色']], s.scope.mode || 'all', v => { s.scope.mode = v; renderScope(); render(); }),
          s.scope.mode === 'tags' ? (tagOptions.length ? chips(tagOptions, s.scope.tags || [], x => { s.scope.tags = x; render(); }) : h('div.small.faint', '还没有角色打过标签：到「角色」页给角色加标签。'))
            : s.scope.mode === 'roles' ? chips(roleOptions, s.scope.roles || [], x => { s.scope.roles = x; render(); }) : h('div.small.faint', '所有角色都用这一套（除非有更具体的方案命中）'));
        renderScope();
        return h('div.card.col.gap8', { style: { gap: '8px' } },
          h('div.row', h('input.grow', { value: s.name || '', placeholder: '方案名，比如：女儿、老师和学生', oninput: e => { s.name = e.target.value; } }),
            h('button.ghost.sm', { title: '上移（同优先级里排在前面的先用）', disabled: i === 0, onclick: () => { [d.schemes[i - 1], d.schemes[i]] = [d.schemes[i], d.schemes[i - 1]]; render(); } }, '↑'),
            h('button.ghost.sm', { title: '下移', disabled: i === d.schemes.length - 1, onclick: () => { [d.schemes[i + 1], d.schemes[i]] = [d.schemes[i], d.schemes[i + 1]]; render(); } }, '↓'),
            h('button.ghost.sm', { title: '删除这套方案', disabled: d.schemes.length === 1, onclick: () => { d.schemes.splice(i, 1); render(); } }, icon('trash'))),
          field('关系（她把这个人当作什么人）', h('input', { value: s.identity || '', placeholder: '比如：她妈、她爸、老同学、学生', oninput: e => { s.identity = e.target.value; renderCalls(); } }),
            '写进提示词的小标题；填「她妈」「她爸」时，下面的称呼会给出常用说法可以点选'),
          field('称呼（可以填多个）', callsBox, '她怎么叫这个人。填了多个就写成「称呼：A、B、C，随场合换着叫」，叫哪个由她自己定；只填一个照旧'),
          field('态度', attitudeEditor(s.attitude, a => s.attitude = a)),
          field('哪些角色用这一套', scopeBox));
      }),
      conflicts.length ? h('div.notice.warn', icon('alert'), h('div.small', '有冲突（同一优先级命中了好几套，只用排在最前面的）：',
        conflicts.map(c => h('div', `角色「${c.role}」同时命中「${c.schemes.join('」「')}」，用「${c.schemes[0]}」`)))) : null,
      h('div', h('button.sm', { onclick: () => { d.schemes.push({ id: '', name: '', identity: '', calls: [], attitude: { presets: [], custom: '' }, scope: { mode: 'tags', tags: [], roles: [] } }); render(); } }, icon('plus'), '加一套方案')));
  };
  render();
  return box;
}

function editPerson(p) {
  const isNew = !p.id;
  const d = JSON.parse(JSON.stringify(p));
  d.attitude ||= { presets: [], custom: '' };
  d.wechat_ids = [...(d.wechat_ids || []), ...(d.wechat_id ? [d.wechat_id] : [])];  // 旧版只有一个「微信号（备忘）」
  d.calls = (d.calls && d.calls.length) ? d.calls : (d.call ? [d.call] : []);  // 旧数据只有一个称呼
  // 关系方案：身份、称呼、态度都放在方案里；老卡片（没有方案）把原来的那套当作一套「默认」方案（全体角色）
  if (!(d.schemes && d.schemes.length)) d.schemes = [{ id: '', name: '默认', identity: d.identity || '', calls: [...d.calls], attitude: d.attitude, scope: { mode: 'all', tags: [], roles: [] } }];
  delete d.wechat_id;
  const f = (label, key, opts = {}) => field(label, h(opts.area ? 'textarea' : 'input', { value: d[key] || '', rows: 2, placeholder: opts.ph || '', oninput: e => d[key] = e.target.value }), opts.help);
  const ids = (label, key, help) => field(label, tagsInput(d[key] || [], x => d[key] = x), help);
  modal(isNew ? '新人物卡' : p.self ? '用户卡' : '人物卡：' + p.name, h('div.form',
    h('div.form2', f('表名', 'name', { help: '她在记录里看到的标记：名字〔表名本人〕' })),
    field('关系方案', schemeEditor(d), '同一个人，不同角色可以把他当成不同的人（比如「女儿」对他是妈，「老师」对他是学生）。每套方案有自己的关系（身份）、称呼和态度，再选哪些角色用。某个角色用哪套：指定角色 > 按标签 > 全体角色；同一优先级命中好几套，取排在前面的。都不命中，用「全体角色」那套'),
    h('div.form2', field('管理员', toggle(d.admin, x => d.admin = x), '管理指令里的「我」= 管理员；管理员自己说「忘掉我」不会执行（他的人物卡和记忆只能在面板里改）')),
    p.self ? h('div.notice', icon('info'), h('div.small', '这是「用户」卡（面板使用者）：系统里固定的一张，不能删，里面的内容都可以改。人设里的 {{用户}} 会换成这张卡的名字；名字没填时，人设里含这个称呼的句子就不出现。你和每个角色是什么关系（女儿、学生、朋友……），在上面的「关系方案」里定。'))
      : field('设为用户卡（面板使用者）', toggle(!!d.self, x => d.self = x), '全表只能有一张用户卡：打上后原来的用户卡自动取消'),
    field('这是别的机器人', toggle(!!d.bot, x => d.bot = x), '只表示这个账号是 AI。怎么对待它（消息跳过守门员、它 @ 她时接几轮）在 设置 → 全局默认 → 插话 里选'),
    ids('QQ 号', 'qq', '大号小号都填，回车分隔'),
    ids('Discord ID', 'discord', 'Discord 里右键头像 →「复制用户 ID」（要先在设置 → 高级里打开开发者模式）'),
    h('details', h('summary.small.muted', { style: { cursor: 'pointer' } }, '其他平台的账号（Telegram、LINE、B站……）'),
      h('div.form.mt8', PLAT_PICK.filter(([k]) => !['qq', 'discord', 'wechat'].includes(k)).map(([k, name]) => field(name + ' 账号',
        tagsInput((d.ids || {})[k] || [], x => { (d.ids ||= {})[k] = x; }), '回车分隔；Telegram 填数字用户 ID，B站填 UID，X 填用户 ID，Reddit 填用户名')))),
    h('details', h('summary.small.muted', { style: { cursor: 'pointer' } }, '各平台上的昵称（提示词里只会写当前平台上的那个）'),
      h('div.form2.mt8', PLAT_PICK.map(([k, name]) => field(name + ' 昵称', h('input', { value: (d.nicks || {})[k] || '', oninput: e => { (d.nicks ||= {})[k] = e.target.value.trim(); } }))))),
    h('div.form2', field('微信号', tagsInput(d.wechat_ids || [], x => d.wechat_ids = x), '这个人自己的微信号（可填多个，回车分隔）。不是用来认人的（微信群里认人只靠右边的秘密备注，因为界面自动化看不到发言人的微信号），而是机器人绑定微信账号时的保险：读到的微信号对上这里，就会警告「这是人物表里的号」'),
      field('微信秘密备注', tagsInput(d.wechat_remark || [], x => d.wechat_remark = x), '在小号的微信里给这个人设的备注，填一样的。微信群里认人只靠它；设好之前微信认不出这个人')),
    f('备注', 'note', { area: true, help: '会原样写进人物卡，比如：做什么工作、有什么忌讳' }),
    f('她知道的事', 'knows', { area: true }),
    B().pgroups.length ? field('分组', chips(B().pgroups.map(g => [g.id, g.name]), d.groups || [], x => d.groups = x)) : null), {
    wide: true,
    foot: c => [!isNew && !p.self && h('button.danger', { onclick: async () => { if (await confirmBox(`删除「${p.name}」的人物卡？`, '删除', true)) { await DEL('/api/docs/person/' + p.id); c(); App.route(); } } }, icon('trash'), '删除'),
      h('div.grow'), h('button', { onclick: c }, '取消'),
      h('button.primary', { onclick: async () => {
        if (!(d.name || '').trim() && !d.self) return toast('表名不能空', 'err');
        d.qq = (d.qq || []).map(String);
        await PUT('/api/docs/person', d); c(); toast('已保存'); App.route();
      } }, '保存')],
  });
}
