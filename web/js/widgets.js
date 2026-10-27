/* 通用小部件：开关、标签输入、分段、页签、拖动排序、态度编辑、分层设置表单。 */
'use strict';

function toggle(checked, onchange, title) {
  const inp = h('input', { type: 'checkbox', checked: !!checked, onchange: () => onchange(inp.checked) });
  return h('label.switch', { title }, inp, h('i'));
}
function select(options, value, onchange) {
  const s = h('select', { onchange: () => onchange(s.value) },
    options.map(([v, t]) => h('option', { value: v, selected: String(v) === String(value) }, t)));
  return s;
}
function field(label, ctl, help) {
  return h('label.field', h('span', label), ctl, help && h('div.field-help', help));
}
function tagsInput(values, onchange, placeholder = '输入后回车') {
  let vals = [...(values || [])];
  const box = h('div.tags');
  const inp = h('input', { placeholder });
  const render = () => {
    clear(box, vals.map((v, i) => h('span.chip', { 'data-raw': '1' }, v, h('span.x', { onclick: () => { vals.splice(i, 1); render(); onchange(vals); } }, '×'))), inp);
  };
  const commit = () => {
    const parts = inp.value.split(/[,，\n]/).map(s => s.trim()).filter(Boolean);
    if (!parts.length) return;
    vals = [...new Set([...vals, ...parts])]; inp.value = ''; render(); onchange(vals); inp.focus();
  };
  inp.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ',' || e.key === '，') { e.preventDefault(); commit(); }
    else if (e.key === 'Backspace' && !inp.value && vals.length) { vals.pop(); render(); onchange(vals); }
  });
  inp.addEventListener('blur', commit);
  render();
  return box;
}
function seg(options, value, onchange) {
  const el = h('div.seg');
  const render = () => clear(el, options.map(([v, t]) => h('button', { class: v === value ? 'on' : '', onclick: () => { value = v; render(); onchange(v); } }, t)));
  render();
  return el;
}
function tabs(items, active, onchange) {
  const el = h('div.tabs');
  const render = () => clear(el, items.map(([k, t]) => h('button', { class: k === active ? 'on' : '', onclick: () => { active = k; render(); onchange(k); } }, t)));
  render();
  return el;
}
function chips(options, values, onchange) {
  const set = new Set(values || []);
  const el = h('div.row.wrap');
  const render = () => clear(el, options.map(([v, t]) => h('span.chip', { class: set.has(v) ? 'on' : '', onclick: () => { set.has(v) ? set.delete(v) : set.add(v); render(); onchange([...set]); } }, t)));
  render();
  return el;
}

/* 拖动排序：鼠标、手指都能用（Pointer Events），只能从把手拖。 */
function sortable(list, onorder) {
  let drag = null;
  list.addEventListener('pointerdown', e => {
    const handle = e.target.closest('.handle');
    if (!handle || !list.contains(handle)) return;
    drag = handle.closest('[data-id]');
    if (!drag || drag.parentElement !== list) { drag = null; return; }
    e.preventDefault();
    drag.classList.add('dragging');
    handle.setPointerCapture(e.pointerId);
  });
  list.addEventListener('pointermove', e => {
    if (!drag) return;
    const sibs = [...list.children].filter(c => c !== drag && c.dataset.id);
    const after = sibs.find(c => { const r = c.getBoundingClientRect(); return e.clientY < r.top + r.height / 2; });
    after ? list.insertBefore(drag, after) : list.append(drag);
  });
  const end = () => {
    if (!drag) return;
    drag.classList.remove('dragging'); drag = null;
    onorder([...list.children].map(c => c.dataset.id).filter(Boolean));
  };
  list.addEventListener('pointerup', end);
  list.addEventListener('pointercancel', end);
  return list;
}

/* 态度编辑：预设多选 + 自填。 */
function attitudeEditor(att, onchange) {
  att = { presets: [...((att && att.presets) || [])], custom: (att && att.custom) || '' };
  const custom = h('textarea', { rows: 2, placeholder: '自己写，比如：叫他老大，但别太当真', value: att.custom, style: { minHeight: '56px' },
    oninput: debounce(() => { att.custom = custom.value; onchange(att); }, 400) });
  return h('div.col.gap4',
    chips(Object.keys(B().presets).map(k => [k, k]), att.presets, v => { att.presets = v; onchange(att); }),
    custom,
    h('div.field-help', att.presets.length ? att.presets.map(p => `${p}：${B().presets[p]}`).join('；') : '不选也不写 = 沿用上一层'));
}
function attText(a) {
  if (!a || (!(a.presets || []).length && !(a.custom || '').trim())) return '';
  return [...(a.presets || []), (a.custom || '').trim()].filter(Boolean).join('、');
}

/* ---------- 分层设置表单 ---------- */
const CAT_ORDER = ['回复', '插话', '多机器人', '上下文', '模型', '学习', '工具', '文件', '记忆', '节奏', '过滤', '权限', '平台', '系统'];
function personaOptions() { return [['', '（沿用角色的人设）'], ...B().personas.map(p => [p.id, p.name])]; }
function endpointOptions() { return B().endpoints.map(e => [e.id, e.name]); }

/* 这一层实际生效的服务商里有没有这个模型：不在就提示一句（不拦保存、不改设置）。 */
function modelMismatchHint(model, eid) {
  const box = h('div.field-help', { style: { display: 'none', color: 'var(--warn)' } });
  const ep = (B().endpoints || []).find(e => e.id === (eid || 'default'));  // 没选服务商 = 用默认那个
  if (!ep || !model) return box;
  endpointModels(ep.id).then(r => {
    const models = (r && r.models) || [];
    if (!models.length || models.some(m => m.id === model)) return;  // 自定义服务商、问不到又没有内置目录：不提示
    box.textContent = r.live
      ? tr(`「${ep.name}」现在提供的模型里没有「${model}」，这样调用会出错。点「选择…」换一个。`)
      : tr(`内置目录里「${ep.name}」下没有「${model}」。如果是刚出的新模型可以不管；不是的话，点「选择…」换一个。`);
    box.style.display = '';
  }).catch(() => {});
  return box;
}

function settingControl(s, value, save) {
  switch (s.type) {
    case 'bool': return toggle(value, save);
    case 'select': return select(s.options, value, v => save(typeof s.default === 'number' ? Number(v) : v));
    case 'int': case 'float': {
      const inp = h('input', { type: 'number', value, step: s.type === 'int' ? 1 : 0.05, style: { width: '120px' },
        onchange: () => { if (inp.value !== '') save(s.type === 'int' ? parseInt(inp.value) : parseFloat(inp.value)); } });
      if (s.options && s.type === 'float') {  // 有常用值的小数：点一下选常用的，也可以自己填
        return h('div.row.wrap', s.options.map(([v, t]) => h('span.chip', { class: Number(value) === v ? 'on' : '', onclick: () => save(v) }, t)), inp);
      }
      return inp;
    }
    case 'list': return tagsInput(value, save);
    case 'text': {
      const t = h('textarea.code', { rows: 6, value: value || '', placeholder: '留空使用内置', onchange: () => save(t.value) });
      return t;
    }
    case 'endpoint': return select([['', '（不设）'], ...endpointOptions()], value, save);
    case 'model': return modelControl(value, save);
    case 'multi': return chips(s.options, value || [], save);
    case 'persona': return select(personaOptions(), value, save);
    case 'perms': return permsEditor(value || {}, save);
    default: {
      const inp = h('input', { value: value ?? '', onchange: () => save(inp.value) });
      return inp;
    }
  }
}

function permsEditor(perms, save) {
  perms = JSON.parse(JSON.stringify(perms || {}));
  const wrap = h('div.col.gap4', { style: { width: '100%' } });
  const modes = [['owner', '只有我'], ['people', '我 + 指定的人'], ['pgroup', '我 + 某分组'], ['admins', '我 + 群主和群管理员']];
  const render = () => clear(wrap, Object.entries(B().commands).map(([cmd, label]) => {
    const c = perms[cmd] || { mode: 'owner' };
    const extra = c.mode === 'people'
      ? select([['', '选人…'], ...B().people.map(p => [p.id, pLabel(p)])], '', v => { if (v) { c.people = [...new Set([...(c.people || []), v])]; perms[cmd] = c; save(perms); render(); } })
      : c.mode === 'pgroup' ? select([['', '选分组…'], ...B().pgroups.map(g => [g.id, g.name])], c.pgroup || '', v => { c.pgroup = v; perms[cmd] = c; save(perms); }) : null;
    return h('div.row.wrap', { style: { padding: '4px 0' } }, h('span', { style: { width: '120px' } }, label),
      select(modes, c.mode, v => { c.mode = v; perms[cmd] = c; save(perms); render(); }), extra,
      c.mode === 'people' && (c.people || []).map(pid => h('span.chip', (byId(B().people, pid) ? pLabel(byId(B().people, pid)) : pid),
        h('span.x', { onclick: () => { c.people = c.people.filter(x => x !== pid); save(perms); render(); } }, '×'))));
  }), h('div.field-help', '「我」= 人物表里勾了「管理员」的人。「忘掉我」除管理员外所有人都能用（管理员的人物卡和记忆只能在面板里改）。'),
  h('details', h('summary.small', { style: { cursor: 'pointer', fontWeight: 600 } }, '指令总表（全部指令）'),
    h('div.field-help', '群里 @她 说「指令」或「帮助」、或者私聊她说，她会回一份你能用的指令清单。'),
    h('div.col.gap4.mt8', (B().command_table || []).map(r => h('div.small', h('b', r.say), '：', r.use, h('span.faint', `（${r.where}；${r.who}）`))))));
  render();
  return wrap;
}

/**
 * 某个作用域的设置表单。opts: cats（只显示这些分类）、exclude（不显示的分类）、keys（只显示这些项）。
 * 每项显示是继承来的还是在这里改过的；改过的可以一键恢复继承。
 */
async function settingsForm(scope, opts = {}) {
  const wrap = h('div.settings-form');
  const content = h('div');
  const st = { q: '', open: {} };  // q：搜索词；open：各分类展开与否（重画时记着）
  const search = opts.keys ? null : h('input.set-search', { type: 'search', placeholder: '搜索设置（按名字或说明）', 'aria-label': '搜索设置',
    oninput: debounce(() => { st.q = search.value.trim().toLowerCase(); render(); }, 150) });
  add(wrap, search, content);
  const schema = Object.fromEntries(B().schema.map(s => [s.key, s]));
  let items;
  const load = async () => { items = (await GET('/api/settings?scope=' + encodeURIComponent(scope))).items; render(); };
  const save = async (key, value, reset = false) => {
    const r = await PUT('/api/settings', { scope, key, value, reset });
    items = r.items; B().settings[scope] = B().settings[scope] || {};
    if (reset) delete B().settings[scope][key]; else B().settings[scope][key] = value;
    toast(reset ? '已恢复继承' : '已保存');
    if (key === 'theme') { S.theme = value; applyTheme(value); }
    if (key === 'ui_lang') { try { localStorage.setItem('mb_lang', value); } catch (e) { /* 没关系 */ } setTimeout(() => location.reload(), 400); }
    render();
  };
  const row = it => {
    const s = schema[it.key];
    const wide = ['list', 'text', 'perms'].includes(s.type);
    const hint = s.type === 'model' ? modelMismatchHint(it.value, (items.find(x => x.key === it.key.replace(/_model$/, '_endpoint')) || {}).value) : null;
    const ctl = settingControl(it.options ? { ...s, options: it.options } : s, it.value, v => save(it.key, v));
    return h('div.set-row', { class: wide ? 'wide' : '' },
      h('div', h('div.lbl', s.label, it.own ? h('span.own', '已改') : h('span.inh', '继承：' + scopeLabel(it.from)),
        it.own && scope !== 'global' ? h('button.ghost.sm', { title: '恢复继承上一层', onclick: () => save(it.key, null, true) }, icon('undo'), '恢复继承') : null,
        it.own && scope === 'global' ? h('button.ghost.sm', { title: '恢复内置默认', onclick: () => save(it.key, null, true) }, icon('undo'), '恢复默认') : null),
        s.help && h('div.field-help', s.help)),
      h('div.ctl', hint ? { style: { flexDirection: 'column', alignItems: 'stretch', gap: '4px' } } : null, ctl, hint));
  };
  const render = () => {
    let list = items.filter(it => {
      const s = schema[it.key];
      if (opts.hide && scope === opts.hideAt && opts.hide.includes(it.key)) return false;
      if (s.when && !Object.entries(s.when).every(([k, v]) => (items.find(x => x.key === k) || {}).value === v)) return false;  // 选了别的项才显示的（比如按比例才显示比例）
      if (opts.keys) return opts.keys.includes(it.key);
      if (opts.cats && !opts.cats.includes(s.cat)) return false;
      if (opts.exclude && opts.exclude.includes(s.cat)) return false;
      return true;
    });
    if (opts.keys) list.sort((a, b) => opts.keys.indexOf(a.key) - opts.keys.indexOf(b.key));
    if (st.q) {  // 搜索：按名字、说明、键名找，高级里的也一起列出来，不分折叠
      const hit = list.filter(it => { const s = schema[it.key]; return `${s.label} ${s.help || ''} ${s.key}`.toLowerCase().includes(st.q); });
      return clear(content, hit.length ? [h('div.small.faint.mb8', `找到 ${hit.length} 项`), h('div.set-cat', hit.map(row))] : h('div.empty', '没有找到匹配的设置项'));
    }
    const top = (opts.top || []).map(k => list.find(it => it.key === k)).filter(Boolean);
    list = list.filter(it => !top.includes(it));
    const cats = [...new Set(list.map(it => schema[it.key].cat))].sort((a, b) => CAT_ORDER.indexOf(a) - CAT_ORDER.indexOf(b));
    // 分类可折叠：电脑上默认展开，手机上默认折叠（「常用」除外）；展开状态重画时保留
    const narrow = () => window.matchMedia('(max-width: 760px)').matches;
    const fold = (key, title, rows, dflt) => {
      if (!title) return h('div.set-cat', rows.map(row));
      const d = h('details.set-cat', { open: key in st.open ? st.open[key] : dflt }, h('summary', icon('chev'), h('h4', title), h('span.faint.small', `${rows.length} 项`)), rows.map(row));
      d.addEventListener('toggle', () => { st.open[key] = d.open; });
      return d;
    };
    const common = top.length ? [fold('top', '常用', top, true)] : [], adv = [];
    for (const c of cats) {
      const its = list.filter(it => schema[it.key].cat === c);
      const cm = its.filter(it => !schema[it.key].adv), ad = its.filter(it => schema[it.key].adv);
      if (cm.length) common.push(fold('c:' + c, cats.length > 1 && !opts.flat ? c : '', cm, !narrow()));
      if (ad.length) adv.push(fold('a:' + c, cats.length > 1 ? c : '', ad, !narrow()));
    }
    const open = content.querySelector('details.adv')?.open;
    clear(content, common, adv.length ? h('details.adv', { open }, h('summary', icon('chev'), `高级设置（${adv.reduce((n, d) => n + d.querySelectorAll('.set-row').length, 0)} 项）`), adv) : null,
      !list.length ? h('div.empty', '这一层没有可设的项') : null);
  };
  await load();
  return wrap;
}

/* 作用域选择 + 设置表单：角色 / 角色在平台 / 角色在群。 */
function scopedSettings(baseScope, choices, opts = {}) {
  const box = h('div');
  const body = h('div');
  let cur = choices[0][0];
  const show = async () => clear(body, await settingsForm(cur, opts));
  const sel = select(choices, cur, v => { cur = v; show(); });
  add(box, h('div.row.wrap.mb8', h('span.muted', '设置范围：'), h('div', { style: { minWidth: '220px' } }, sel),
    h('span.faint.small', '没单独改的项沿用上一层')), body);
  show();
  return box;
}
