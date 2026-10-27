/* 群聊分寸与工具：联网搜索、预筛规则、群风格画像、表情包、提醒、插话力度自动调节。 */
'use strict';

// ---------- 联网搜索（设置 → 模型与接口）----------
async function searchCard() {
  const info = await GET('/api/tools');
  const sr = info.search, ts = info.tools;
  const wrap = h('div.card.col.mt16');
  const render = async () => {
    let cur = sr.choice;
    const detail = h('div.col.gap8');
    const showDetail = () => {
      const p = sr.providers.find(x => x.id === (cur === 'auto' ? sr.provider : cur));
      if (!p) return clear(detail, h('div.small.muted', '还没有可用的搜索服务：选一个并填好密钥；如果已经配了 DeepSeek、Kimi、智谱，或者 Z.ai / 智谱 Coding 套餐的模型，会自动用上那个密钥。'));
      const key = h('input', { type: 'password', placeholder: p.key_from === 'own' ? '已填（不显示）。要换就重新粘贴' : '粘贴密钥', autocomplete: 'off' });
      const url = h('input', { value: sr.searxng_url || '', placeholder: 'http://127.0.0.1:8080' });
      clear(detail, h('div.small.muted', p.help),
        p.key ? h('div.small', p.key_from === 'own' ? h('span.badge.ok', '已填（不显示）') : p.key_from ? h('span.badge.ok', `已用上「${p.key_from}」的密钥（要换就在这里另填一个）`) : h('span.badge.warn', '还没有密钥')) : null,
        p.key ? field(p.name + ' 的密钥', key, '只存在这台电脑，用 Windows 账户加密保存') : null,
        p.id === 'searxng' ? field('SearXNG 地址', url, '要在它的 settings.yml 里把 json 加进 search.formats') : null,
        h('div.row', h('button.sm.primary', { onclick: async () => {
          if (p.key && key.value.trim()) await PUT('/api/secret', { k: 'search:' + p.id, v: key.value.trim() });
          await PUT('/api/search', { provider: cur, searxng_url: p.id === 'searxng' ? url.value.trim() : undefined });
          toast('已保存'); App.route();
        } }, '保存'), p.key && p.key_from === 'own' ? h('button.sm.ghost', { onclick: async () => {
          await PUT('/api/secret', { k: 'search:' + p.id, v: '' });
          toast('已清除自己填的密钥'); App.route();
        } }, '清除自己填的密钥') : null));
    };
    clear(wrap, h('h3', '联网搜索'),
      h('div.small.muted', '主模型需要最新信息时，通过工具调用上网搜一下；搜到的内容只用于当次回复，不会写进记忆。用量单独记为「搜索」。'),
      ts.state === 'yes' ? h('div.small', h('span.badge.ok', '工具调用：支持'), ` 当前主模型 ${ts.model}`)
        : h('div.notice', { class: ts.state === 'no' ? 'bad' : 'warn' }, icon('alert'), h('div.small', ts.state === 'no' ? '当前主模型不支持工具调用，联网搜索和发表情包不生效：' + ts.reason : ts.reason)),
      field('搜索服务', select([['auto', '自动（优先 DeepSeek）'], ...sr.providers.map(p => [p.id, p.name])], cur, v => { cur = v; showDetail(); })), detail,
      h('div.small', sr.available ? h('span.badge.ok', '已就绪') : h('span.badge.warn', '现在不能用'), ' ', sr.available ? '' : sr.why,
        h('span.faint', '　', sr.limit ? `今天已搜 ${sr.today} / ${sr.limit} 次` : `今天已搜 ${sr.today} 次`)),
      await settingsForm('global', { keys: ['search_on', 'search_daily', 'search_rounds'] }));
    showDetail();
  };
  await render();
  return wrap;
}

// ---------- 预筛规则（设置 → 全局默认）----------
async function prefilterCard() {
  const d = await GET('/api/prefilter');
  let rules = d.rules;
  const list = h('div.col');
  const save = debounce(async () => {
    try { const r = await PUT('/api/prefilter', { rules }); rules = r.rules; toast('已保存'); } catch (e) { /* 已提示 */ }
  }, 500);
  const render = () => clear(list, rules.map((r, i) => h('div.row.wrap', toggle(r.on !== false, x => { r.on = x; save(); }),
    r.kind === 'builtin' ? h('span.grow', r.name) : [
      h('input', { value: r.name, style: { flex: '0 1 200px', minWidth: '120px' }, placeholder: '规则名', oninput: e => { r.name = e.target.value; save(); } }),
      h('input', { value: r.pattern, style: { flex: '1 1 200px', minWidth: '160px' }, placeholder: '正则，比如 ^\\s*抽奖', oninput: e => { r.pattern = e.target.value; save(); } })],
    r.kind !== 'builtin' ? h('button.ghost.icon', { title: '删除', onclick: () => { rules.splice(i, 1); save(); render(); } }, icon('trash')) : null)));
  render();
  return h('div.card.mt16.col', h('h3', '预筛规则'),
    h('div.small.muted', '没人@她的消息，命中下面任何一条就不去问守门员（省一次调用）。只用于主动插话；被@、被回复照旧必回。不按字数筛。'),
    list,
    h('div.row.wrap', h('button.sm', { onclick: () => { rules.push({ id: 'r' + Date.now(), name: '', kind: 'regex', pattern: '', on: true }); render(); } }, icon('plus'), '加一条正则规则'),
      h('button.sm.ghost', { onclick: () => { rules = JSON.parse(JSON.stringify(d.defaults)); save(); render(); } }, icon('undo'), '恢复默认规则'),
      h('span.small.faint', `累计省下 ${d.saved} 次守门员调用（用量页按时间段看）`)),
    h('div.small.faint', '别的机器人发的消息要不要跳过守门员、它 @ 她时接几轮，在上面「插话」分类的设置里选；先要在人物表里把它标成「这是别的机器人」。'));
}

// ---------- 群页：插话力度、风格画像 ----------
function tuneCard(roomId) {
  const box = h('div.card.col', h('h3', '插话力度'), h('div.small.muted', '加载中…'));
  GET('/api/tune').then(all => {
    const mine = all.filter(t => t.room === roomId);
    const lab = n => (EAGER_LABELS[n] || n);
    clear(box, h('h3', '插话力度'),
      h('div.small.muted', '设定值在下面的「本群设置」里改。打开「插话力度自动调节」后，守门员会顺带判断她上一次插话有没有人接，在你设定的上下档数之内调整；接得好会回升。'),
      mine.length ? mine.map(t => h('div.col', { style: { gap: '4px' } },
        h('div.row.wrap', h('b', t.role), h('span.badge', `设定 ${lab(t.base)}`), h('span.badge', { class: t.level === t.base ? 'blue' : 'orange' }, `现在 ${lab(t.level)}`),
          t.auto ? h('span.badge.ok', '自动调节开着') : h('span.badge', '自动调节关着'),
          t.paused ? h('span.badge.warn', `被叫停，还剩约 ${Math.ceil(t.paused / 60)} 分钟`) : null),
        t.history.length ? t.history.slice(-5).reverse().map(x => h('div.small.faint', `${fmtTime(x.ts)}　${x.delta > 0 ? '＋' : ''}${x.delta} 档　${x.reason}`))
          : h('div.small.faint', '还没有调整记录'))) : h('div.faint.small', '这个群里现在没有在线的机器人'));
  });
  return box;
}
const EAGER_LABELS = { 1: '很少', 2: '偏少', 3: '正常', 4: '偏多', 5: '话痨' };

async function styleCard(roomId) {
  const all = await GET('/api/styles');
  const s = all.find(x => x.room === roomId) || { content: '', have: 0, need: 30 };
  const ta = h('textarea', { rows: 3, value: s.content, placeholder: s.content ? '' : `还没有画像（群友消息攒到 ${s.need || 30} 条才开始生成，现在 ${s.have || 0} 条）` });
  return h('div.card.col', h('h3', '这个群的说话习惯'),
    h('div.small.muted', '在整理记忆的那次调用里顺带更新，只描述句子长短、标点、口头禅、表情习惯，不额外花钱。她说话时只会稍微贴近一点，人设、态度、语言、平台格式永远优先。'),
    ta, h('div.row', h('button.sm.primary', { onclick: async () => { await PUT('/api/styles', { room: roomId, text: ta.value }); toast('已保存'); } }, '保存'),
      h('button.sm.ghost', { onclick: async () => { await PUT('/api/styles', { room: roomId, text: '' }); ta.value = ''; toast('已清空'); } }, icon('trash'), '清空')));
}

// ---------- 记忆页的三个标签 ----------
async function stylePage(body) {
  const all = await GET('/api/styles');
  add(body, h('div.notice.mb8', icon('info'), h('div.small', '每个群一份画像：句子长短、标点、常用的梗和口头禅、表情习惯。只在整理记忆时顺带更新，群友消息攒够才生成，不编造。可以直接改、清空。开关在 设置 → 全局默认 → 学习。')),
    all.map(s => h('div.card.col.mb8', h('div.row', h('b.grow', s.room_name), s.content ? h('span.small.faint', fmtTime(s.ts)) : h('span.small.faint', `攒了 ${s.have || 0} / ${s.need || 30} 条`)),
      (() => {
        const ta = h('textarea', { rows: 2, value: s.content, placeholder: '还没有画像' });
        return h('div.col', ta, h('div.row', h('button.sm.primary', { onclick: async () => { await PUT('/api/styles', { room: s.room, text: ta.value }); toast('已保存'); } }, '保存'),
          h('button.sm.ghost', { onclick: async () => { await PUT('/api/styles', { room: s.room, text: '' }); ta.value = ''; toast('已清空'); } }, icon('trash'), '清空')));
      })())),
    !all.length ? h('div.card.empty', icon('brain'), h('div', '还没有群。')) : null);
}

async function stickersPage(body) {
  const rows = await GET('/api/stickers');
  const info = await GET('/api/tools');
  const favs = await GET('/api/stickers/fav');
  const favText = f => {
    const l = f.last || {};
    if (!l.ts) return '还没同步过';
    const when = new Date(l.ts * 1000).toLocaleString();
    return l.error ? `${when} 同步没成功：${l.error}` : `${when} 同步过：收藏里共 ${l.total} 张，新收 ${l.added} 张，删掉 ${l.removed} 张` + (l.pending ? `，还有 ${l.pending} 张下一轮再描述` : '');
  };
  const favBox = favs.length ? h('div.card.col.mb8', { style: { gap: '6px' } },
    h('div', h('b', '账号收藏的表情（QQ 收藏表情 / 微信自定义表情）')),
    h('div.small.muted', '这个账号收藏的表情（QQ 是收藏表情，微信是表情面板里的「自定义表情」），定期同步下来、各写一句描述，和本群学来的表情包一起挑；这个账号所在的所有群都能用，不占「每个群最多存几张」。以 QQ / 微信里的收藏为准：想去掉，在 QQ 里取消收藏、在微信里删掉。微信的要打开表情面板一格一格翻着看，会占用微信窗口一会儿。开关、同步间隔、是否做内容检查在 设置 → 全局默认 → 工具。'),
    ...favs.map(f => h('div.small', `${f.name}：现有 ${f.count} 张 · ${favText(f)}`)),
    h('div', h('button.sm', { onclick: async ev => {
      const btn = ev.currentTarget; btn.disabled = true;
      try { await POST('/api/stickers/sync'); toast('已同步'); App.route(); } catch (e) { /* 已提示 */ } finally { btn.disabled = false; }
    } }, icon('refresh'), '现在同步收藏'))) : null;
  add(body, h('div.notice.mb8', icon('info'), h('div.small', '只学群里「常用」的：同一张图被多人或多次发过才学，用看图模型出描述并判断是否适合，近似图算同一张。发的时机和发哪张都由主模型在回复时决定（按心情找出几张候选，她看描述自己挑，也可以都不选），发一张算一条发言。阈值、频率、容量、描述字数、候选张数在 设置 → 全局默认 → 工具。')),
    favBox,
    h('div.card.col.mb8', { style: { gap: '6px' } },
      h('div.small.muted', '表情包的描述最多多少字在 设置 → 全局默认 → 工具 里改；改了以后，已有的会在后台自动重新描述（可以关）。想不看字数、全部重来一遍，点下面的按钮（每张一次看图调用，算在看图的用量里）。'),
      h('div', h('button.sm', { onclick: async ev => {
        const btn = ev.currentTarget; btn.disabled = true;
        try { const r = await POST('/api/stickers/redescribe'); toast(r.running ? '正在重新描述，等它跑完' : `开始重新描述 ${r.total} 张，在后台进行`); } catch (e) { /* 已提示 */ } finally { btn.disabled = false; }
      } }, icon('refresh'), '重新描述全部表情包'))),
    h('div.small.muted.mb8', `共 ${info.stickers.count} 张（其中账号收藏的 ${info.stickers.fav || 0} 张），占 ${(info.stickers.bytes / 1048576).toFixed(1)} MB`),
    rows.length ? h('div.grid', rows.map(r => h('div.card.col', { style: { gap: '6px' } },
      h('img', { src: `/api/stickers/${r.id}/image`, style: { maxWidth: '100%', maxHeight: '140px', objectFit: 'contain', borderRadius: '8px' } }),
      h('div.small', r.source === 'fav' ? [h('span.badge.blue', '收藏'), ' ', r.descr] : r.descr),
      h('div.small.faint', r.source === 'fav' ? `账号收藏 · 发过 ${r.uses} 次` : `${r.room_name} · 发过 ${r.uses} 次`),
      r.source === 'fav' ? null : h('div', h('button.sm.ghost.danger', { onclick: async () => { await DEL('/api/stickers/' + r.id); App.route(); } }, icon('trash'), '删除')))))
      : h('div.card.empty', icon('star'), h('div', '还没学到表情包。群里有人把同一张图发了几次、或几个人都发了，就会学。')));
}

async function remindersPage(body) {
  const rows = await GET('/api/reminders');
  add(body, h('div.notice.mb8', icon('info'), h('div.small', '有人对她说「20 分钟后提醒我……」「明天早上 8 点提醒我……」就会出现在这里。到点由当时在该群值班的角色用自己的口吻 @ 他提醒。每人同时最多几条在 设置 → 全局默认 → 学习。')),
    rows.length ? h('div.list', rows.map(r => h('div.item', icon('calendar'), h('div.grow', h('b', `${r.user_name}`), '：', r.text, h('div.small.faint', `${fmtTime(r.due)} · ${r.room_name || r.chat_id}`)),
      h('button.ghost.icon', { title: '删除', onclick: async () => { await DEL('/api/reminders/' + r.id); App.route(); } }, icon('trash')))))
      : h('div.card.empty', icon('calendar'), h('div', '现在没有挂着的提醒。')));
}

// ---------- 重置记忆（记忆页）----------
async function resetMemoryDialog() {
  const mem = await GET('/api/memories');
  let scope = 'all', target = '';
  const kinds = new Set(Object.keys(RESET_LABELS));
  const targetBox = h('div'), counts = h('div.small'), foot = { btn: null };
  const options = () => scope === 'space' ? mem.spaces.map(s => [s, spaceLabel(s)])
    : scope === 'room' ? B().chats.map(c => [c.id, c.name || c.id]) : scope === 'person' ? B().people.map(p => [p.id, pLabel(p)]) : [];
  const refresh = debounce(async () => {
    if (scope !== 'all' && !target) { clear(counts, h('span.faint', '先选一个' + ({ space: '记忆空间', room: '群', person: '人' }[scope]))); foot.btn.disabled = true; return; }
    try {
      const r = await POST('/api/memory/reset/preview', { scope: { type: scope, id: target }, kinds: [...kinds] });
      const rows = Object.entries(r.counts);
      clear(counts, rows.length ? rows.map(([k, n]) => h('div', `${RESET_LABELS[k]}：`, h('b', String(n)), ' 条')) : h('span.faint', '没有选内容'));
      foot.btn.disabled = !rows.some(([, n]) => n > 0);
    } catch (e) { /* 已提示 */ }
  }, 150);
  const showTarget = () => {
    const o = options();
    target = o.length ? o[0][0] : '';
    clear(targetBox, scope === 'all' ? null : (o.length ? select(o, target, v => { target = v; refresh(); }) : h('div.faint.small', '还没有可选的')));
    refresh();
  };
  const body = h('div.form',
    h('div.small.muted', '先备份所选范围的内容，再删；删完可以在记忆页「撤销刚才的重置」。人物表、人设、设置、用量不会被动。'),
    field('范围', seg([['all', '全部'], ['space', '某个记忆空间'], ['room', '某个群'], ['person', '某个人']], scope, x => { scope = x; showTarget(); })), targetBox,
    field('内容（可多选）', h('div.col', Object.entries(RESET_LABELS).map(([k, label]) => h('label.row.small', h('input', { type: 'checkbox', checked: true,
      onchange: e => { e.target.checked ? kinds.add(k) : kinds.delete(k); refresh(); } }), h('span', label))))),
    field('将删掉', counts));
  modal('重置记忆', body, { foot: c => {
    foot.btn = h('button.danger', { onclick: async () => {
      const r = await POST('/api/memory/reset/preview', { scope: { type: scope, id: target }, kinds: [...kinds] });
      const lines = Object.entries(r.counts).filter(([, n]) => n > 0).map(([k, n]) => `${RESET_LABELS[k]} ${n} 条`).join('、');
      if (!await confirmBox(`确定重置？将删掉：${lines}。删之前会先备份，可以撤销。`, '重置', true)) return;
      await POST('/api/memory/reset', { scope: { type: scope, id: target }, kinds: [...kinds], confirm: true });
      toast('已重置'); c(); App.route();
    } }, '重置…');
    return [h('button', { onclick: c }, '取消'), foot.btn];
  } });
  showTarget();
}
