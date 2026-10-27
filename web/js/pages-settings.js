/* 设置：模型与接口、全局默认、平台、访问与安全、系统、数据迁移、关于。 */
'use strict';

page('settings', '设置', 'gear', async (v, [tab = 'model']) => {
  await reloadBoot();
  const body = h('div');
  clear(v, tabs([['model', '模型与接口'], ['defaults', '全局默认'], ['platform', '平台'], ['access', '访问与安全'], ['system', '系统'], ['data', '数据与迁移'], ['about', '关于']],
    tab, t => go('settings/' + t)), body);
  const T = { model: setModel, defaults: setDefaults, platform: setPlatform, access: setAccess, system: setSystem, data: setData, about: setAbout };
  await T[tab](body);
}, { order: 11 });

async function setModel(body) {
  const box = h('div');
  add(body, h('div.grid2', h('div',
    h('div.notice.mb8', icon('info'), h('div.small', '默认用 DeepSeek 官方的 deepseek-flash（官方别名，永远指向最新的 Flash）。主模型、守门员、记忆整理、看图可以各选一家服务商和模型。',
      '不同服务商的「思考强度」写法不一样，程序会自动对应；不认识的服务商不发思考参数。')),
    box),
    h('div.card', h('h3.mb8', '模型'), await settingsForm('global', { cats: ['模型'], top: ['main_endpoint', 'main_model', 'main_effort', 'proxy'] }),
      h('div.notice.mt8', icon('info'), h('div.small', '看图模型要选能看图的（模型选择里带「能看图」标记的）。DeepSeek 官方说 deepseek-flash 能看图；每张图多一次调用。')))));
  await providersUI(box);
  add(body, await searchCard());
}

async function setDefaults(body) {
  add(body, h('div.notice.mb8', icon('info'), h('div', '这里是最底下的一层：所有角色、平台、群没单独改的项都用这里的值。每项都有合理默认值，不碰也能正常跑。')),
    h('div.card', await settingsForm('global', { exclude: ['模型', '系统', '平台'] })),
    await prefilterCard());
}

async function setPlatform(body) {
  let p = 'qq';
  const box = h('div');
  const show = async () => clear(box, await settingsForm('platform:' + p));
  add(body, h('div.row.mb8', seg(PLAT_PICK, p, x => { p = x; show(); }), h('span.small.faint', '每个平台单独设置：开关、输出格式、模型参数、冷却、上限、上下文长度、在哪些群说话。')),
    h('div.card', box));
  show();
}

async function setAccess(body) {
  const lan = await GET('/api/lan');
  const old = h('input', { type: 'password', placeholder: '原密码' });
  const pw1 = h('input', { type: 'password', placeholder: '新密码（至少 4 位）' });
  const urls = lan.ips.map(ip => `http://${ip}:${lan.port}`);
  add(body, h('div.grid2',
    h('div.card.form', h('h3', '面板密码'), h('div.small.muted', '手机、局域网打开面板要输密码。电脑上的面板窗口免密码。'),
      old, pw1, h('div', h('button.primary', { onclick: async () => { await POST('/api/setup/password', { old: old.value, password: pw1.value }); toast('密码已修改'); old.value = pw1.value = ''; } }, '修改密码')),
      h('button.sm.ghost', { onclick: async () => { await POST('/api/logout'); location.reload(); } }, '退出登录（本设备）')),
    h('div.card.form', h('h3', '手机访问'),
      await settingsForm('global', { keys: ['panel_lan', 'panel_public', 'panel_port'] }),
      lan.enabled && urls.length ? h('div.col', h('div.small', '手机和电脑连同一个 Wi-Fi，用手机浏览器打开（改完开关要重启程序）：'),
        urls.map(u => h('div.row', h('code', u))), h('div.qrbox', h('img', { src: '/api/qr.svg?text=' + encodeURIComponent(urls[0]) })))
        : h('div.small.muted', '默认只有这台电脑能打开面板。打开「允许局域网访问」并重启程序后，这里会出现手机扫码地址。'),
      h('details', h('summary', { style: { cursor: 'pointer', fontWeight: 600 } }, '添加到手机主屏幕'),
        h('div.small.mt8', 'iPhone：Safari 打开面板 → 分享按钮 →「添加到主屏幕」。安卓：Chrome 打开面板 → 右上角菜单 →「添加到主屏幕」。之后像 App 一样从桌面图标打开。')))),
    h('div.card.mt16', h('h3.mb8', '在外面（不在家里 Wi-Fi）也想用手机管理？用 Tailscale'),
      h('div.small.muted.mb8', '不要把面板直接暴露到公网。Tailscale 会在你的电脑和手机之间建一条加密的私人通道，只有你自己的设备能连。免费版够用。'),
      h('ol.guide',
        h('li', h('div', '电脑上：到 ', h('a', { href: 'https://tailscale.com/download', target: '_blank' }, 'tailscale.com/download'), ' 下载安装，用微软/谷歌/GitHub 账号登录。')),
        h('li', h('div', '手机上：应用商店搜「Tailscale」安装，用同一个账号登录。')),
        h('li', h('div', '本页打开「允许局域网访问」，重启程序。')),
        h('li', h('div', '电脑上点任务栏的 Tailscale 图标，看到这台电脑的地址（100.x.x.x）。手机浏览器打开 ', h('code', `http://100.x.x.x:${lan.port}`), '，输入面板密码。')),
        h('li', h('div', '不用时可以在手机上关掉 Tailscale 的开关。')))));
}

async function setSystem(body) {
  const bks = await GET('/api/backups');
  add(body, h('div.grid2',
    h('div.card', h('h3.mb8', '系统'), await settingsForm('global', { cats: ['系统'], exclude: [], top: ['ui_lang', 'theme', 'autostart'] }).then(f => { $$('.set-row', f).forEach(r => { if (/面板端口|局域网/.test(r.textContent)) r.remove(); }); return f; })),
    h('div.card.col', h('h3', '诊断与更新'),
      h('div.small.muted', '遇到问题要找人帮忙时，点「打包诊断信息」生成一个 zip（版本、去掉密钥的设置、近期日志；不含密钥、聊天记录和记忆），发给对方。'),
      h('div', h('button', { onclick: async () => {
        const r = await fetch('/api/diagnostics', { method: 'POST' });
        const blob = await r.blob();
        const a = h('a', { href: URL.createObjectURL(blob), download: 'diagnostics-' + new Date().toISOString().slice(0, 16).replace(/[-:T]/g, '') + '.zip' }); document.body.append(a); a.click(); a.remove();
      } }, icon('download'), '打包诊断信息')),
      h('div.small.muted.mt8', '新版本提醒：每天查一次 GitHub Releases，有新版本就在面板顶上提示；只提醒，不自动更新。'),
      h('div.row.wrap', h('button', { onclick: async ev => {
        const b = ev.currentTarget; b.disabled = true;
        try { const u = await GET('/api/update?refresh=1'); toast(u.error ? `没查到：${u.error}` : u.newer ? `有新版本 ${u.latest}` : `已是最新（${u.current}）`, u.error ? 'err' : ''); } catch (e) { /* 已提示 */ }
        b.disabled = false;
      } }, icon('refresh'), '现在检查'))),
    h('div.card', h('div.card-h', h('h3', '备份'), h('button.sm', { onclick: async () => { const r = await POST('/api/backup'); toast('已备份'); App.route(); } }, icon('download'), '现在备份')),
      h('div.small.muted.mb8', '每天自动备份一次人设、人物表、记忆和全部设置（整份数据库），在数据目录的 backups 文件夹里。'),
      h('div.list', bks.slice(0, 20).map(b => h('div.item', icon('history'), h('div.grow', b.name), h('span.small.faint', (b.size / 1024).toFixed(0) + ' KB'))),
        !bks.length && h('div.faint.small', '还没有备份')))));
}

async function setData(body) {
  const wk = h('input', { type: 'checkbox' });
  const file = h('input', { type: 'file', accept: '.zip' });
  const oldPath = h('input', { value: B().old_bot_guess || '', placeholder: '旧程序的 bot 文件夹' });
  add(body, h('div.grid2',
    h('div.card.form', h('h3', '导出（换电脑、留底）'),
      h('div.small.muted', '打包全部数据：设置、角色、人设（含历史版本）、人物表、记忆、聊天记录、备份。'),
      h('label.row', wk, '带上密钥（API Key、Discord 令牌、面板密码）'),
      h('div.small.faint', '不带密钥更安全；到新电脑导入后重新填一下 Key 和令牌即可。'),
      h('div', h('button.primary', { onclick: async () => {
        const r = await fetch('/api/export', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ with_secrets: wk.checked }) });
        const blob = await r.blob();
        const name = decodeURIComponent((r.headers.get('content-disposition') || '').split("''")[1] || 'export.zip');
        const a = h('a', { href: URL.createObjectURL(blob), download: name }); document.body.append(a); a.click(); a.remove();
      } }, icon('download'), '导出数据包'))),
    h('div.card.form', h('h3', '导入'),
      h('div.small.muted', '导入本程序导出的数据包，会整份替换当前数据（替换前自动备份一份）。'),
      file, h('div', h('button', { onclick: async () => {
        if (!file.files[0]) return toast('先选文件', 'err');
        if (!(await confirmBox('导入会替换当前全部数据（会先自动备份）。继续？', '导入', true))) return;
        const f = new FormData(); f.append('file', file.files[0]);
        const r = await POST('/api/import', f);
        toast('导入完成' + (r.manifest.with_secrets ? '' : '，请重新填 API Key 等密钥'));
        setTimeout(() => location.reload(), 800);
      } }, icon('upload'), '导入')))),
    !B().old_bot_guess ? null : h('div.card.form.mt16', h('h3', '从旧程序一键导入'),  // 只在检测到旧程序时才显示（和向导一致）
      h('div.small.muted', '读取旧程序 bot 文件夹里的 config.json（API Key、模型、冷却等）和人设文件（外壳、内核拆成模块）。旧程序的文件不会被改动。'),
      h('div.row', oldPath, h('button', { onclick: async () => {
        const r = await POST('/api/import_old', { path: oldPath.value.trim() });
        toast('已导入：' + r.done.join('、'));
        await reloadBoot();
      } }, '导入'))),
    h('div.card.mt16', h('h3.mb8', '数据位置'), h('code', B().data_dir), h('div.small.muted.mt8', '数据放在安装目录下的 data 文件夹里（为所有用户安装时放在用户目录的 PodMates 里），卸载时默认保留，重装不会丢。开机自启、备份、导出导入都跟着这个位置走。')));
}

async function setAbout(body) {
  const a = await GET('/api/about');
  const showText = (title, text) => modal(title, h('pre.about-text', text || '（找不到这份文件）'), { wide: true });
  add(body, h('div.card.col',
    h('div.row.gap12', h('img', { src: 'icon-192.png', width: 48 }), h('div', h('h3', '群聊机器人面板 · PodMates'), h('div.muted', '版本 ' + a.version))),
    h('div.about-grid',
      h('div.muted', '作者'), h('div', a.author),
      h('div.muted', '作者主页'), h('div', h('a', { href: a.author_home, target: '_blank' }, a.author_home)),
      h('div.muted', '项目地址'), h('div', h('a', { href: a.repo_url, target: '_blank' }, a.repo_url)),
      h('div.muted', '许可证'), h('div.row.wrap', h('span', a.license + ' 许可证'), h('button.sm', { onclick: () => showText('许可证', a.license_text) }, '查看全文')),
      h('div.muted', '第三方说明'), h('div', h('button.sm', { onclick: () => showText('第三方说明', a.third_party) }, '查看使用的第三方组件和它们的许可证'))),
    h('div.small', 'QQ 经 NapCat（OneBot v11），微信经 wxauto4（界面自动化），Discord 经官方机器人接口。NapCat 与 wxauto4 不随安装包分发，由你从官方渠道下载。'),
    h('div', h('button', { onclick: () => Wizard.open(true) }, icon('refresh'), '重新运行设置向导'))));
}
