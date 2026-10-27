/* 记忆、日志、用量。 */
'use strict';

const RESET_LABELS = { person: '人物备忘', recap: '群前情', meme: '群梗', doc: '文件片段', style: '群风格画像', sticker: '表情包', reminder: '提醒', context: '最近的聊天上下文' };
const SRC = { self: '本人说的', others: '别人说的', inferred: '她推测的', told: '本人要求记住' };
// 群（平台:群号）或私聊（dm:账号:对方）的名字
function scopeName(s) {
  if (s.startsWith('dm:')) { const [, aid, uid] = s.split(':'); const a = byId(B().accounts, aid); return `私聊「${uid}」（${a ? accountTitle(a) : aid}）`; }
  return `群「${roomName(s)}」`;
}
function spaceLabel(sp) {
  if (!sp) return '';
  const [t, ...rest] = sp.split(':');
  const r = rest.join(':');
  const roleName = id => (byId(B().roles, id) || {}).name || id;
  if (t === 'role') return '角色「' + roleName(r) + '」共用';
  if (t === 'chat') return scopeName(r);
  if (t === 'sp') return '空间「' + ((byId(B().spaces, r) || {}).name || r) + '」';
  if (t === 'iso') { const i = r.lastIndexOf(':person:'); return i < 0 ? sp : `${scopeName(r.slice(0, i))}的人物备忘（角色「${roleName(r.slice(i + 8))}」）`; }
  if (t === 'legacy') return `待认领：旧版共用的人物备忘（角色「${roleName(r)}」，看不出是哪个群 / 私聊的）`;
  return sp;
}

page('memory', '记忆', 'brain', async (v, [tab = 'items']) => {
  await reloadBoot();
  const body = h('div');
  clear(v, tabs([['items', '记忆条目'], ['style', '群风格画像'], ['stickers', '表情包'], ['reminders', '提醒'], ['semantic', '语义回忆'], ['spaces', '记忆空间'], ['import', '导入聊天记录']], tab, t => go('memory/' + t)), body);
  if (tab === 'spaces') return memorySpaces(body);
  if (tab === 'style') return stylePage(body);
  if (tab === 'stickers') return stickersPage(body);
  if (tab === 'reminders') return remindersPage(body);
  if (tab === 'semantic') return semanticPage(body);
  if (tab === 'import') return memoryImport(body);
  let kind = '', status = '', space = '', q = '';
  const list = h('div.col');
  const sel = new Set();
  const load = async () => {
    const r = await GET(`/api/memories?kind=${kind}&status=${status}&space=${encodeURIComponent(space)}&q=${encodeURIComponent(q)}`);
    spaceSel.replaceChildren(...[['', '全部空间'], ...r.spaces.map(s => [s, spaceLabel(s)])].map(([a, b]) => h('option', { value: a, selected: a === space }, b)));
    allBox.checked = false;
    clear(list, r.items.length ? r.items.map(m => memItem(m, sel, load)) : h('div.card.empty', icon('brain'), h('div', '还没有记忆。群里每聊够一段（默认 50 条）会自动整理一次。')));
  };
  const allBox = h('input', { type: 'checkbox', onchange: e => {
    list.querySelectorAll('input[data-mid]').forEach(cb => { cb.checked = e.target.checked; e.target.checked ? sel.add(+cb.dataset.mid) : sel.delete(+cb.dataset.mid); });
  } });
  const undoBar = h('div');
  const showUndo = async () => {
    const l = await GET('/api/memory/reset/last');
    clear(undoBar, l.ts && !l.undone ? h('div.notice.warn.mb8', icon('undo'), h('div.grow', `刚才重置了${l.scope_label}（${fmtTime(l.ts)}）：`,
      Object.entries(l.counts).filter(([, n]) => n).map(([k, n]) => `${RESET_LABELS[k]} ${n} 条`).join('、') || '没有东西被删', '。在下一次重置之前都可以撤销。'),
      h('button.sm.primary', { onclick: async () => { await POST('/api/memory/reset/undo'); toast('已撤销'); App.route(); } }, '撤销刚才的重置')) : null);
  };
  const spaceSel = h('select', { style: { maxWidth: '240px' }, onchange: e => { space = e.target.value; load(); } });
  add(body,
    h('div.row.wrap.mb8',
      seg([['', '全部'], ['person', '人物备忘'], ['impression', '人物印象'], ['recap', '群前情'], ['meme', '群梗']], kind, x => { kind = x; load(); }),
      seg([['', '全部状态'], ['pending', '待审核'], ['active', '已生效']], status, x => { status = x; load(); }),
      spaceSel, h('input', { placeholder: '搜索', style: { maxWidth: '200px' }, oninput: debounce(e => { q = e.target.value; load(); }, 300) })),
    undoBar,
    h('div.row.wrap.mb8',
      h('label.row.small', allBox, h('span', '全选当前筛选结果')),
      h('button.sm', { onclick: async () => { if (!sel.size) return toast('先勾选'); await POST('/api/memories/bulk', { ids: [...sel], action: 'approve' }); sel.clear(); load(); } }, icon('check'), '通过所选'),
      h('button.sm.danger', { onclick: async () => { if (!sel.size) return toast('先勾选'); if (await confirmBox(`删除所选 ${sel.size} 条记忆？`, '删除', true)) { await POST('/api/memories/bulk', { ids: [...sel], action: 'delete' }); sel.clear(); load(); } } }, icon('trash'), '删除所选'),
      h('button.sm.danger', { onclick: () => resetMemoryDialog() }, icon('refresh'), '重置记忆…'),
      h('span.faint.small', '新记忆是自动生效还是先等审核，在 设置 → 全局默认 → 记忆 里选。')),
    list);
  load();
  showUndo();
}, { order: 8 });

const unitDay = ts => { const d = new Date(ts * 1000); return `${d.getFullYear() === new Date().getFullYear() ? '' : d.getFullYear() + ' 年 '}${d.getMonth() + 1} 月 ${d.getDate()} 日`; };
const unitSpan = (t0, t1) => { const a = unitDay(t0), b = unitDay(t1); return a === b ? a : `${a}—${b}`; };

function memItem(m, sel, reload) {
  const src = h('div.hide.col.gap4.mt8');
  const content = h('textarea', { rows: 2, value: m.content, class: 'hide' });
  const text = h('div', { style: { whiteSpace: 'pre-wrap' } }, m.content);
  return h('div.card', { style: { padding: '12px' } },
    h('div.row.wrap', h('input', { type: 'checkbox', 'data-mid': m.id, checked: sel.has(m.id), onchange: e => e.target.checked ? sel.add(m.id) : sel.delete(m.id) }),
      h('span.badge', { person: '人物备忘', impression: '人物印象', recap: '群前情', meme: '群梗', doc: '文件片段' }[m.kind]),
      m.kind === 'recap' ? h('span.badge', `第 ${m.level || 1} 层`) : null,
      m.kind === 'recap' && (m.meta.t0 || m.meta.t1) ? h('span.badge.blue', unitSpan(m.meta.t0 || m.ts, m.meta.t1 || m.ts)) : null,
      m.kind === 'recap' && m.meta.m0 ? h('span.small.faint', `消息 #${m.meta.m0}–#${m.meta.m1}`) : null,
      m.kind === 'recap' && (m.meta.parts || []).length ? h('span.small.faint', `并自 ${m.meta.parts.length} 个下层单位`) : null,
      m.subject_name && h('b', m.kind === 'meme' ? '「' + m.subject_name + '」' : m.subject_name),
      m.kind === 'person' && h('span.badge.orange', SRC[m.source] || m.source),
      m.status === 'pending' && h('span.badge.warn', '待审核'), m.merged ? h('span.badge', '已合并进上一层') : null,
      h('div.grow'), h('span.small.faint', spaceLabel(m.space) + ' · ' + fmtTime(m.ts))),
    h('div.mt8', text, content),
    h('div.row.wrap.mt8',
      h('button.sm.ghost', { onclick: () => { src.classList.toggle('hide'); if (!src.childElementCount) add(src, m.src.length ? m.src.map(s => h('div.small', h('b', s.name + '：'), s.text)) : h('div.faint.small', '没有来源消息')); } }, icon('eye'), '来源消息'),
      h('button.sm.ghost', { onclick: async e => {
        if (content.classList.contains('hide')) { content.classList.remove('hide'); text.classList.add('hide'); e.currentTarget.lastChild.textContent = '保存'; }
        else { await PUT('/api/memories/' + m.id, { content: content.value }); m.content = content.value; text.textContent = content.value; content.classList.add('hide'); text.classList.remove('hide'); e.currentTarget.lastChild.textContent = '编辑'; toast('已保存'); }
      } }, icon('edit'), '编辑'),
      m.status === 'pending' && h('button.sm', { onclick: async () => { await PUT('/api/memories/' + m.id, { status: 'active' }); reload(); } }, icon('check'), '通过'),
      h('button.sm.ghost.danger', { onclick: async () => {
        if (m.kind === 'recap' && !(await confirmBox('删除这个前情单位？覆盖它的上层会一起删，上层所并的其他下层变回单独的单位，以后重新并。', '删除', true))) return;
        await DEL('/api/memories/' + m.id); reload();
      } }, icon('trash'), '删除')),
    m.kind === 'recap' ? h('div.small.faint.mt8', '改过的单位，覆盖它的上层之后照改过的重写。') : null,
    m.kind === 'impression' ? h('div.small.faint.mt8', '印象是这个人现在的情况；之后备忘被挤出最新那几条时，会在这份印象上接着更新。') : null,
    src);
}

function memorySpaces(body) {
  const spaceSwitches = h('div.card');
  const kinds = Object.entries(B().memory_kinds);
  const roles = [['*', '任何角色'], ...B().roles.map(r => [r.id, r.name])];
  const platsO = [['*', '任何平台'], ...PLAT_PICK];
  const dms = [];
  GET('/api/rooms?all=1').then(rs => dms.push(...rs.filter(x => x.type === 'private').map(x => [x.id, (PLAT[x.platform] || '') + ' 私聊 ' + (x.name || x.chat_id)])));
  const chatsO = () => [['*', '任何群 / 私聊'], ...B().chats.map(c => [c.id, (PLAT[c.platform] || '') + ' ' + (c.name || c.chat_id)]), ...dms];
  const editSpace = sp => {
    const d = JSON.parse(JSON.stringify(sp));
    d.kinds ||= ['person']; d.bindings ||= [{ role: '*', platform: '*', chat: '*' }];
    const bx = h('div.col');
    const render = () => clear(bx, d.bindings.map((b, i) => h('div.row.wrap', select(roles, b.role || '*', x => b.role = x), select(platsO, b.platform || '*', x => b.platform = x),
      select(chatsO(), b.chat || '*', x => b.chat = x), h('button.ghost.icon', { onclick: () => { d.bindings.splice(i, 1); render(); } }, icon('x')))),
      h('button.sm', { onclick: () => { d.bindings.push({ role: '*', platform: '*', chat: '*' }); render(); } }, icon('plus'), '加一条绑定'));
    render();
    modal(sp.id ? '编辑记忆空间' : '新记忆空间', h('div.form',
      field('名字', h('input', { value: d.name || '', oninput: e => d.name = e.target.value })),
      field('管哪几种记忆', chips(kinds, d.kinds, x => d.kinds = x)),
      field('绑定（满足任意一条就用这个空间）', bx, "比如：「角色 任何 · 平台 任何 · 群 A」+「… 群 B」= A、B 两个群共用一份（群换成某个私聊也行，群和私聊可以绑在一起）；「角色 A」+「角色 B」= 两个角色共享对人的认识")), {
      wide: true,
      foot: c => [sp.id && h('button.danger', { onclick: async () => { await DEL('/api/docs/space/' + sp.id); c(); App.route(); } }, '删除'), h('div.grow'),
        h('button', { onclick: c }, '取消'), h('button.primary', { onclick: async () => { if (!d.name) return toast('起个名字', 'err'); await PUT('/api/docs/space', d); c(); App.route(); } }, '保存')],
    });
  };
  clear(body,
    h('div.card', h('h3.mb8', '默认怎么存'), h('ul.small', { style: { margin: 0, paddingLeft: '20px', lineHeight: 1.9 } },
      h('li', '每个群、每个私聊得来的记忆（人物备忘、群前情、群梗、「记住」的事、文件片段、群风格画像、表情包）只在那个群、那个私聊里用；语义回忆也只在当前群 / 私聊的范围里找。'),
      h('li', '私聊按「哪个账号 × 哪个人」各自一份；人物备忘还按角色分开记。'),
      h('li', '想让几个群（或私聊）共享，在下面建一个自定义空间把它们绑在一起。自定义空间优先于默认的隔离，按排序匹配，排前面的先用。'),
      h('li', '人物表（人物卡、称呼、态度）不是记忆，所有群通用。'))),
    h('div.section-title', h('h2', '自定义记忆空间'), h('button.primary', { onclick: () => editSpace({}) }, icon('plus'), '新空间')),
    B().spaces.length ? h('div.grid', B().spaces.map(sp => h('div.card.col', { style: { cursor: 'pointer', gap: '6px' }, onclick: () => editSpace(sp) },
      h('b', sp.name), h('div.row.wrap', (sp.kinds || []).map(k => h('span.badge.blue', B().memory_kinds[k]))),
      h('div.small.muted', (sp.bindings || []).map(b => [b.role, b.platform, b.chat].map((x, i) => x && x !== '*' ? (i === 0 ? (byId(B().roles, x) || {}).name : i === 1 ? PLAT[x] : roomName(x)) : '任意').join(' · ')).join('；')))))
      : h('div.card.empty', '还没有自定义空间，全部按默认来'),
    h('div.section-title', h('h2', '各空间的语义回忆')),
    spaceSwitches);
  GET('/api/embed').then(info => clear(spaceSwitches, h('div.small.muted.mb8', '默认都打开；关掉的空间不参与语义回忆（其余三种取法照常）。总开关在「语义回忆」页。'),
    h('div.list', info.spaces.map(sp => h('div.item', h('div.grow', spaceLabel(sp.space), h('span.small.faint', `　${sp.count} 条`)),
      toggle(sp.on, async on => { await PUT('/api/memory/semantic_off', { space: sp.space, off: !on }); toast(on ? '已打开' : '已关闭'); })))),
    !info.spaces.length && h('div.faint.small', '还没有记忆')));
}

async function semanticPage(body) {
  const info = await GET('/api/embed');
  const c = info.conf;
  const statusBox = h('div');
  const renderStatus = st => clear(statusBox,
    h('div.row.wrap', h('span', `已建向量 ${st.indexed} / 记忆 ${st.total} 条`),
      st.rebuilding ? h('span.badge.warn', `重建中 ${st.progress[0]}/${st.progress[1]}`) : null,
      !st.model_ready ? h('span.badge.bad', '本机模型还没下载') : null),
    st.needs_rebuild && !st.rebuilding ? h('div.notice.warn.mt8', icon('alert'), h('div.grow', st.blocked
      ? `有 ${st.missing} 条记忆还没有可用的向量，暂时算不了：${st.blocked}`
      : `有 ${st.missing} 条记忆的向量过期或还没生成（${st.stale} 条是别的模型算的），程序会自动在后台重建，不用手动点；也可以点右边立刻开始。重建期间语义回忆照常可用（用已经算好的），其余取法照常。`),
      h('button.sm.primary', { onclick: async () => { await POST('/api/embed/rebuild'); toast('开始在后台重建'); } }, icon('refresh'), '一键重建')) : null,
    st.rebuilding ? h('div.mt8', h('div.bar', { style: { width: (100 * st.progress[0] / Math.max(1, st.progress[1])) + '%' } })) : null);
  renderStatus(info.status);
  live('embed', async d => { if (d.finished || d.error) renderStatus((await GET('/api/embed')).status); else renderStatus({ ...info.status, rebuilding: true, progress: [d.done, d.total] }); });
  const save = async patch => { const r = await PUT('/api/embed/config', patch); Object.assign(c, r.conf); renderStatus(r.status); toast('已保存'); };
  const prog = h('span.small.muted');
  live('download', d => { if (d.what !== 'embed') return; if (d.error) prog.textContent = d.error; else if (d.finished) { prog.textContent = '下载完成'; App.route(); } else prog.textContent = `${d.file}：${(d.done / 1048576).toFixed(1)}${d.total ? ' / ' + (d.total / 1048576).toFixed(1) : ''} MB`; });
  const localCards = h('div.col', Object.entries(info.local).map(([k, m]) => h('div.item', { class: c.preset === k ? 'on' : '' },
    h('input', { type: 'radio', name: 'lm', checked: c.preset === k, onchange: () => save({ source: 'local', preset: k }) }),
    h('div.grow', h('b', m.name), h('div.small.muted', `群聊记忆评测命中 ${m.chat}% · 公开中文检索分 ${m.retr} · 实测多占内存约 ${m.mem} MB、单句约 ${m.ms} 毫秒 · 文件约 ${m.mb} MB · ${m.dim} 维 · 许可证 ${m.license}`)),
    m.ready ? h('span.badge.ok', m.bundled ? '已随安装包' : '已下载') : h('button.sm', { onclick: async () => { prog.textContent = '开始下载…'; await POST('/api/embed/download', { preset: k }); } }, icon('download'), '下载'))),
    h('div.item', { class: c.preset === 'custom' ? 'on' : '' }, h('input', { type: 'radio', name: 'lm', checked: c.preset === 'custom', onchange: () => save({ source: 'local', preset: 'custom' }) }),
      h('div.grow', h('b', '自填本机模型'), h('div.small.muted', '一个文件夹，里面放 model.onnx 和 tokenizer.json'))),
    c.preset === 'custom' ? h('div.form2', field('文件夹', h('input', { value: c.local_folder, onchange: e => save({ local_folder: e.target.value.trim() }) })),
      field('取向量方式', select([['cls', 'CLS（BERT/BGE 类）'], ['mean', '平均'], ['last', '最后一个词（Qwen3 类）']], c.local_pooling, x => save({ local_pooling: x }))),
      field('检索时加在前面的说明（可空）', h('input', { value: c.local_prefix, onchange: e => save({ local_prefix: e.target.value }) }))) : null,
    h('div.small.faint', '下载依次试 ModelScope、Hugging Face、hf-mirror。都下不了时：手动下载 model.onnx 和 tokenizer.json，放进 ', h('code', info.models_dir + '\\<模型名>'), '（详见使用说明）。'), prog);
  const ap = info.api[c.api_preset] || {};
  const key = h('input', { type: 'password', placeholder: info.has_key ? '已保存（不显示）。要换就重新粘贴' : '粘贴向量接口的 API Key', autocomplete: 'off' });
  const apiBox = h('div.form',
    field('预设', select(Object.entries(info.api).map(([k, a]) => [k, `${a.name}（${a.price}/百万 tokens${a.retr ? '，中文检索分 ' + a.retr : ''}）`]), c.api_preset, x => save({ api_preset: x, api_base: '', api_model: '', api_dim: 0 }).then(() => App.route()))),
    h('div.form2', field('接口地址', h('input', { value: c.api_base, placeholder: ap.base_url, onchange: e => save({ api_base: e.target.value.trim() }) })),
      field('模型名', h('input', { value: c.api_model, placeholder: ap.model, onchange: e => save({ api_model: e.target.value.trim() }) }))),
    h('div.form2', field('维度（可空）', h('input', { type: 'number', value: c.api_dim || '', placeholder: String(ap.dim || ''), onchange: e => save({ api_dim: parseInt(e.target.value) || 0 }) })),
      field('API Key', h('div.row', key, h('button.sm', { onclick: async () => { if (key.value.trim()) { await PUT('/api/secret', { k: 'embed:api', v: key.value.trim() }); toast('已保存'); key.value = ''; } } }, '保存')))),
    h('div.small.faint', '走 OpenAI 兼容的 /embeddings 接口；会按接口价格计费（记在用量页「向量」用途下）。'));
  const ta = h('input', { value: '最近复习考研好累' }), tb = h('input', { value: '阿明在准备研究生入学考试' }), tr = h('span.small');
  add(body,
    h('div.notice.mb8', icon('info'), h('div.small', '语义回忆：按眼下聊的话题，在当前记忆空间里找意思相近的旧记忆（没提到当事人、没碰上关键词也能想起来），和原有三种取法一起用，同一条只放一次。默认用本机小模型，不调接口、不占模型额度。')),
    h('div.grid2',
      h('div.card', h('h3.mb8', '开关'), await settingsForm('global', { keys: ['semantic_enabled', 'semantic_top_k', 'semantic_min_score'], flat: true }), h('div.mt8', statusBox)),
      h('div.card', h('h3.mb8', '试一试'), h('div.form', field('一句眼下的话', ta), field('一条旧记忆', tb),
        h('div.row', h('button', { onclick: async () => { const r = await POST('/api/embed/test', { a: ta.value, b: tb.value }); tr.textContent = `相似度 ${r.similarity}（${r.dim} 维，${r.ms} 毫秒）`; } }, '算相似度'), tr)))),
    h('div.card.mt16', h('div.card-h', h('h3', '向量从哪来'), seg([['local', '本机模型（默认）'], ['api', '接口（API）']], c.source, x => save({ source: x }).then(() => App.route()))),
      c.source === 'local' ? localCards : apiBox,
      h('div.small.muted.mt8', '换来源或换模型后，旧记忆的向量不通用，上面会提示一键重建。')));
}

function memoryImport(body) {
  const file = h('input', { type: 'file', accept: '.json,.txt' });
  let room = (B().chats[0] || {}).id || '', roles = B().roles.map(r => r.id);
  const out = h('div');
  const fd = dry => { const f = new FormData(); f.append('file', file.files[0]); f.append('room', room); f.append('roles', roles.join(',')); if (dry) f.append('dry', '1'); return f; };
  clear(body, h('div.card.form',
    h('div', '把以前的群聊记录导进来，让她从一开始就有前情、认识群友、知道群梗。支持 QQ 聊天记录导出插件生成的 JSON / TXT，也支持 QQ 自带「导出聊天记录」的 TXT。'),
    field('聊天记录文件', file),
    field('导入到哪个群', B().chats.length ? select(B().chats.map(c => [c.id, (PLAT[c.platform] || '') + ' ' + (c.name || c.chat_id)]), room, x => room = x) : h('div.faint', '机器人进过群之后才能选')),
    field('人物备忘记给哪些角色', chips(B().roles.map(r => [r.id, r.name]), roles, x => roles = x)),
    h('div.row', h('button.primary', { onclick: async () => {
      if (!file.files[0] || !room) return toast('先选文件和群', 'err');
      const r = await POST('/api/memory/import', fd(true));
      if (!(await confirmBox(`认出 ${r.messages} 条消息，要调用记忆模型约 ${r.calls} 次（每次一段）。开始导入？`, '开始导入'))) return;
      await POST('/api/memory/import', fd(false));
      clear(out, h('div.muted', '导入中…可以离开这个页面，进度在日志里。'));
    } }, icon('upload'), '分析并导入')), out));
  live('import', d => clear(out, h('div.muted', `进度：${d.done} / ${d.total} 条`)));
}

// ---------- 日志 ----------
page('logs', '日志', 'list', async v => {
  let cat = '', q = '', debug = false;
  const list = h('div');
  const row = l => h('div.log', { class: l.level }, h('time', fmtTime(l.ts)), h('span.badge', catName(l.cat)),
    l.cat === 'gatekeeper' && l.data ? h('span.badge', { class: l.data.allow ? 'ok' : '' }, l.data.allow ? '插' : '不插') : null, h('span.txt', dispText(l.text)));
  const load = async () => {
    const rows = await GET(`/api/logs?cat=${cat}&q=${encodeURIComponent(q)}${debug ? '&debug=1' : ''}&limit=300`);
    clear(list, rows.map(row), !rows.length && h('div.empty', '没有日志'));
  };
  live('log', l => {
    if ((cat && l.cat !== cat) || (q && !l.text.includes(q)) || (!debug && l.level === 'debug')) return;
    list.prepend(row(l));
    if (list.children.length > 500) list.lastChild.remove();
  });
  clear(v, h('div.row.wrap.mb8', seg([['', '全部'], ...Object.entries(CATS)], cat, x => { cat = x; load(); })),
    h('div.row.wrap.mb8', h('input', { placeholder: '搜索', style: { maxWidth: '260px' }, oninput: debounce(e => { q = e.target.value; load(); }, 300) }),
      h('label.row.small', toggle(debug, x => { debug = x; load(); }), '显示 NapCat 原始输出'), h('div.grow'),
      h('span.small.faint', '守门员每次插或不插都有理由；日志保留天数在 设置 → 系统。')),
    h('div.card', { style: { padding: '4px 12px' } }, list));
  load();
}, { order: 9 });

// ---------- 用量 ----------
page('usage', '用量', 'chart', async v => {
  let by = 'day', days = 30;
  const box = h('div');
  const label = (k) => by === 'role' ? ((byId(B().roles, k) || {}).name || k || '（无角色）') : by === 'room' ? (k ? roomName(k) : '私聊/其他')
    : by === 'purpose' ? ({ main: '主模型', gk: '守门员', mem: '记忆提取', vis: '看图', trial: '试聊/体验', embed: '向量', search: '搜索', sticker: '学表情包', sticker_search: '表情包搜索', compress: '压缩回复', stale: '过时判断' }[k] || k) : by === 'platform' ? (PLAT[k] || k || '—') : k;
  const pct = x => x == null ? '—' : (x * 100).toFixed(1) + '%';
  const trendChart = trend => {
    if (!trend.length) return h('div.empty.small', '这段时间还没有用量');
    const W = 900, H = 120, pad = 22, bw = Math.max(3, Math.min(24, (W - pad) / trend.length - 3));
    const max = Math.max(1, ...trend.map(t => t.prompt + t.completion));
    const NS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', `0 0 ${W} ${H + 18}`); svg.setAttribute('class', 'trend'); svg.style.width = '100%';
    const el = (n, a) => { const e = document.createElementNS(NS, n); for (const [k, v] of Object.entries(a)) e.setAttribute(k, v); return e; };
    trend.forEach((t, i) => {
      const x = pad + i * ((W - pad) / trend.length), hh = H * (t.prompt + t.completion) / max, hc = H * Math.min(t.cached, t.prompt) / max;
      const r = el('rect', { x, y: H - hh, width: bw, height: Math.max(1, hh), rx: 2, fill: 'var(--primary)', opacity: .85 });
      const title = document.createElementNS(NS, 'title'); title.textContent = `${t.day}：输入 ${t.prompt}（缓存 ${t.cached}）、输出 ${t.completion}，${t.calls} 次调用`; r.append(title);
      svg.append(r, el('rect', { x, y: H - hc, width: bw, height: hc, rx: 2, fill: 'var(--ok)', opacity: .95 }));
    });
    const t0 = el('text', { x: pad, y: H + 14, fill: 'currentColor', 'font-size': 11, opacity: .6 }); t0.textContent = trend[0].day;
    const t1 = el('text', { x: W, y: H + 14, fill: 'currentColor', 'font-size': 11, opacity: .6, 'text-anchor': 'end' }); t1.textContent = trend[trend.length - 1].day;
    svg.append(t0, t1);
    return svg;
  };
  const load = async () => {
    const [rows, sum] = await Promise.all([GET(`/api/usage?by=${by}&days=${days}`), GET(`/api/usage/summary?days=${days}`)]);
    const max = Math.max(1, ...rows.map(r => r.prompt + r.completion));
    const t = sum.total;
    const showR = sum.has_reasoning, showW = sum.has_cache_write;
    clear(box, h('div.grid.mb8',
      h('div.card.stat', h('span.muted', '调用次数'), h('b', t.calls)),
      h('div.card.stat', h('span.muted', '输入 tokens'), h('b', fmtNum(t.prompt)), h('span.small.muted', `其中缓存命中 ${fmtNum(t.cached)}`)),
      h('div.card.stat', h('span.muted', '缓存命中率'), h('b', pct(sum.hit_rate)), h('span.small.muted', '命中的输入 ÷ 全部输入')),
      h('div.card.stat', h('span.muted', '输出 tokens'), h('b', fmtNum(t.completion)), showR ? h('span.small.muted', `其中思考 ${fmtNum(t.reasoning)}`) : null),
      h('div.card.stat', h('span.muted', '平均每条回复'), h('b', fmtNum(sum.avg_reply.prompt + sum.avg_reply.completion)),
        h('span.small.muted', `输入 ${fmtNum(sum.avg_reply.prompt)} + 输出 ${fmtNum(sum.avg_reply.completion)}（主模型 ${sum.avg_reply.calls} 次）`)),
      showW ? h('div.card.stat', h('span.muted', '缓存写入 tokens'), h('b', fmtNum(t.cache_write)), h('span.small.muted', '服务商返回了才显示')) : null,
      h('div.card.stat', h('span.muted', '预筛省下的守门员调用'), h('b', sum.gk_saved + ' 次'), h('span.small.muted', `实际问了守门员 ${sum.gk_calls} 次`)),
      sum.searches ? h('div.card.stat', h('span.muted', '联网搜索'), h('b', sum.searches + ' 次'), h('span.small.muted', '用途「搜索」里还有带着搜索结果的那一轮模型调用')) : null),
      sum.hard_cap && sum.hard_cap.active ? h('div.notice.warn.mb8', icon('alert'), h('div.small', `用量硬上限生效中：今天已用 ${fmtNum(sum.hard_cap.used)} tokens，上限 ${fmtNum(sum.hard_cap.limit)}。被@和私聊只回一句固定的话，记忆整理、看图、搜索等暂停；明天自动恢复。`)) : null,
      h('div.card.mb8', h('div.card-h', h('h3', '趋势'), h('span.small.faint', '蓝色 = 每天输入 + 输出，绿色 = 其中缓存命中的输入')), trendChart(sum.trend)),
      h('div.card.table-wrap', h('table.tbl', h('tr', h('th', { day: '日期', role: '角色', room: '群', model: '模型', purpose: '用途', platform: '平台' }[by]), h('th', '调用'), h('th', '输入'), h('th', '缓存'), h('th', '命中率'),
        showW ? h('th', '缓存写入') : null, h('th', '输出'), showR ? h('th', '思考') : null, h('th.desktop-only', '')),
        rows.map(r => h('tr', h('td', label(r.k)), h('td', r.calls), h('td', fmtNum(r.prompt)), h('td', fmtNum(r.cached)), h('td', pct(r.prompt ? r.cached / r.prompt : null)),
          showW ? h('td', fmtNum(r.cache_write)) : null, h('td', fmtNum(r.completion)), showR ? h('td', fmtNum(r.reasoning)) : null,
          h('td.desktop-only', h('div.bar', { style: { width: (100 * (r.prompt + r.completion) / max) + '%' } })))),
        !rows.length && h('tr', h('td', { colSpan: 9 }, h('div.empty', '还没有用量'))))),
      h('div.small.faint.mt8', '这里只统计 token 和次数，不换算成钱（各家价格和优惠不同，以服务商账单为准）。思考 token、缓存写入只在服务商返回了才有。'));
  };
  const exportCsv = async () => {
    const r = await fetch(`/api/usage/export.csv?by=${by}&days=${days}`);
    const a = h('a', { href: URL.createObjectURL(await r.blob()), download: `usage-${by}.csv` }); document.body.append(a); a.click(); a.remove();
  };
  clear(v, h('div.row.wrap.mb8', seg([['day', '按天'], ['role', '按角色'], ['room', '按群'], ['purpose', '按用途'], ['model', '按模型'], ['platform', '按平台']], by, x => { by = x; load(); }),
    select([[1, '今天'], [7, '近 7 天'], [30, '近 30 天'], [365, '近一年']], days, x => { days = +x; load(); }),
    h('button.sm', { onclick: exportCsv }, icon('download'), '导出 CSV'),
    h('span.small.faint', '每日用量上限在 设置 → 模型与接口。')), box);
  await reloadBoot();
  load();
}, { order: 10 });
