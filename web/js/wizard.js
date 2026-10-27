/* 首次设置向导：密码 → API Key → 选平台 → 各平台准备 → 完成。 */
'use strict';

const Wizard = {
  async open(rerun = false) {
    const st = await GET('/api/state');
    S.theme = st.theme; applyTheme(st.theme);
    this.step = st.has_password ? 1 : 0;
    if (!st.has_password) this.step = 0;
    this.rerun = rerun;
    S.wizard = true;
    this.render();
  },
  async render() {
    const steps = ['密码', '服务商', '角色', '用户卡', '记忆', '平台', '账号', '完成'];
    const card = h('div.card.wizard.col.gap16');
    clear($('#root'), h('div.center-page', card));
    add(card, h('div.row.gap12', h('img', { src: 'icon-192.png', width: 36 }), h('div.grow', h('h2', '设置向导'), h('div.small.muted', `第 ${this.step + 1} 步 / 共 ${steps.length} 步：${steps[this.step]}`)),
      this.step > 0 && h('button.ghost.sm', { onclick: () => this.finish(true) }, '跳过向导')),
      h('div.steps', steps.map((_, i) => h('i', { class: i <= this.step ? 'on' : '' }))));
    const body = h('div.col.gap12');
    add(card, body);
    if (this.step > 0) await reloadBoot();
    await [this.sPassword, this.sKey, this.sRole, this.sYou, this.sEmbed, this.sPlatforms, this.sAccounts, this.sDone][this.step].call(this, body);
  },
  next() { this.step++; this.render(); },
  back() { this.step--; this.render(); },
  nav(nextBtn, canBack = true) {
    return h('div.row.mt8', canBack && this.step > 1 ? h('button', { onclick: () => this.back() }, icon('back'), '上一步') : null, h('div.grow'), nextBtn);
  },

  async sPassword(body) {
    const p1 = h('input', { type: 'password', placeholder: '至少 4 位' });
    const p2 = h('input', { type: 'password', placeholder: '再输一遍' });
    add(body, h('div', '欢迎！先给面板设个密码。电脑上的面板窗口不用输；手机或别的电脑打开面板时要输。'),
      field('面板密码', p1), field('确认密码', p2),
      this.nav(h('button.primary', { onclick: async () => {
        if (p1.value.length < 4) return toast('密码至少 4 位', 'err');
        if (p1.value !== p2.value) return toast('两次输入不一样', 'err');
        await POST('/api/setup/password', { password: p1.value });
        this.next();
      } }, '下一步', icon('chev')), false));
  },

  async sKey(body) {
    if (!B().providerMeta) B().providerMeta = await GET('/api/providers');
    const meta = B().providerMeta;
    const ep = byId(B().endpoints, 'default') || { id: 'default' };
    const old = h('input', { value: B().old_bot_guess || '', placeholder: '旧程序 bot 文件夹' });
    const pick = ['deepseek', 'zhipuai', 'alibaba-cn', 'moonshotai-cn', 'siliconflow-cn', 'volcengine', 'openai', 'anthropic', 'google', 'openrouter'];
    const opts = [...pick.map(id => meta.providers.find(p => p.id === id)).filter(Boolean), ...meta.providers.filter(p => !pick.includes(p.id))];
    let prov = opts.find(p => p.id === (ep.provider || 'deepseek')) || opts[0];
    const key = h('input', { type: 'password', placeholder: ep.has_key ? '已填（不显示）。要换就重新粘贴' : '粘贴 API Key', autocomplete: 'off' });
    const hint = h('div.small.muted');
    const res = h('div.small');
    const g = B().settings.global || {};
    const cap = h('input', { type: 'number', min: 0, value: g.daily_token_limit || 0, style: { maxWidth: '140px' } });
    const hard = h('input', { type: 'checkbox' });
    hard.checked = !!g.usage_hard_cap;
    const saveCap = async () => {
      await PUT('/api/settings', { scope: 'global', key: 'daily_token_limit', value: Math.max(0, parseInt(cap.value, 10) || 0) });
      await PUT('/api/settings', { scope: 'global', key: 'usage_hard_cap', value: hard.checked });
    };
    const sel = select(opts.map(p => [p.id, provName(p)]), prov.id, v => { prov = opts.find(p => p.id === v); setHint(); });
    const setHint = () => {
      clear(hint, `接口地址 ${prov.api}`, prov.env && prov.env[0] ? `；也可以不在这里填，改用环境变量 ${prov.env[0]}` : '', prov.proxy ? '；国内访问一般要代理' : '', prov.doc ? h('span', '；', h('a', { href: prov.doc, target: '_blank' }, '文档')) : null);
    };
    setHint();
    const save = async () => {
      const d = { id: 'default', name: provName(prov), provider: prov.id, protocol: prov.protocol, base_url: prov.api, key_env: (prov.env || [])[0] || '', use_proxy: !!prov.proxy, dialect: 'auto' };
      await PUT('/api/docs/endpoint', d);
      if (key.value.trim()) await PUT('/api/secret', { k: 'endpoint:default', v: key.value.trim() });
      await reloadBoot();
    };
    add(body,
      B().old_bot_guess ? h('div.notice.ok', icon('star'), h('div.grow', h('b', '发现旧程序'), h('div.small', '一键导入旧程序的 API Key、模型和冷却设置、人设、她妈那张人物卡的 QQ 号。旧程序的文件不会被改动。'),
        h('div.row.mt8', old, h('button.primary', { onclick: async () => {
          const r = await POST('/api/import_old', { path: old.value.trim() }); toast('已导入：' + r.done.join('、')); await reloadBoot(); this.render();
        } }, '一键导入')))) : null,
      h('div', '机器人说话要调用大模型。选一家服务商、填上你的 API Key（默认选好的是 DeepSeek，用 deepseek-flash）。'),
      field('服务商', sel, '内置几十家；找不到的，完成向导后到 设置 → 模型与接口 里「自定义 API」添加'), hint,
      field('API Key', key, '只存在这台电脑，用 Windows 账户加密保存'),
      h('div.card.col.gap8', h('b', '设一个每日用量上限（建议）'),
        h('div.small.muted', '机器人在群里说话是按量计费的。设一个每天最多用多少，万一被人刷屏或出了差错，也不会一夜烧掉很多。默认 0 = 不限。'),
        field('每天最多用（万 tokens，0 为不限）', cap, '到了就先停止主动插话，被@和私聊照常回。'),
        h('label.row.small', hard, h('span', '到了以后，被@和私聊也停（只回一句固定的话）；记忆整理、看图、搜索等也一起暂停，第二天自动恢复'))),
      h('div.row', h('button.sm', { onclick: async () => {
        await save(); res.textContent = '测试中…'; res.className = 'small';
        try { const r = await POST('/api/endpoints/default/test'); res.textContent = `✓ 连通，${r.models.length} 个可用模型`; res.className = 'small ok'; }
        catch (e) { res.textContent = '✗ ' + e.message; res.className = 'small bad'; }
      } }, '测试连接（不耗额度）'), res),
      this.nav(h('button.primary', { onclick: async () => {
        await save();
        await saveCap();
        if (!byId(B().endpoints, 'default').has_key && !byId(B().endpoints, 'default').env_set && !(await confirmBox('还没填 API Key，机器人暂时说不了话（可以先用「体验」看看效果）。先跳过？', '跳过'))) return;
        this.next();
      } }, '下一步', icon('chev'))));
  },

  async sRole(body) {
    const roles = B().roles;
    const feat = [['gentle-friend', '温和版群友', '好相处的普通群友，适合第一次用'],
      ['translator', '翻译', '中外互译'], ['recap', '群聊总结与答疑', '总结讨论、回答问题']];
    add(body, h('div', '机器人要扮演谁？选一个角色模板，之后随便改；也可以先跳过，完成后到「角色」页再建。'),
      roles.length ? h('div.card.col.gap8', h('b', '已有的角色'), roles.map(r => h('div.row', h('span.badge.blue', r.name), h('span.small.muted.grow', r.summary || '')))) : null,
      h('div.grid', feat.map(([id, name, desc]) => h('div.card.col', { style: { gap: '6px' } }, h('b', name), h('div.small.muted', desc),
        h('div', h('button.sm.primary', { onclick: async () => { await POST(`/api/templates/${id}/use`, { name }); await reloadBoot(); toast('已新建角色「' + name + '」'); this.render(); } }, '用这个'))))),
      h('div', h('button', { onclick: () => templatePicker(() => this.render()) }, icon('star'), '看全部模板')),
      this.nav(h('button.primary', { onclick: () => this.next() }, '下一步', icon('chev'))));
  },

  // 用户卡：她该怎么称呼你。「用户」是系统里固定的一张人物卡（装好就有，名字等都空着），这里把它填上。人设里的 {{用户}} 会换成这里的名字。可以跳过。
  async sYou(body) {
    const me = { ...((B().people || []).find(p => p.self) || {}) };
    const name = h('input', { value: me.name || '', placeholder: '她叫你什么名字，比如：海豚' });
    const ident = h('input', { value: me.identity || '', placeholder: '比如：她妈、她爸、她老师（你和她的关系）', oninput: () => renderCalls() });
    let calls = (me.calls && me.calls.length) ? [...me.calls] : (me.call ? [me.call] : []);
    const callsBox = h('div.col.gap4');
    const renderCalls = () => clear(callsBox, tagsInput(calls, x => { calls = x; renderCalls(); }, '输入后回车'),
      callPresets(ident.value).length ? h('div.row.wrap.small', h('span.muted', '常用称呼（点一下加上或去掉）：'), callPresets(ident.value).map(c => h('span.chip', { 'data-raw': '1', class: calls.includes(c) ? 'on' : '',
        onclick: () => { calls = calls.includes(c) ? calls.filter(x => x !== c) : [...calls, c]; renderCalls(); } }, c))) : null);
    renderCalls();
    const qq = tagsInput((me.qq || []).map(String), x => me.qq = x);
    const wx = tagsInput(me.wechat_remark || [], x => me.wechat_remark = x);
    const dc = tagsInput(me.discord || [], x => me.discord = x);
    add(body, h('div', '「用户」卡是你自己：人设里的 {{用户}} 会换成你在这里填的名字。你和她是什么关系（女儿、学生、朋友……）、她怎么称呼你，也在这张卡上定：这里填的身份和称呼会写进卡上「全体角色」那套关系方案，以后到「人物」页可以再加别的方案。她也靠这张卡认出你是谁（管理指令里的「我」就是你）。'),
      h('div.form2', field('名字', name), field('身份', ident, '写进提示词的小标题；填「她妈」「她爸」时，下面会给出常用称呼可以点选')),
      field('称呼（可以填多个）', callsBox, '她怎么叫你，写进人设成「称呼：A、B、C，随场合换着叫」'),
      h('details', h('summary.small.muted', { style: { cursor: 'pointer' } }, '各平台上的账号（可选，填了她才认得出你；以后在「人物」页还能改）'),
        h('div.form.mt8', field('QQ 号', qq, '回车分隔'), field('微信秘密备注', wx, '在机器人小号的微信里给你设的备注'), field('Discord ID', dc))),
      h('div.faint.small', '不想填可以跳过：名字没填时，人设里提到「谁的女儿」的句子就不会出现。'),
      this.nav(h('div.row', h('button', { onclick: () => this.next() }, '跳过这一步'), h('button.primary', { onclick: async () => {
        if (!name.value.trim()) return toast('名字不能空（不想填就点「跳过这一步」）', 'err');
        // 身份、称呼写进「全体角色」那套关系方案（和人物页一致）；服务端会拿这套方案盖回卡片顶层字段
        const schemes = (me.schemes || []).map(sc => (sc.scope || {}).mode === 'all' || !sc.scope ? { ...sc, identity: ident.value.trim(), calls } : sc);
        await PUT('/api/docs/person', { ...me, id: me.id, name: name.value.trim(), identity: ident.value.trim(), call: calls[0] || '', calls, schemes, self: true, admin: true,
          qq: (me.qq || []).map(String), discord: me.discord || [], wechat_remark: me.wechat_remark || [], nicks: me.nicks || {}, note: me.note || '', knows: me.knows || '',
          attitude: me.attitude || { presets: ['亲近贴心'], custom: '' }, groups: me.groups || [] });
        await reloadBoot(); this.next();
      } }, '保存并下一步', icon('chev')))));
  },

  async sEmbed(body) {
    const info = await GET('/api/embed');
    const rec = info.recommended, ready = info.local[rec].ready, cur = info.conf.preset;
    const prog = h('span.small.muted');
    const off = on('download', d => {
      if (d.what !== 'embed') return;
      if (d.error) prog.textContent = '下载失败：' + d.error + '（国内可以手动下载，见使用说明）';
      else if (d.finished) { prog.textContent = '下载完成'; PUT('/api/embed/config', { source: 'local', preset: rec }).then(() => { off(); this.render(); }); }
      else prog.textContent = `${d.file}：${(d.done / 1048576).toFixed(1)} / ${(d.total / 1048576).toFixed(1)} MB`;
    });
    const gte = rec !== 'bge-small-zh-v1.5';
    add(body, h('div', '她除了记得在场的人和群前情，还会按眼下聊的话题想起「意思相关」的旧事。这靠一个在本机运行的小模型，不调接口、不花钱。'),
      h('div.card.col.gap8', h('b', `你的电脑内存 ${info.ram_gb} GB`),
        gte ? [h('div.small', `推荐用效果最好的「${info.local[rec].name}」：要下载约 ${info.local[rec].mb} MB，运行时多占内存约 ${info.local[rec].mem} MB。`),
          cur === rec ? h('span.badge.ok', '已在使用') : ready
            ? h('div', h('button.primary', { onclick: async () => { await PUT('/api/embed/config', { source: 'local', preset: rec }); this.render(); } }, '用它'))
            : h('div.row', h('button.primary', { onclick: async () => { prog.textContent = '开始下载…'; await POST('/api/embed/download', { preset: rec }); } }, icon('download'), '一键下载并使用'), prog)]
          : h('div.small', '内存不太宽裕，用随安装包带的小模型（多占内存约 50 MB），已经就绪，不用下载。'),
        h('div.small.faint', gte ? '不想下载就跳过这一步，先用随包的小模型 bge-small（已就绪）。' : ''),
        h('div.small.faint', '以后可以在 记忆 → 语义回忆 里换模型，或改用接口。')),
      this.nav(h('button.primary', { onclick: () => { off(); this.next(); } }, '下一步', icon('chev'))));
  },

  async sPlatforms(body) {
    const p = { ...B().platforms };
    const desc = { qq: '用 QQ 小号当机器人，扫码登录。需要电脑上装 QQ（或用 NapCat 一键包）。', wechat: '用微信小号，靠界面自动化。微信窗口要一直开着。', discord: '用 Discord 官方机器人账号。国内需要代理。' };
    add(body, h('div', '要在哪些平台上用？以后随时能在「账号」页改。'),
      Object.entries(PLAT_MAIN).map(([k, name]) => h('div.card.row', plat(k), h('div.grow', h('b', name), h('div.small.muted', desc[k])), toggle(p[k], v => p[k] = v))),
      this.nav(h('button.primary', { onclick: async () => { await PUT('/api/platforms', p); this.next(); } }, '下一步', icon('chev'))));
  },

  async sAccounts(body) {
    const on_ = Object.entries(B().platforms).filter(([, v]) => v).map(([k]) => k);
    if (!on_.length) add(body, h('div.muted', '没选平台，可以直接下一步。'));
    const role = B().roles[0];
    for (const p of on_) {
      const box = h('div.card.col', h('div.row', plat(p), h('h3', PLAT[p])));
      const accs = B().accounts.filter(a => a.platform === p);
      if (accs.length) add(box, accs.map(a => h('div.row', statusBadge(a), h('span.grow', accountTitle(a)), h('button.sm', { onclick: () => editAccount(a) }, '设置'))));
      // 检查要几秒（微信要加载 wxauto4、读微信版本）：先把整页和「下一步」画出来，检查结果到了再填进卡片
      if (p === 'qq') {
        const slot = h('div.small.muted', '检查 NapCat 中…');
        add(box, slot);
        GET('/api/napcat/info').then(info => clear(slot, info.found ? h('div.notice.ok', icon('check'), 'NapCat 已就绪') : h('div.notice.warn', icon('alert'), h('div.grow', '需要先准备 NapCat（QQ 登录组件）'), h('button.sm.primary', { onclick: () => napcatSetup() }, '准备')),
          !accs.length && this.qqQuick(role))).catch(() => clear(slot, h('div.notice.warn', icon('alert'), '没检查成功，稍后可以在「账号」页再看')));
      }
      if (p === 'wechat') {
        const slot = h('div.small.muted', '检查微信中…（要几秒）');
        add(box, slot);
        GET('/api/wechat/check').then(c => clear(slot, wechatNotice(c), !accs.length && h('button', { onclick: () => editAccount({ platform: 'wechat', role_id: role && role.id }) }, icon('plus'), '添加微信小号')))
          .catch(() => clear(slot, h('div.notice.warn', icon('alert'), '没检查成功，稍后可以在「账号」页再看')));
      }
      if (p === 'discord') add(box, !accs.length && h('button', { onclick: () => editAccount({ platform: 'discord', role_id: role && role.id }) }, icon('plus'), '添加 Discord 机器人（有分步引导）'));
      add(body, box);
    }
    add(body, h('div.small.muted', role ? `新账号默认挂角色「${role.name}」，以后在账号页能换。扫码登录可以现在做，也可以完成向导后在「总览」里点「扫码」。` : ''),
      this.nav(h('button.primary', { onclick: () => this.next() }, '下一步', icon('chev'))));
    const off = on('account', () => { if (this.step === 6) { off(); this.render(); } });
  },
  qqQuick(role) {
    const qq = h('input', { inputmode: 'numeric', placeholder: '机器人小号的 QQ 号' });
    return h('div.row', qq, h('button.primary', { onclick: async () => {
      if (!/^\d{5,12}$/.test(qq.value.trim())) return toast('QQ 号不对', 'err');
      const a = await PUT('/api/docs/account', { platform: 'qq', qq: qq.value.trim(), role_id: role && role.id, name: '' });
      toast('已添加，正在启动 NapCat…');
      await POST(`/api/accounts/${a.id}/start`);
      await reloadBoot();
      const acc = byId(B().accounts, a.id);
      setTimeout(() => showQR(acc), 1500);
      this.render();
    } }, '添加并扫码'));
  },

  async sDone(body) {
    add(body, h('div', '差不多了！这几个开关以后在 设置 → 系统 里也能改：'),
      h('div.card', await settingsForm('global', { keys: ['autostart', 'hide_console', 'notify_windows', 'theme'] })),
      h('div.notice', icon('info'), h('div.small', '关掉面板窗口机器人照样在跑（任务栏右下角的托盘图标）。右键托盘图标可以停止或退出。')),
      this.nav(h('button.primary.big', { onclick: () => this.finish(false) }, icon('check'), '完成，开始使用')));
  },
  async finish(skip) {
    await PUT('/api/kv', { k: 'setup_done', v: true });
    if (!skip) await POST('/api/engine/start').catch(() => {});
    location.hash = '#/home';
    location.reload();
  },
};
