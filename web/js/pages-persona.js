/* 人设：模块拼装、整体视图、历史版本、试聊、构型库。 */
'use strict';

const KIND_NAME = { shared: '共用', exclusive: '专属', platform: '平台', archetype: '构型', people: '人物表', memory: '记忆' };
const kindTag = k => h('span.kind', { class: k }, KIND_NAME[k] || k);

page('personas', '人设', 'mask', async (v, [pid]) => {
  await reloadBoot();
  const list = h('div.list');
  const renderList = () => clear(list, B().personas.map(p => h('div.item.click', { 'data-id': p.id, class: p.id === pid ? 'on' : '', onclick: e => { if (!e.target.closest('.handle')) go('personas/' + p.id); } },
    h('span.handle', icon('grip')), h('div.grow.ellipsis', p.name), p.mode === 'whole' ? h('span.badge', '整体') : null,
    h('span.small.faint', B().roles.filter(r => r.persona === p.id).map(r => r.name).join('、')))));
  renderList();
  sortable(list, async ids => { await POST('/api/docs/persona/order', { ids }); B().personas.sort((a, b) => ids.indexOf(a.id) - ids.indexOf(b.id)); });
  const left = h('div.card', { style: { padding: '8px' } },
    h('div.row', { style: { padding: '4px 6px 8px' } }, h('h3.grow', '人设库'),
      h('button.sm', { onclick: async () => {
        const name = await promptBox('新建人设', '名字');
        if (!name) return;
        const p = await PUT('/api/docs/persona', { name, mode: 'modules', items: [{ m: '@people', on: true }, { m: '@memory', on: true }], whole: '', _note: '新建' });
        go('personas/' + p.id);
      } }, icon('plus'), '新建')),
    list, h('div.item.click.mt8', { onclick: () => go('personas/_modules') }, icon('copy'), h('div.grow', '共用模块库'), icon('chev')));
  const right = h('div');
  clear(v, h('div.split', left, right));
  if (pid === '_modules') return moduleLibrary(right);
  const p = byId(B().personas, pid) || B().personas[0];
  if (!p) return add(right, h('div.card.empty', '还没有人设'));
  if (!pid) { history.replaceState(null, '', '#/personas/' + p.id); $$('.item', list)[0]?.classList.add('on'); }
  personaEditor(right, p);
}, { order: 5 });

function personaEditor(root, p) {
  p = JSON.parse(JSON.stringify(p));
  const mods = Object.fromEntries(B().modules.map(m => [m.id, JSON.parse(JSON.stringify(m))]));
  const dirtyMods = new Set();
  let platform = 'qq', peopleMode = 'all';
  const preview = h('pre.preview');
  const chars = h('span.small.faint');
  const status = h('span.small.faint');
  const refresh = debounce(async () => {
    const r = await POST('/api/personas/preview', { persona: p, modules: [...dirtyMods].map(id => mods[id]), platform, people: peopleMode });
    preview.textContent = r.text; chars.textContent = `${r.chars} 字`;
  }, 250);
  const saveNow = async () => {
    for (const id of dirtyMods) { if (mods[id]) await PUT('/api/docs/module', mods[id]); }
    dirtyMods.clear();
    await PUT('/api/docs/persona', { ...p, _note: '' });
    status.textContent = '已保存 ' + new Date().toLocaleTimeString();
    await reloadBoot();
  };
  const autosave = debounce(saveNow, 1200);
  const changed = (modId) => { if (modId) dirtyMods.add(modId); status.textContent = '未保存…'; refresh(); autosave(); };

  const modList = h('div');
  const renderMods = () => {
    clear(modList, p.items.map((it, idx) => {
      const m = mods[it.m];
      if (!m) return null;
      const body = h('div.mod-b.hide');
      const used = B().personas.filter(x => x.id !== p.id && x.items.some(i => i.m === m.id)).length;
      const fillBody = () => {
        clear(body);
        if (m.kind === 'people') add(body, h('div.small.muted', '按当前对话自动生成：只放对话里出现的人的人物卡（身份、称呼、态度、她知道的事），以及同群的其他机器人。到「人物」页编辑。'));
        else if (m.kind === 'memory') add(body, h('div.small.muted', '按当前对话自动带上：在场的人的备忘、本群前情、提到的群梗。到「记忆」页查看。'));
        else if (m.kind === 'archetype') {
          const a = B().archetypes.find(x => x.id === m.archetype);
          add(body, field('段首说明', h('textarea', { rows: 3, value: m.intro ?? '', oninput: e => { m.intro = e.target.value; changed(m.id); } })),
            h('div.row', h('div.grow', h('b', a ? a.title : '未选构型')), h('button.sm', { onclick: () => pickArchetype(id => { m.archetype = id; changed(m.id); renderMods(); }) }, '换构型')),
            a && h('div.small.muted', '核心命题：' + a.core));
        } else {
          add(body,
            m.kind === 'shared' && used ? h('div.notice.warn.small', icon('alert'), `共用模块：另有 ${used} 个人设在用，改这里它们一起变。`) : null,
            h('div.form2', field('名字', h('input', { value: m.name, oninput: e => { m.name = e.target.value; changed(m.id); } })),
              field('所在小节', h('input', { value: m.section || '', placeholder: '比如：说话（同小节的模块合在一个【标题】下）', oninput: e => { m.section = e.target.value; changed(m.id); } }))),
            m.kind === 'platform' && field('挂在哪些平台', chips(PLAT_PICK, m.platforms || [], x => { m.platforms = x; changed(m.id); })),
            m.kind !== 'platform' && field('受「承认是AI」开关管', toggle(m.gate === 'admit_ai', x => { if (x) m.gate = 'admit_ai'; else delete m.gate; changed(m.id); }),
              '打开后，总览页的「承认是AI」关着时，这个模块整个不出现。'),
            h('textarea', { rows: Math.min(14, Math.max(3, (m.body || '').split('\n').length + 1)), value: m.body || '', oninput: e => { m.body = e.target.value; changed(m.id); } }),
            h('div.field-help', '文字里写 [[AI|这段话]]，这段话也受「承认是AI」开关管：开着照常出现，关着不出现。'));
        }
      };
      const head = h('div.mod-h', h('span.handle', icon('grip')),
        toggle(it.on !== false, x => { it.on = x; changed(); renderMods(); }, '开关这个模块'),
        h('span.name.ellipsis', { onclick: () => { body.classList.toggle('hide'); if (!body.classList.contains('hide')) fillBody(); } }, m.name,
          h('span.snip.desktop-only', (m.kind === 'archetype' ? (B().archetypes.find(a => a.id === m.archetype) || {}).title : (m.body || '').replace(/\s+/g, ' ')) || '')),
        kindTag(m.kind), m.kind === 'platform' ? (() => { const t = (m.platforms || []).filter(x => PLAT[x]).map(x => PLAT[x]).join('、'); return h('span.small.faint.plats', { title: t }, t); })() : null,
        h('button.ghost.icon', { title: '从这个人设里移除', onclick: async () => {
          if (m.kind === 'exclusive' && !(await confirmBox(`移除专属模块「${m.name}」？它只属于这个人设，移除后会删掉（可从历史版本恢复）。`, '移除', true))) return;
          p.items.splice(idx, 1);
          if (m.kind === 'exclusive') await DEL('/api/docs/module/' + m.id);
          changed(); renderMods();
        } }, icon('x')));
      return h('div.mod', { 'data-id': it.m, class: it.on === false ? 'off' : '' }, head, body);
    }));
  };
  sortable(modList, ids => { p.items.sort((a, b) => ids.indexOf(a.m) - ids.indexOf(b.m)); changed(); });

  const addMenu = () => {
    const have = new Set(p.items.map(i => i.m));
    const lib = B().modules.filter(m => m.kind !== 'exclusive' && !have.has(m.id));
    const close = modal('添加模块', h('div.col.gap12',
      h('div.row.wrap',
        h('button', { onclick: async () => { close(); await newModule('exclusive'); } }, icon('plus'), '新建专属模块'),
        h('button', { onclick: async () => { close(); await newModule('shared'); } }, icon('plus'), '新建共用模块'),
        h('button', { onclick: async () => { close(); await newModule('platform'); } }, icon('plus'), '新建平台模块'),
        h('button', { onclick: () => { close(); pickArchetype(async id => { await newModule('archetype', id); }); } }, icon('star'), '从构型库选')),
      h('h4', '从共用库添加'),
      lib.length ? h('div.list', lib.map(m => h('div.item.click', { onclick: () => { p.items.push({ m: m.id, on: true }); changed(); renderMods(); close(); } },
        kindTag(m.kind), h('div.grow', m.name), h('span.small.faint.ellipsis', { style: { maxWidth: '50%' } }, (m.body || m.archetype || '').slice(0, 40)))))
        : h('div.faint.small', '共用库里的模块都已经加上了')));
  };
  const newModule = async (kind, archetype) => {
    const name = kind === 'archetype' ? (B().archetypes.find(a => a.id === archetype) || {}).title : await promptBox('新建模块', '模块名字');
    if (!name) return;
    const m = await PUT('/api/docs/module', { name, kind, section: kind === 'archetype' ? '她看世界的根本方式' : '', body: '',
      platforms: kind === 'platform' ? ['qq', 'wechat'] : undefined, owner: kind === 'exclusive' ? p.id : undefined,
      archetype, intro: kind === 'archetype' ? ARCH_INTRO : undefined });
    mods[m.id] = m; B().modules.push(m);
    const memIdx = p.items.findIndex(i => i.m === '@memory');
    p.items.splice(memIdx >= 0 ? memIdx : p.items.length, 0, { m: m.id, on: true });
    changed(); renderMods();
  };

  const whole = h('textarea.code', { rows: 24, value: p.whole || '', oninput: e => { p.whole = e.target.value; changed(); } });
  const editArea = p.mode === 'whole'
    ? h('div.col', h('div.notice', icon('info'), h('div.small', '这个人设是一整块自由文字。', h('code', '{{人物表}}'), ' 和 ', h('code', '{{记忆}}'), ' 两处会按当前对话自动替换。')), whole,
      h('button.sm', { onclick: async () => { if (await confirmBox('改回模块拼装？整块文字会保留在历史版本里，模块按转换前的样子恢复。')) { p.mode = 'modules'; changed(); await saveNow(); App.route(); } } }, '改回模块拼装'))
    : h('div', modList, h('button.mt8', { onclick: addMenu }, icon('plus'), '添加模块'));
  if (p.mode !== 'whole') renderMods();

  clear(root,
    h('div.card',
      h('div.card-h', h('input', { value: p.name, style: { fontSize: '17px', fontWeight: 650, maxWidth: '280px' }, oninput: e => { p.name = e.target.value; changed(); } }),
        h('div.grow'), status,
        h('button.sm', { onclick: () => trialChat(p) }, icon('chat'), '试聊'),
        h('button.sm', { onclick: () => historyDrawer(p) }, icon('history'), '历史版本'),
        h('button.sm.ghost', { title: '复制一份', onclick: async () => { await saveNow(); const c = await POST(`/api/personas/${p.id}/copy`); go('personas/' + c.id); } }, icon('copy')),
        h('button.sm.ghost.danger', { title: '删除', onclick: async () => {
          if (B().roles.some(r => r.persona === p.id)) return toast('还有角色在用这个人设，先给角色换一个', 'err');
          if (await confirmBox(`删除人设「${p.name}」？`, '删除', true)) { await DEL('/api/docs/persona/' + p.id); go('personas'); }
        } }, icon('trash'))),
      h('div.small.muted.mb8', '每次保存自动留历史版本。拖左边的把手排序；点模块名展开编辑。'),
      editArea),
    h('div.card',
      h('div.card-h', h('h3', '整体视图'), chars,
        seg(PLAT_PICK, platform, x => { platform = x; refresh(); }),
        select([['all', '示例：人物表里的人都在场'], ['none', '示例：没有人物表里的人']], peopleMode, x => { peopleMode = x; refresh(); }),
        h('button.sm', { onclick: () => { navigator.clipboard.writeText(preview.textContent); toast('已复制'); } }, icon('copy'), '复制'),
        p.mode !== 'whole' && h('button.sm', { onclick: async () => {
          if (!(await confirmBox(`把「${p.name}」按 ${PLAT[platform]} 的拼法转成一整块自由文字？之后模块的改动不会再同步到它（可从历史版本恢复）。`, '转成整体'))) return;
          await saveNow(); await POST(`/api/personas/${p.id}/to_whole`, { platform }); App.route();
        } }, '转成整体')),
      h('div.small.muted.mb8', '这就是实际发给模型的系统提示词（人物表、记忆两块按对话自动生成，这里用示例）。'),
      preview));
  refresh();
  S.cleanup.push(() => { if (status.textContent.startsWith('未保存')) saveNow(); });
}

const ARCH_INTRO = '下面是她自己骨子里的看法，不是台词，也不在群里复述；她怎么接话、怎么对人，由这份看法同她的身份和眼前的事一起决定。';

function pickArchetype(onpick) {
  let q = '';
  const list = h('div.list');
  const render = () => clear(list, B().archetypes.filter(a => !q || (a.title + a.core + a.reality + a.tendency).includes(q)).map(a =>
    h('div.item.click', { style: { alignItems: 'flex-start' }, onclick: () => { close(); onpick(a.id); } },
      h('div.grow', h('b', a.title), a.src && a.src !== '随程序' ? h('span.small.faint', ' 来自 ' + a.src) : null,
        h('div.small.muted', '核心命题：' + a.core), h('div.small.faint', '人物倾向：' + a.tendency)))));
  render();
  const file = h('input', { type: 'file', accept: '.json,application/json', multiple: true, style: { display: 'none' }, onchange: async () => {
    let n = 0, bad = 0;
    for (const f of file.files) {
      const fd = new FormData(); fd.append('file', f);
      try { const r = await POST('/api/archetypes/import', fd); n += r.imported; bad += r.skipped; } catch (e) { /* api() 已提示 */ }
    }
    await reloadBoot(); render(); toast(`导入了 ${n} 条构型` + (bad ? `，${bad} 条格式不对已跳过` : ''));
  } });
  const close = modal('构型库', h('div.col',
    h('div.notice.mb8', icon('info'), h('div.small', '安装包里只自带「构型171」（海豚娘用的）。要用别的构型，点「导入构型文件」选作者卡里的构型 JSON（可以一次选几个）。',
      '导入时只取 id、核心命题、现实展开、人物倾向这几项，文件里别的内容一律丢弃、不会进程序。')),
    h('div.row.wrap', h('input', { placeholder: '搜索', oninput: e => { q = e.target.value; render(); } }),
      h('button', { onclick: () => file.click() }, icon('upload'), '导入构型文件'), file),
    list), { wide: true });
}

async function historyDrawer(p) {
  const rows = await GET('/api/history/' + p.id);
  const view = h('pre.preview', { style: { maxHeight: '40vh' } }, '点左边的版本查看');
  modal('历史版本：' + p.name, h('div.grid2',
    h('div.list', rows.map(r => h('div.item.click', { onclick: async () => {
      const d = await GET('/api/history/item/' + r.id);
      view.textContent = d.persona.mode === 'whole' ? d.persona.whole
        : d.persona.items.map(it => { const m = (d.modules.find(x => x.id === it.m) || byId(B().modules, it.m)); return `${it.on === false ? '（关）' : ''}${m ? m.name : it.m}${m && m.body ? '\n' + m.body : ''}`; }).join('\n\n');
    } }, h('div.grow', fmtTime(r.ts), h('div.small.faint', r.note || '保存')),
      h('button.sm', { onclick: async e => { e.stopPropagation(); if (await confirmBox('恢复到这个版本？当前内容会先存一个版本。')) { await POST(`/api/history/item/${r.id}/restore`); toast('已恢复'); App.route(); } } }, '恢复')))),
    view), { wide: true });
}

function trialChat(p) {
  const roles = B().roles;
  let role = (roles.find(r => r.persona === p.id) || roles[0] || {}).id;
  let platform = 'qq', mode = 'at';
  const lines = [{ name: '群友A', person_id: '', text: '今天这破天气谁顶得住' }];
  const box = h('div.col');
  const out = h('div.col');
  const people = [['', '（不在人物表的群友）'], ...B().people.map(x => [x.id, pLabel(x)])];
  const render = () => clear(box, lines.map((l, i) => h('div.row',
    h('div', { style: { width: '140px', flex: 'none' } }, select(people, l.person_id, x => { l.person_id = x; if (x) l.name = byId(B().people, x).name || '用户'; render(); })),
    !l.person_id ? h('input', { value: l.name, style: { width: '100px', flex: 'none' }, oninput: e => l.name = e.target.value }) : null,
    h('input', { value: l.text, oninput: e => l.text = e.target.value }),
    h('button.ghost.icon', { onclick: () => { lines.splice(i, 1); render(); } }, icon('x')))),
    h('button.sm', { onclick: () => { lines.push({ name: '群友B', person_id: '', text: '' }); render(); } }, icon('plus'), '加一条'));
  render();
  const run = h('button.primary', { onclick: async () => {
    run.disabled = true; clear(out, h('div.muted', '正在调用模型…'));
    try {
      const r = await POST('/api/trial', { role_id: role, platform, lines: lines.filter(l => l.text.trim()), mode });
      const u = r.usage || {};
      clear(out,
        r.gatekeeper && h('div.notice', { class: r.gatekeeper.reply ? 'ok' : 'warn' }, icon(r.gatekeeper.reply ? 'check' : 'x'),
          h('div', h('b', r.gatekeeper.reply ? '守门员：插话' : '守门员：不插'), h('div.small', r.gatekeeper.brief))),
        r.reply && h('div.card', h('div.small.muted.mb8', '她会说：'), h('div', { style: { whiteSpace: 'pre-wrap' } }, r.reply)),
        h('div.small.faint', `本次用量：${u.calls || 0} 次调用，输入 ${u.prompt || 0}（缓存 ${u.cached || 0}），输出 ${u.completion || 0} tokens`),
        r.system && h('details', h('summary.small.muted', '这次的系统提示词'), h('pre.preview', r.system)));
    } catch (e) { clear(out, h('div.notice.bad', icon('alert'), e.message)); }
    finally { run.disabled = false; }
  } }, icon('play'), '试一下');
  modal('试聊：' + p.name, h('div.col.gap12',
    h('div.small.muted', '模拟一段群聊，最后一条是要她回应的。会真的调用模型（计入用量，记为「试聊」）。人设用的是已保存的版本。'),
    h('div.row.wrap', field('角色', select(roles.map(r => [r.id, r.name]), role, x => role = x)),
      field('平台', select(PLAT_PICK, platform, x => platform = x)),
      field('场景', seg([['at', '最后一条@了她'], ['auto', '没人@她（看守门员）']], mode, x => mode = x))),
    box, h('div', run), out), { wide: true });
}

function moduleLibrary(root) {
  const mods = B().modules.filter(m => m.kind !== 'exclusive');
  clear(root, h('div.card',
    h('div.card-h', h('h3', '共用模块库'), h('span.small.muted', '改一处，所有用到的人设一起变')),
    h('div.list', mods.map(m => {
      const users = B().personas.filter(p => p.items.some(i => i.m === m.id)).map(p => p.name);
      const body = h('div.hide.form.mt8');
      const save = debounce(async () => { await PUT('/api/docs/module', m); toast('已保存'); }, 800);
      return h('div', { style: { borderBottom: '1px solid var(--border)', padding: '8px 4px' } },
        h('div.row', kindTag(m.kind), h('b.grow', { style: { cursor: 'pointer' }, onclick: () => {
          body.classList.toggle('hide');
          if (!body.childElementCount && !['people', 'memory'].includes(m.kind)) add(body,
            m.kind === 'archetype' ? h('div.small.muted', '构型：' + m.archetype + '（在人设编辑里换）')
              : h('textarea', { rows: 6, value: m.body || '', oninput: e => { m.body = e.target.value; save(); } }));
        } }, m.name), h('span.small.faint', users.length ? '用在：' + users.join('、') : '没人用'),
          !m.id.startsWith('@') && h('button.ghost.icon', { onclick: async () => {
            if (await confirmBox(`删除共用模块「${m.name}」？用到它的人设会去掉这一块。`, '删除', true)) { await DEL('/api/docs/module/' + m.id); App.route(); }
          } }, icon('trash'))), body);
    }))));
}
