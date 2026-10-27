/* 服务商、模型选择、角色模板、更多平台（官方接口与非官方渠道）、体验模式。 */
'use strict';

const PLAT_PICK = Object.entries(PLAT).filter(([k]) => k !== 'demo');

// ---------- 模型选择 ----------
const MODEL_CACHE = {};
async function endpointModels(eid, live = false) {
  if (!live && MODEL_CACHE[eid]) return MODEL_CACHE[eid];
  const r = await GET(`/api/endpoints/${encodeURIComponent(eid)}/models`);
  return (MODEL_CACHE[eid] = r);
}
function modelPicker(onpick, eid = '') {
  const eps = B().endpoints;
  if (!eps.length) { toast('先到 设置 → 模型与接口 添加一个服务商', 'err'); return; }
  let cur = eid || eps[0].id, q = '', data = null;
  const list = h('div.list', { style: { maxHeight: '50vh', overflow: 'auto' } });
  const note = h('div.small.faint');
  const render = () => {
    const items = (data ? data.models : []).filter(m => !q || (m.id + (m.name || '')).toLowerCase().includes(q.toLowerCase()));
    clear(list, items.map(m => h('div.item.click', { onclick: () => { close(); onpick(m.id); } },
      h('div.grow', h('b', m.id), m.name && m.name !== m.id ? h('div.small.muted', m.name) : null),
      m.vision ? h('span.badge.blue', '能看图') : null, m.reasoning ? h('span.badge', '可思考') : null)), !items.length && h('div.empty.small', '没有匹配的模型'));
    note.textContent = data ? (data.live ? '这是服务商现在提供的模型列表。' : '没能向服务商拉取列表' + (data.error ? '（' + data.error + '）' : '') + '，这是内置目录里的清单，也可以直接手填模型名。') : '加载中…';
  };
  const load = async live => { data = null; render(); data = await endpointModels(cur, live); render(); };
  const close = modal('选择模型', h('div.col.gap12',
    h('div.row.wrap', select(eps.map(e => [e.id, e.name]), cur, v => { cur = v; load(true); }), h('input', { placeholder: '搜索', oninput: e => { q = e.target.value; render(); } }),
      h('button.sm', { onclick: () => load(true) }, icon('refresh'), '向服务商拉取最新')), note, list), { wide: true });
  load(true);
}
function modelControl(value, save) {
  const inp = h('input', { value: value ?? '', style: { flex: 1, minWidth: '140px' }, onchange: () => save(inp.value.trim()) });
  return h('div.row', { style: { width: '100%', maxWidth: '420px' } }, inp,
    h('button.sm', { onclick: () => modelPicker(v => { inp.value = v; save(v); }) }, '选择…'));
}

// ---------- 服务商目录 ----------
const CN_NAME = { deepseek: 'DeepSeek（深度求索）', zhipuai: '智谱 AI（开放平台）', 'zhipuai-coding-plan': '智谱 Coding 套餐', zai: 'Z.ai（智谱国际）',
  'zai-coding-plan': 'Z.ai Coding 套餐', 'alibaba-cn': '阿里云百炼（国内）', alibaba: '阿里云百炼（国际）', 'moonshotai-cn': '月之暗面 Kimi（国内）',
  moonshotai: '月之暗面 Kimi（国际）', 'siliconflow-cn': '硅基流动（国内）', siliconflow: '硅基流动（国际）', volcengine: '火山引擎方舟',
  'minimax-cn': 'MiniMax（国内）', minimax: 'MiniMax（国际）', stepfun: '阶跃星辰（国内）', 'kimi-code-plan-cn': 'Kimi Code 套餐', 'tencent-tokenhub': '腾讯 TokenHub',
  modelscope: '魔搭 ModelScope', xiaomi: '小米 MiMo', 'baidu-qianfan': '百度千帆', 'iflytek-spark': '讯飞星火', 'tencent-hunyuan': '腾讯混元', 'ollama-local': 'Ollama（本机）' };
const provName = p => CN_NAME[p.id] || p.name;

function keyStateText(e) {
  if (e.unreadable) return h('span.badge.warn', '换了电脑或 Windows 用户，密钥读不出来，请重新填');
  if (e.keyless) return h('span.badge', '本机服务，不需要密钥');
  if (e.has_key) return h('span.badge.ok', e.source === '本机' ? '已填密钥' : `密钥来自${e.source}`);
  return h('span.badge.warn', e.key_env ? `没有密钥（也没读到环境变量 ${e.key_env}）` : '没填密钥');
}
function statusDot(e) {
  const st = e.status || {};
  return h('span.dot', { class: st.ok === true ? 'online' : st.ok === false ? 'error' : '',
    title: st.ok === true ? `上次测试连通（${st.n} 个模型）` : st.ok === false ? '上次测试没连上：' + (st.msg || '') : '还没测试' });
}

function editEndpoint(e0, { catalog, onsaved }) {
  const isNew = !e0.id;
  const e = { protocol: 'openai', use_proxy: false, dialect: 'auto', ...e0 };
  const prov = catalog.find(p => p.id === e.provider) || null;
  const key = h('input', { type: 'password', autocomplete: 'off', placeholder: e0.has_key ? '已保存（不显示）。要换就重新粘贴' : '粘贴 API Key' });
  const fromEnv = h('input', { type: 'checkbox', checked: !!e.key_env && !e0.has_key });
  const envName = h('input', { value: e.key_env || '', placeholder: '环境变量名，如 DEEPSEEK_API_KEY', oninput: ev => e.key_env = ev.target.value.trim() });
  const res = h('span.small');
  const body = h('div.form',
    h('div.form2', field('名字', h('input', { value: e.name || '', oninput: ev => e.name = ev.target.value })),
      field('协议', select(PROTOCOL_OPTIONS, e.protocol, v => e.protocol = v), prov ? '服务商目录里已经定好了' : '')),
    field('接口地址', h('input', { value: e.base_url || '', placeholder: 'https://…', oninput: ev => e.base_url = ev.target.value.trim().replace(/\/+$/, '') }),
      e.protocol === 'openai' ? '末尾不要写 /chat/completions' : null),
    field('API Key', key, '只存在这台电脑，用 Windows 账户加密保存；不进日志、不进安装包，导出数据包时默认不带'),
    h('label.row', fromEnv, h('span.small', '不在这里存密钥，改从环境变量读')),
    field('环境变量名', envName, '填了环境变量名、又没存密钥时，程序启动时会去读这个变量'),
    h('div.form2', field('使用代理', toggle(e.use_proxy, v => e.use_proxy = v), '代理地址在下面「模型」里设；海外服务商（OpenAI、Anthropic、Gemini……）国内一般要开'),
      field('思考强度写法', select(B().providerMeta.dialects, e.dialect || 'auto', v => e.dialect = v), '不认识的服务商默认不发思考参数；这里可以改')),
    field('模型能读多长（tokens，可不填）', h('input', { type: 'number', min: 0, value: e.context_tokens || '', placeholder: '不填：自动判断（目录里报的、报错里写明的、被拒后记下的，都没有按 1M）',
      oninput: ev => e.context_tokens = parseInt(ev.target.value, 10) || 0 }), '用来决定每次最多带多少记忆进提示词。填了就按你填的；没填，程序取目录、报错和被拒后记下的上限里最小的；成功发出过的最大请求作为底'),
    h('details', h('summary.small.muted', { style: { cursor: 'pointer' } }, '额外请求参数（高级）'),
      field('JSON，会合并进每次请求', h('textarea.code', { rows: 3, value: e.extra || '', placeholder: '{"top_p": 0.9}', oninput: ev => e.extra = ev.target.value }))),
    prov && prov.doc ? h('div.small', h('a', { href: prov.doc, target: '_blank' }, '这家服务商的文档'), prov.manual ? '（接口地址是手工整理的，请点「测试连接」核对）' : '') : null);
  const persist = async () => {
    if (!e.name || !e.base_url) { toast('名字和接口地址都要填', 'err'); return null; }
    if (fromEnv.checked) { if (!e.key_env) { toast('请填环境变量名', 'err'); return null; } }
    const saved = await PUT('/api/docs/endpoint', { ...e, has_key: undefined, keyless: undefined, unreadable: undefined, env_set: undefined, source: undefined, status: undefined, doc: undefined });
    if (key.value.trim() && !fromEnv.checked) await PUT('/api/secret', { k: 'endpoint:' + saved.id, v: key.value.trim() });
    await reloadBoot();
    return saved;
  };
  const close = modal(isNew ? '添加服务商' : '编辑服务商', body, { wide: true, foot: c => [
    res, h('div.grow'), h('button', { onclick: c }, '取消'),
    h('button', { onclick: async () => {
      const saved = await persist(); if (!saved) return;
      res.textContent = '测试中…'; res.className = 'small';
      try { const r = await POST(`/api/endpoints/${saved.id}/test`); res.textContent = `✓ 连通，${r.models.length} 个模型`; res.className = 'small ok'; }
      catch (err) { res.textContent = '✗ ' + err.message; res.className = 'small bad'; }
      await reloadBoot(); onsaved && onsaved();
    } }, '保存并测试连接'),
    h('button.primary', { onclick: async () => { const saved = await persist(); if (!saved) return; c(); toast('已保存'); onsaved && onsaved(); } }, '保存')] });
  return close;
}
const PROTOCOL_OPTIONS = [['openai', 'OpenAI 兼容'], ['anthropic', 'Anthropic'], ['gemini', 'Gemini']];

const fmtDay = ts => { const d = new Date(ts * 1000); const p = n => String(n).padStart(2, '0'); return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`; };

// 这家服务商下的新模型提醒：待替换的提议、这家新出的模型、暂不换和一直不换的
function watchBox(e, w, reload) {
  const d = (w.eps || {})[e.id];
  if (!d || !(d.pending.length || d.fresh.length || d.snoozed.length || d.never.length)) return null;
  const act = (action, key) => async ev => {
    const b = ev.currentTarget; b.disabled = true;
    try { const r = await POST('/api/models/watch/' + action, { key }); toast(r.text, r.ok ? '' : 'err'); } catch (err) { /* api() 已提示 */ }
    await reload();
  };
  return h('div.col.gap8.mt8',
    d.pending.map(p => h('div.notice.ok', icon('star'), h('div.grow', h('div', `新出了 ${p.new}，你在用 ${p.old}`), p.diff ? h('div.small', p.diff) : null,
      h('div.ep-btns.mt8', h('button.sm.primary', { onclick: act('replace', p.key) }, '一键替换'),
        p.partial ? h('button.sm', { onclick: act('replace_able', p.key) }, '只换能换的') : null,
        p.vision ? h('button.sm', { onclick: act('replace_vision', p.key) }, '看图也换') : null,
        h('button.sm', { onclick: act('snooze', p.key) }, '暂不换'),
        h('button.sm.ghost', { onclick: act('never', p.key) }, '一直不换'))))),
    d.fresh.length ? h('div.small', h('b', '这家新出的模型'), '：', d.fresh.map(f => `${f.id}（${fmtDay(f.ts)}）`).join('、')) : null,
    d.snoozed.concat(d.never).length ? h('div.col.gap4', d.snoozed.map(p => h('div.small.row', `${p.new}：暂不换（到 ${fmtDay(p.until)}）`, h('button.sm.ghost', { onclick: act('revoke', p.key) }, '撤销'))),
      d.never.map(p => h('div.small.row', `${p.new}：一直不换`, h('button.sm.ghost', { onclick: act('revoke', p.key) }, '撤销')))) : null);
}

async function providersUI(box, { compact = false } = {}) {
  if (!B().providerMeta) B().providerMeta = await GET('/api/providers');
  const meta = B().providerMeta;
  let tab = 'third', watch = { eps: {} };
  const cards = h('div.col.gap8');
  const adder = h('div.card');
  const loadWatch = async () => { try { watch = await GET('/api/models/watch'); } catch (err) { /* 没拿到就不显示 */ } renderCards(); };
  const renderCards = () => {
    const eps = B().endpoints;
    clear(cards, eps.map(e => {
      const p = meta.providers.find(x => x.id === e.provider);
      return h('div.card.ep-card', h('div.ep-head', statusDot(e), h('b', e.name), keyStateText(e), e.use_proxy ? h('span.badge', '走代理') : null),
        h('div.small.muted', { style: { wordBreak: 'break-all' } }, `${p ? provName(p) : '自定义 API'} · ${(PROTOCOL_OPTIONS.find(x => x[0] === e.protocol) || [])[1] || e.protocol} · ${e.base_url}`),
        h('div.ep-btns', h('button.sm', { onclick: async ev => {
          const b = ev.currentTarget; b.disabled = true;
          try { const r = await POST(`/api/endpoints/${e.id}/test`); toast(`连通，${r.models.length} 个模型`); } catch (err) { /* api() 已提示 */ }
          await reloadBoot(); b.disabled = false; renderCards();
        } }, '测试'),
        h('button.sm', { title: '真调一次：让模型读一张写着数字的小图，看它能不能看图（几百 token）', onclick: async ev => {
          const model = await promptBox('测试看图', '要测的模型名', (B().settings.global || {}).vis_model || 'deepseek-flash');
          if (!model) return;
          const b = ev.currentTarget; b.disabled = true;
          try { const r = await POST(`/api/endpoints/${e.id}/vision_test`, { model: model.trim() }); toast(r.ok ? `✓ ${r.model} 能看图（读出了 ${r.expected}）` : `✗ ${r.model} 没读对：应该是 ${r.expected}，它回答「${r.answer}」`, r.ok ? '' : 'err'); }
          catch (err) { /* api() 已提示 */ }
          b.disabled = false;
        } }, icon('eye'), '测试看图'),
        h('button.sm', { onclick: () => editEndpoint(e, { catalog: meta.providers, onsaved: renderCards }) }, icon('edit'), '编辑'),
        h('button.sm.ghost.danger', { title: '删除', onclick: async () => {
          if (!(await confirmBox(`删除「${e.name}」？用它的模型设置会改用主服务商。`, '删除', true))) return;
          await DEL('/api/docs/endpoint/' + e.id); await reloadBoot(); renderCards();
        } }, icon('trash'))),
        watchBox(e, watch, loadWatch));
    }), !eps.length && h('div.notice.warn', icon('alert'), '还没有配置任何服务商，机器人说不了话。在下面选一家、填上密钥。'));
  };
  const renderAdder = () => {
    clear(adder, h('div.row.wrap.mb8', seg([['third', '第三方服务商'], ['custom', '自定义 API']], tab, x => { tab = x; renderAdder(); }),
      tab === 'third' ? h('button.sm.ghost', { onclick: async ev => {
        const b = ev.currentTarget; b.disabled = true;
        try { const r = await POST('/api/providers/refresh'); toast(r.updated ? `目录已更新：${r.providers} 家、${r.models} 个模型` : '目录已经是最新'); B().providerMeta = await GET('/api/providers'); renderAdder(); }
        catch (err) { /* 已提示 */ } b.disabled = false;
      } }, icon('refresh'), '联网更新服务商目录') : null),
      tab === 'third' ? thirdParty() : h('div.col.gap8', h('div.small.muted', '任何 OpenAI 兼容、Anthropic 或 Gemini 协议的接口都能加：自己的中转站、本机的 Ollama / LM Studio、公司内网的网关……'),
        h('div', h('button.primary', { onclick: () => editEndpoint({ name: '', base_url: '' }, { catalog: meta.providers, onsaved: renderCards }) }, icon('plus'), '添加自定义 API'))));
  };
  const thirdParty = () => {
    let q = '';
    const sel = h('select', { size: compact ? 6 : 8, style: { width: '100%' } });
    const fill = () => {
      clear(sel, meta.providers.filter(p => !q || (provName(p) + p.id).toLowerCase().includes(q.toLowerCase())).map(p =>
        h('option', { value: p.id }, `${provName(p)}${p.manual ? '（地址手工整理）' : ''}${p.local ? '（本机）' : ''}${p.proxy ? ' · 国内需代理' : ''}`)));
    };
    fill();
    return h('div.col.gap8', h('div.small.muted', `内置 ${meta.providers.length} 家（数据来自 models.dev，MIT 许可）。选一家，填上密钥就能用。`),
      h('input', { placeholder: '搜索，比如 deepseek、千问、kimi、openai', oninput: ev => { q = ev.target.value; fill(); } }), sel,
      h('div', h('button.primary', { onclick: () => {
        const p = meta.providers.find(x => x.id === sel.value);
        if (!p) return toast('先在上面选一家', 'err');
        editEndpoint({ name: provName(p), provider: p.id, protocol: p.protocol, base_url: p.api, key_env: p.env[0] || '', use_proxy: !!p.proxy }, { catalog: meta.providers, onsaved: renderCards });
      } }, icon('plus'), '用选中的这家')));
  };
  const checkBtn = h('button.sm', { title: '立刻问一遍你配过的各家服务商现在有哪些模型，和上次比（不花 token）', onclick: async ev => {
    const b = ev.currentTarget; b.disabled = true;
    try { const r = await POST('/api/models/watch/check'); toast(r.text); } catch (err) { /* 已提示 */ }
    b.disabled = false; await loadWatch();
  } }, icon('refresh'), '现在检查新模型');
  renderCards(); renderAdder(); loadWatch();
  add(box, h('div.row.wrap.mb8', checkBtn, h('span.small.faint', '定时检查的间隔在 设置 → 系统 里；你在用的模型出了同系列的新版本时提醒你，不会自动换。')), cards, adder);
}

// ---------- 角色模板库 ----------
// 卡片上的「本地」徽标和三个按钮：复制到本地模板 / 编辑 / 恢复成自带的。refresh 重画模板库
function tplLocal(t, refresh) {
  const copy = async () => {
    const r = await POST(`/api/templates/${t.id}/copy_local`);
    toast(`已复制到本地模板：${r.path}。以后升级不会覆盖它；改它不会改到已经用这个模板建好的人设。`);
    refresh();
  };
  const edit = async () => {
    const d = await GET(`/api/templates/${t.id}/local`);
    const ta = h('textarea.code', { rows: 22, value: d.text });
    modal(`编辑本地模板：${d.file}`, h('div.col.gap8', h('div.small.muted', '这是整个文件的全文，格式和 assets/templates 里的一样；不能改这个模板的 id。'), ta), {
      wide: true,
      foot: c => [h('button', { onclick: () => c() }, '取消'),
        h('button.primary', { onclick: async () => { await PUT(`/api/templates/${t.id}/local`, { text: ta.value }); c(); toast('已保存'); refresh(); } }, '保存')],
    });
  };
  const restore = async () => {
    if (!(await confirmBox(`恢复成自带的「${t.name}」？会删掉本地的 local-${t.id}.md，你改的内容会丢失；已经建好的人设不会变。`, '恢复', true))) return;
    await POST(`/api/templates/${t.id}/restore`);
    toast('已恢复成自带的'); refresh();
  };
  return {
    badge: t.local ? h('span.badge', '本地') : null,
    buttons: [t.local ? null : h('button.sm.ghost', { onclick: copy }, icon('plus'), '复制到本地模板'),
      t.local ? h('button.sm.ghost', { onclick: edit }, icon('edit'), '编辑') : null,
      t.restorable ? h('button.sm.ghost', { onclick: restore }, icon('refresh'), '恢复成自带的') : null],
  };
}

async function templatePicker(onpick) {
  const box = h('div.col.gap16');
  const render = async () => {
    const lst = await GET('/api/templates');
    clear(box, lst.map(cat => h('div', h('h4.mb8', cat.category),
      h('div.grid', cat.items.map(t => { const lc = tplLocal(t, render); return h('div.card.col', { style: { gap: '8px' } },
        h('div.row', h('b.grow', t.name), lc.badge, t.disclaimer ? h('span.badge', { title: '以科普和给方向为主，不下诊断，大事找执业人士' }, '科普') : null),
        h('div.small.muted', t.blurb),
        h('div.row.wrap', h('button.sm.primary', { onclick: async () => {
          const name = await promptBox('用「' + t.name + '」新建角色', '角色名字', t.name);
          if (!name) return;
          const role = await POST(`/api/templates/${t.id}/use`, { name: name.trim() });
          await reloadBoot(); close(); toast('已新建角色「' + role.name + '」'); onpick && onpick(role);
        } }, icon('plus'), '用这个'), lc.buttons)); })))));
  };
  await render();
  const close = modal('角色模板库', h('div.col.gap12', h('div.small.muted', '选一个模板就新建一个角色，之后随便改。模板只是起点：人设只在第一次从这个模板建角色时复制一份，之后再从同一个模板建角色，直接共用那份（改它会连带改到用它的所有角色，想各改各的先在「人设」页复制一份）。'), box), { wide: true });
}

// 角色页下半部分常驻的模板库：按类别分组、按类别筛选、按名字搜索，卡片能展开看人设全文
function templateLibrary(onpick) {
  const root = h('div.card.col', { id: 'tpl-lib', style: { gap: '12px', scrollMarginTop: '70px' } });
  let data = [], cat = 'all', q = '';
  const body = h('div.col.gap16');
  const bar = h('div.col.gap8');
  const use = async t => {
    const name = await promptBox('用「' + t.name + '」新建角色', '角色名字', t.name);
    if (!name) return;
    const role = await POST(`/api/templates/${t.id}/use`, { name: name.trim() });
    await reloadBoot(); toast('已新建角色「' + role.name + '」'); onpick && onpick(role);
  };
  const reload = () => GET('/api/templates').then(d => { data = d; draw(); });
  const card = t => {
    const pv = h('div');
    let open = false;
    const btn = h('button.sm.ghost', { onclick: async () => {
      open = !open;
      btn.replaceChildren(icon(open ? 'chev' : 'chev'), open ? '收起预览' : '展开预览');
      if (!open) return clear(pv);
      clear(pv, h('div.small.muted', '加载中…'));
      try {
        const d = await GET('/api/templates/' + t.id);
        clear(pv, h('pre.preview', { style: { maxHeight: '320px', overflow: 'auto' } },
          [d.shell, ...d.sections.map(s => `## ${s.title}\n${s.body}`)].filter(Boolean).join('\n\n')));
      } catch (e) { clear(pv, h('div.small.muted', '没读到')); }
    } }, icon('chev'), '展开预览');
    const lc = tplLocal(t, reload);
    return h('div.card.col', { style: { gap: '8px' } },
      h('div.row', h('b.grow', t.name), lc.badge, t.disclaimer ? h('span.badge', { title: '以科普和给方向为主，不下诊断，大事找执业人士' }, '科普') : null),
      h('div.small.muted', t.blurb),
      h('div.row.wrap', h('button.sm.primary', { onclick: () => use(t) }, icon('plus'), '用这个'), btn, lc.buttons), pv);
  };
  const draw = () => {
    const total = data.reduce((n, c) => n + c.items.length, 0);
    clear(bar, h('div.row.wrap', seg([['all', '全部 ' + total], ...data.map(c => [c.category, c.category + ' ' + c.items.length])], cat, v => { cat = v; draw(); })),
      h('input', { type: 'search', placeholder: '按名字搜索模板', value: q, style: { maxWidth: '360px' }, oninput: e => { q = e.target.value.trim(); drawBody(); } }));
    drawBody();
  };
  const drawBody = () => {
    const shown = data.filter(c => cat === 'all' || c.category === cat)
      .map(c => ({ category: c.category, items: c.items.filter(t => !q || t.name.includes(q) || t.blurb.includes(q)) })).filter(c => c.items.length);
    clear(body, shown.map(c => h('div', h('h4.mb8', c.category), h('div.grid', c.items.map(card)))),
      !shown.length && h('div.empty', '没有符合的模板'));
  };
  add(root, h('div.row', h('h2.grow', '角色模板库'), h('span.small.muted', '选一个就复制成新角色')),
    h('div.small.muted', '模板只是起点：点「用这个」会新建一个角色并打开编辑页，之后随便改。人设只在第一次从这个模板建角色时复制一份，之后再从同一个模板建角色，直接共用那份（改它会连带改到用它的所有角色，想各改各的先在「人设」页复制一份）。想放自己的模板，在数据目录里建一个 templates 文件夹，照 assets/templates 里的格式写 .md 文件；同 id 的以你的为准。'), bar, body);
  reload();
  return root;
}

// ---------- 更多平台（官方接口 / 非官方渠道）----------
async function platformSpecs() {
  if (!S.specs) S.specs = await GET('/api/platform_specs');
  return S.specs;
}
async function moreSections() {
  const specs = await platformSpecs();
  const groups = [['official', '官方接口的平台', '用平台官方开放的机器人接口，稳定，但各有各的开通门槛。'],
    ['unofficial', '非官方渠道（有风险，默认关闭）', '平台没有开放这些接口，是用网页登录态模拟的。可能违反平台用户协议，账号有被限制或封禁的风险；请用小号、先开影子模式演练。'],
    ['info', '没有做的平台', '']];
  return groups.map(([tier, title, desc]) => {
    const items = specs.filter(s => s.tier === tier);
    return h('details.card.mt16', { open: tier === 'official' && B().accounts.some(a => specs.some(s => s.id === a.platform && s.tier === tier)) },
      h('summary', { style: { cursor: 'pointer', fontWeight: 650 } }, title, h('span.small.faint', `（${items.length} 个）`)),
      desc && h('div.small.muted.mt8', desc),
      h('div.col.gap12.mt8', items.map(s => platformCard(s))));
  });
}
function platformCard(s) {
  const b = B();
  const accs = b.accounts.filter(a => a.platform === s.id);
  const on_ = !!b.platforms[s.id];
  return h('div.card.col', { style: { gap: '8px', background: 'var(--bg-soft, transparent)' } },
    h('div.card-h', plat(s.id), h('h3', s.name), s.tier === 'unofficial' ? h('span.badge.bad', '有风险') : null, s.tier === 'unofficial' ? h('span.badge.warn', '预览版') : null, s.experimental ? h('span.badge.warn', '实验性') : null,
      s.tier === 'info' ? null : h('span.small.muted', on_ ? '已开启' : '未开启'),
      s.tier === 'info' ? null : toggle(on_, async val => { await PUT('/api/platforms', { [s.id]: val }); b.platforms[s.id] = val; App.route(); }, '开关这个平台'),
      s.tier === 'info' ? null : h('button.sm.ghost', { onclick: () => platformSettings(s.id) }, icon('gear'), '平台设置'),
      s.tier === 'info' ? h('button.sm', { onclick: () => platformGuide(s) }, icon('info'), '为什么没做') : h('button.sm', { onclick: () => editGeneric(s, {}) }, icon('plus'), '添加')),
    h('div.small.muted', s.summary), s.risk ? h('div.notice.bad', icon('alert'), h('div.small', s.risk)) : null,
    accs.length ? h('div.grid', accs.map(a => accountCard(a))) : null);
}
function platformGuide(s) {
  modal(s.name, h('div.col.gap8', h('div.small.muted', s.summary), h('ol.guide', s.steps.map(t => h('li', h('div', t))))), { wide: true });
}

function editGeneric(spec, a) {
  const isNew = !a.id;
  const d = JSON.parse(JSON.stringify(a));
  d.platform = spec.id;
  if (isNew) { d.role_id = (B().roles[0] || {}).id; spec.flags.forEach(([k, , def]) => { if (d[k] === undefined) d[k] = def; }); }
  const secretInputs = {};
  const roleSel = select([['', '（不绑定）'], ...B().roles.map(r => [r.id, r.name])], d.role_id || '', v => d.role_id = v);
  const fields = spec.fields.map(f => {
    if (f.secret) {
      const st = (a.secrets_set || {})[f.key];
      const inp = h('input', { type: 'password', autocomplete: 'off', placeholder: st === 'ok' ? '已保存（不显示）。要换就重新粘贴' : st === 'unreadable' ? '换了电脑，需要重新填' : (f.placeholder || '') });
      secretInputs[f.key] = inp;
      return field(f.label, inp, f.help);
    }
    if (f.choices) return field(f.label, select(f.choices, d[f.key] || f.choices[0][0], v => d[f.key] = v), f.help);
    return field(f.label, h('input', { value: d[f.key] ?? '', type: f.number ? 'number' : 'text', placeholder: f.placeholder || '',
      oninput: ev => d[f.key] = f.number ? (parseInt(ev.target.value) || undefined) : ev.target.value.trim() }), f.help);
  });
  const persist = async () => {
    const saved = await PUT('/api/docs/account', d);
    for (const [k, inp] of Object.entries(secretInputs)) if (inp.value.trim()) await PUT('/api/secret', { k: `acct:${saved.id}:${k}`, v: inp.value.trim() });
    await reloadBoot();
    return saved;
  };
  const actions = (spec.actions || []).map(act => h('button', { onclick: async () => {
    const saved = await persist(); d.id = saved.id;
    if (act.id === 'line_register') { try { const r = await POST(`/api/accounts/${saved.id}/line_register`); toast(r.success ? '已登记给 LINE，LINE 测试通过' : 'LINE 测试没通过：' + r.detail, r.success ? '' : 'err'); } catch (e) { /* 已提示 */ } }
    else if (act.id === 'youtube_oauth') { const r = await POST(`/api/accounts/${saved.id}/youtube_oauth`); window.open(r.url, '_blank'); toast('已在浏览器里打开授权页，授权完回到这里启动账号'); }
    else if (act.id === 'bilibili_qr') bilibiliQR(saved);
  } }, icon(act.id === 'bilibili_qr' ? 'qr' : 'link'), act.label));
  const lineUrl = spec.id === 'line' ? h('div.small.muted', 'webhook 地址会是：', h('code', (d.public_url || 'https://你的隧道地址').replace(/\/+$/, '') + '/line/' + (d.id || '（保存后生成）'))) : null;
  const body = h('div.form',
    spec.tier === 'unofficial' ? h('div.notice.bad', icon('alert'), h('div', h('b', '风险提示'), h('div.small', spec.risk))) : null,
    spec.experimental ? h('div.notice.warn', icon('alert'), h('div.small', spec.summary)) : null,
    h('details', { open: isNew }, h('summary', { style: { cursor: 'pointer', fontWeight: 600 } }, '开通步骤（照着做）'), h('ol.guide.mt8', spec.steps.map(t => h('li', h('div', t))))),
    h('div.form2', field('显示名', h('input', { value: d.name || '', placeholder: '只在面板里显示', oninput: ev => d.name = ev.target.value })),
      field('默认角色', roleSel, '没有排班规则命中时，这个账号说话时是谁')),
    fields, lineUrl,
    h('div.col.gap4', spec.flags.map(([k, label]) => h('label.row', toggle(!!d[k], v => d[k] = v), h('span', label)))),
    actions.length ? h('div.row.wrap', actions) : null,
    h('div.small.faint', '这类平台是「评论区」形态：别人 @ 她、回复她、给她发私信，她才回；不会主动插话。节奏和每日额度在 设置 → 平台 里调（评论、回复、私信分开）。'));
  modal(isNew ? '添加' + spec.name + '账号' : spec.name + ' 账号设置', body, { wide: true, foot: close => [
    !isNew && h('button.danger', { onclick: async () => {
      if (await confirmBox(`删除账号「${accountTitle(a)}」？（不会删除角色和记忆）`, '删除', true)) { await DEL('/api/docs/account/' + a.id); close(); refresh(); }
    } }, icon('trash'), '删除'),
    h('div.grow'), h('button', { onclick: close }, '取消'),
    h('button.primary', { onclick: async () => { await persist(); close(); toast('已保存'); refresh(); } }, '保存')] });
}
function bilibiliQR(acc) {
  const img = h('img', { alt: '二维码' });
  const tip = h('div.muted.small', '用小号的 B站手机 App 扫码并确认');
  let key = '', stop = false;
  const close = modal('B站扫码登录', h('div.col.gap12', h('div.qrbox', img), tip), { onclose: () => { stop = true; } });
  (async () => {
    const q = await POST(`/api/accounts/${acc.id}/bilibili_qr`);
    key = q.key; img.src = '/api/qr.svg?text=' + encodeURIComponent(q.url);
    while (!stop) {
      await new Promise(r => setTimeout(r, 2000));
      if (stop) break;
      const r = await GET(`/api/accounts/${acc.id}/bilibili_qr?key=${encodeURIComponent(key)}`);
      if (r.state === 'scanned') tip.textContent = '已扫码，请在手机上点确认';
      else if (r.state === 'expired') { tip.textContent = '二维码过期了，关掉重开'; break; }
      else if (r.state === 'ok') { toast('登录成功'); close(); refresh(); break; }
      else if (r.state === 'error') { tip.textContent = r.error; break; }
    }
  })();
}

// ---------- 体验模式 ----------
page('demo', '体验', 'star', async v => {
  let st = await GET('/api/demo');
  await reloadBoot();
  const box = h('div');
  const render = async () => {
    st = await GET('/api/demo');
    if (!st.on) {
      let role = (B().roles[0] || {}).id || '';
      return clear(box, h('div.card.col.gap12', h('h3', '体验模式'),
        h('div.small.muted', '不连任何账号。程序里开一个模拟的群，里面有几个按剧本说话的群友，你自己也能发言：看看角色怎么回、守门员什么时候插话、面板长什么样。退出后什么都不留。'),
        B().roles.length ? field('用哪个角色体验', select(B().roles.map(r => [r.id, r.name]), role, x => role = x))
          : h('div.notice.warn', icon('alert'), h('div', '还没有角色。', h('button.sm.primary', { onclick: () => templatePicker(render) }, '从模板新建一个'))),
        h('div', h('button.primary.big', { disabled: !B().roles.length, onclick: async () => { await POST('/api/demo/start', { role_id: role, chatter: true }); await reloadBoot(); render(); } }, icon('play'), '开始体验'))));
    }
    const msgs = h('div.msgs', { style: { maxHeight: '55vh', minHeight: '260px', overflow: 'auto' } });
    const logs = h('div.small.faint.mt8');
    const bubble = m => h('div.msg', { class: m.kind === 'bot' ? 'bot' : '' },
      h('div.who', m.name, h('span.faint', fmtTime(m.ts))), h('div.bubble', m.text));
    (await GET('/api/rooms/' + encodeURIComponent(st.room) + '/messages')).forEach(m => msgs.append(bubble(m)));
    msgs.scrollTop = msgs.scrollHeight;
    live('msg', m => { if (m.room === st.room) { msgs.append(bubble(m)); msgs.scrollTop = msgs.scrollHeight; } });
    live('log', l => { if (l.cat === 'gatekeeper' && l.data && l.data.room === st.room) { add(logs, h('div', `守门员：${l.data.allow ? '插话' : '不插'}（${l.data.brief}）`)); } });
    const ta = h('input', { placeholder: '以「你」的身份说一句（回车发送）', style: { flex: 1 } });
    let at = true;
    const send = async () => { const t = ta.value.trim(); if (!t) return; ta.value = ''; await POST('/api/demo/say', { text: t, at }); };
    ta.addEventListener('keydown', e => { if (e.key === 'Enter') send(); });
    clear(box,
      h('div.notice.mb8', icon('star'), h('div.grow', h('b', '体验模式'), h('div.small', st.has_key ? '已填服务商密钥：会真的调用模型，用量记在「用量 → 按用途 → 试聊」下。'
        : '还没填服务商密钥：回复是预设的演示句子，不调模型。要看真实效果，先填一个服务商密钥。')),
        st.has_key ? null : h('button.sm.primary', { onclick: () => go('settings/model') }, '去填密钥')),
      h('div.card', h('div.row.wrap.mb8', h('b', '体验群'), h('div.grow'),
        h('label.row', toggle(st.chatter, async v => { await POST('/api/demo/chatter', { on: v }); }), h('span.small', '模拟群友自己说话')),
        h('button.sm.danger', { onclick: async () => { await POST('/api/demo/stop'); await reloadBoot(); render(); } }, icon('x'), '退出体验')),
        msgs, logs,
        h('div.row.mt8', ta, h('label.row', toggle(true, v => at = v), h('span.small', '@她')), h('button.primary', { onclick: send }, icon('send'), '发送'))),
      h('div.small.faint.mt8', '提示：不@她时，由守门员判断要不要插话；@她必回。想试别的角色，退出后重新开始。'));
  };
  clear(v, box);
  await render();
}, { order: 8 });

// ---------- 新版本提醒 ----------
async function updateBanner() {
  try {
    const u = await GET('/api/update');
    if (!u.newer || u.dismissed) return;
    const bar = h('div.notice.ok.mb8', { id: 'update-bar' }, icon('star'), h('div.grow', `有新版本 ${u.latest}（当前 ${u.current}）。`, u.url ? h('a', { href: u.url, target: '_blank' }, ' 去看看') : null),
      h('button.sm.ghost', { onclick: async () => { await POST('/api/update/dismiss'); bar.remove(); } }, '不再提醒这个版本'));
    App.view.prepend(bar);
  } catch (e) { /* 没填仓库或没网，不打扰 */ }
}
