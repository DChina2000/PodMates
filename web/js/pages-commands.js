/* 自定义指令：群里有人这样说，就做这件事。匹配顺序：自定义指令 → 内置管理指令 → 正常聊天。 */
'use strict';

page('commands', '指令', 'list', async v => {
  await reloadBoot();
  const meta = await GET('/api/commands');
  let rules = meta.rules;
  const list = h('div');
  const saveAll = async (quiet = false) => {
    try { rules = (await PUT('/api/commands', { rules })).rules; if (!quiet) toast('已保存'); return true; } catch (e) { return false; /* 已提示 */ }
  };
  const summary = r => {
    const trig = (r.at ? '@她 ' : '') + ({ equal: '说', contains: '说的话里有', regex: '说的话符合' }[r.match]) + `「${r.pattern}」`;
    const act = { reply: `回：${r.text.slice(0, 24)}`, ask: `让她回答：${r.prompt.slice(0, 24)}`, builtin: `执行「${meta.builtin[r.builtin] || r.builtin}」${r.arg || ''}`, random: `随机发 ${r.lines.length} 句里的一句` }[r.action];
    return [trig, act];
  };
  const render = () => clear(list, rules.length ? sortable(h('div.list', rules.map((r, i) => {
    const [trig, act] = summary(r);
    return h('div.item', { 'data-id': r.id, style: { alignItems: 'flex-start' } }, h('span.handle', icon('grip')), h('span.badge', `#${i + 1}`),
      toggle(r.on !== false, async on => { r.on = on; await saveAll(true); toast(on ? '已启用' : '已停用'); }),
      h('div.grow', { style: { cursor: 'pointer' }, onclick: () => editRule(r) }, h('b', r.name), h('div.small.muted', trig), h('div.small', '→ ', act),
        h('div.small.faint', [meta.perm_modes[r.perm.mode], r.action === 'builtin' && r.perm.mode === 'all' ? '⚠ 对所有人开放' : '', r.cooldown ? `冷却 ${r.cooldown} 秒` : '', r.platforms.length || r.roles.length || r.chats.length ? '限定了范围' : '', ].filter(Boolean).join(' · '))),
      h('button.ghost.icon', { title: '删除', onclick: async () => { if (await confirmBox(`删除指令「${r.name}」？`, '删除', true)) { rules = rules.filter(x => x !== r); await saveAll(); render(); } } }, icon('trash')));
  })), async ids => { rules.sort((a, b) => ids.indexOf(a.id) - ids.indexOf(b.id)); await saveAll(true); render(); })
    : h('div.card.empty', icon('list'), h('div', '还没有自定义指令。点上面的「添加指令」建一条：群里有人说某句话，就让她做某件事。')));
  const editRule = r => {
    const d = JSON.parse(JSON.stringify(r || { name: '', on: true, at: true, match: 'equal', pattern: '', action: 'reply', text: '', prompt: '', builtin: 'mute', arg: '', lines: [],
      perm: { mode: 'all', people: [], pgroup: '' }, platforms: [], roles: [], chats: [], cooldown: 0 }));  // 动作换成「执行内置指令」时，权限自动改成「跟随」
    const dyn = h('div.form');
    const showAction = () => clear(dyn,
      d.action === 'reply' ? field('回复的文字', h('textarea', { rows: 3, value: d.text, oninput: e => d.text = e.target.value }), '原样发出去，不调模型。可以写 {name}，会换成说话人的名字') : null,
      d.action === 'ask' ? field('给她的提示', h('textarea', { rows: 3, value: d.prompt, placeholder: '比如：用三句话总结一下上面聊了什么', oninput: e => d.prompt = e.target.value }),
        '她用自己的口吻回答，走正常的人设拼装，会调一次模型（用量硬上限生效时不执行）') : null,
      d.action === 'builtin' ? h('div.form2', field('内置指令', select(Object.entries(meta.builtin), d.builtin, x => d.builtin = x), '相当于给内置指令起个别名'),
        field('参数', h('input', { value: d.arg, placeholder: '比如 480（分钟）、角色名 2小时、开、3', oninput: e => d.arg = e.target.value }), '和对她说内置指令时后面跟的那部分一样')) : null,
      d.action === 'random' ? field('候选的句子（一行一句）', h('textarea', { rows: 4, value: d.lines.join('\n'), oninput: e => d.lines = e.target.value.split('\n') }), '每次随机发其中一句，不调模型') : null);
    showAction();
    const permBox = h('div.form');
    const showPerm = () => clear(permBox,
      field('谁能用', select(Object.entries(meta.perm_modes).filter(([k]) => k !== 'follow' || d.action === 'builtin'), d.perm.mode, x => { d.perm.mode = x; showPerm(); }),
        d.action === 'builtin' ? '人物表里勾了「管理员」的人永远能用。「跟随」= 和直接对她说这条内置指令时的权限一样（在 设置 → 全局默认 → 权限 里设）' : '人物表里勾了「管理员」的人永远能用'),
      d.action === 'builtin' && d.perm.mode === 'all' ? h('div.notice.warn', icon('alert'), h('div.small', `这条会对所有人开放「${meta.builtin[d.builtin] || d.builtin}」：群里任何人说这句话，都能让她执行（内置指令本来只有管理员能用）。想保持原来的权限，把「谁能用」改成「跟随这条内置指令的权限」。`)) : null,
      d.perm.mode === 'people' ? field('指定的人', chips(B().people.map(p => [p.id, pLabel(p)]), d.perm.people, x => d.perm.people = x)) : null,
      d.perm.mode === 'pgroup' ? field('分组', select([['', '选一个分组'], ...B().pgroups.map(g => [g.id, g.name])], d.perm.pgroup, x => d.perm.pgroup = x)) : null);
    showPerm();
    const probe = h('div.small.muted');
    const testIn = h('input', { placeholder: '输入一句群里的话，看会触发哪条（不会真的执行）' });
    modal(r ? '编辑指令：' + r.name : '新指令', h('div.form',
      field('名字', h('input', { value: d.name, placeholder: '随便起，方便自己认', oninput: e => d.name = e.target.value })),
      h('div.card.form', h('b', '触发'),
        field('要不要先 @ 她', toggle(d.at, x => d.at = x), '开：要 @ 她（或回复她、叫她的名字）才触发。关：群里任何人说这句话都触发，只由一个机器人回'),
        h('div.form2', field('匹配方式', select(Object.entries(meta.matches), d.match, x => d.match = x)),
          field('触发的文字', h('input', { value: d.pattern, placeholder: d.match === 'regex' ? '正则，比如 ^(睡觉|晚安)$' : '比如：睡觉', oninput: e => d.pattern = e.target.value }))),
        h('div.row.wrap', testIn, h('button.sm', { onclick: async () => {
          const t = await POST('/api/commands/test', { text: testIn.value, at: d.at });
          probe.textContent = t.all.length ? '会触发：' + t.all.join('、') + '（只看已保存的指令表）' : '没有已保存的指令会触发';
        } }, '试一试')), probe),
      h('div.card.form', h('b', '动作'), field('做什么', select(Object.entries(meta.actions), d.action, x => {
        d.action = x;
        if (x === 'builtin' && d.perm.mode === 'all') d.perm.mode = 'follow';  // 给内置指令起别名：权限默认跟随那条指令，不悄悄放开给所有人
        if (x !== 'builtin' && d.perm.mode === 'follow') d.perm.mode = 'all';
        showAction(); showPerm();
      })), dyn),
      h('div.card.form', h('b', '权限与范围'), permBox,
        field('在哪些平台生效', chips(Object.keys(PLAT).map(p => [p, PLAT[p]]), d.platforms, x => d.platforms = x), '都不选 = 所有平台'),
        field('哪些角色在岗时生效', B().roles.length ? chips(B().roles.map(x => [x.id, x.name]), d.roles, x => d.roles = x) : h('div.faint.small', '还没有角色'), '都不选 = 所有角色'),
        field('在哪些群生效', B().chats.length ? chips(B().chats.map(c => [c.id, c.name || c.id]), d.chats, x => d.chats = x) : h('div.faint.small', '还没有群'), '都不选 = 所有群'),
        field('冷却时间（秒）', h('input', { type: 'number', min: 0, value: d.cooldown, oninput: e => d.cooldown = parseInt(e.target.value, 10) || 0 }), '同一个群里这条指令两次触发之间至少隔这么久；冷却中静静地不理。0 = 不设')),
      h('div.small.faint', '匹配顺序：自定义指令 → 内置管理指令（闭嘴、换人设……）→ 正常聊天。不提供脚本或代码执行。')), {
      wide: true,
      foot: c => [h('div.grow'), h('button', { onclick: c }, '取消'), h('button.primary', { onclick: async () => {
        const backup = JSON.parse(JSON.stringify(rules));
        if (r) Object.assign(r, d); else rules.push(d);
        if (await saveAll()) { c(); render(); } else { rules = backup; render(); }
      } }, '保存')],
    });
  };
  const exportJson = () => {
    const a = h('a', { href: URL.createObjectURL(new Blob([JSON.stringify(rules, null, 2)], { type: 'application/json' })), download: 'custom-commands.json' });
    document.body.append(a); a.click(); a.remove();
  };
  const file = h('input', { type: 'file', accept: '.json', class: 'hide', onchange: async e => {
    try {
      const data = JSON.parse(await e.target.files[0].text());
      const list2 = Array.isArray(data) ? data : data.rules;
      if (!Array.isArray(list2)) throw new Error('文件里不是指令表');
      if (!await confirmBox(`导入 ${list2.length} 条指令？会和现有的合并（同名的覆盖）。`, '导入')) return;
      const byName = new Map(rules.map(x => [x.name, x]));
      for (const x of list2) { const old = byName.get(x.name); if (old) Object.assign(old, x, { id: old.id }); else { const { id, ...rest } = x; rules.push(rest); } }
      await saveAll(); render();
    } catch (err) { toast('导入失败：' + err.message, 'err'); }
    e.target.value = '';
  } });
  clear(v, h('div.notice.mb8', icon('info'), h('div.small', '自己定义「群里有人这样说，就做这件事」：固定回复、让角色回答、执行内置指令（给它起个别名，比如「@她 睡觉」= 闭嘴 8 小时）、随机发一句。',
    '默认一条都没有。匹配顺序：自定义指令 → 内置管理指令 → 正常聊天；不提供脚本或代码执行；受影子模式、静默时段约束，用量硬上限生效时「让角色回答」不执行。')),
    h('div.row.wrap.mb8', h('button.primary', { onclick: () => editRule(null) }, icon('plus'), '添加指令'),
      h('button.sm', { onclick: exportJson }, icon('download'), '导出 JSON'), h('button.sm', { onclick: () => file.click() }, icon('upload'), '导入 JSON'), file,
      h('span.small.faint', '拖动左边的把手排序，越靠前越先匹配。')), list);
  render();
}, { order: 6.5 });
