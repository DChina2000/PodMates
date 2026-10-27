/* 界面语言：默认中文；选 English 时，把页面上的中文文字按 i18n/en.json 换成英文。
 * 做法：整页文字节点和 placeholder / title 等属性，先整句匹配，再按「带 {0} 的模板」匹配，最后把里面认得的中文片段逐段替换；
 * 聊天消息、人设正文、代码块这类用户内容和模型提示词不翻译。词典里没有的文字原样保留中文（不会出错，只是没译）。 */
'use strict';

const I18N = {
  lang: 'zh', dict: new Map(), tpls: [], keys: [],
  SKIP: '.bubble,.msgs,.snip,pre,code,textarea,.code,.preview,[data-raw],script,style,.who',
  ATTRS: ['placeholder', 'title', 'alt', 'aria-label'],
  CJK: /[㐀-鿿　-〿＀-￯]/,

  async init(lang) {
    try { lang = lang || localStorage.getItem('mb_lang') || 'zh'; localStorage.setItem('mb_lang', lang); } catch (e) { lang = lang || 'zh'; }
    this.lang = lang;
    document.documentElement.lang = lang === 'en' ? 'en' : 'zh-CN';
    if (lang !== 'en') return;
    const en = await (await fetch('i18n/en.json')).json();
    for (const [zh, t] of Object.entries(en)) {
      if (zh.includes('{0}') && zh.replace(/\{\d+\}/g, '').replace(/[\s：:，,。.、/（）()「」"'“”-]/g, '').length >= 2) this.tpls.push({ re: new RegExp('^' + zh.replace(/[.*+?^$()|[\]\\]/g, '\\$&').replace(/\{(\d+)\}/g, '(.+?)') + '$'), en: t });
      this.dict.set(zh, t);
    }
    this.keys = [...this.dict.keys()].filter(k => k.length >= 3 && !k.includes('{0}')).sort((a, b) => b.length - a.length);
    this.tpls.sort((a, b) => b.re.source.length - a.re.source.length);
    document.title = this.t(document.title);
    this.walk(document.body);
    new MutationObserver(ms => {
      for (const m of ms) {
        if (m.type === 'characterData') this.node(m.target);
        else if (m.type === 'attributes') this.attr(m.target, m.attributeName);
        else m.addedNodes.forEach(n => this.walk(n));
      }
    }).observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: this.ATTRS });
  },

  t(s) {
    if (this.lang !== 'en' || !s || !this.CJK.test(s)) return s;
    const lead = s.match(/^\s*/)[0], tail = s.match(/\s*$/)[0], core = s.trim();
    if (!core) return s;
    let hit = this.dict.get(core);
    if (hit == null) {
      for (const { re, en } of this.tpls) {
        const m = core.match(re);
        if (m) { hit = en.replace(/\{(\d+)\}/g, (_, i) => this.t(m[+i + 1])); break; }
      }
    }
    if (hit == null) {  // 零碎拼起来的句子：把认得的中文片段逐段换成英文
      hit = core;
      for (const k of this.keys) if (hit.includes(k)) { const v = this.dict.get(k); hit = hit.split(k).join(/[:,;]$/.test(v) ? v + ' ' : v); }
    }
    if (hit !== core) hit = hit.replace(/[「“]/g, '“').replace(/[」”]/g, '”');  // 译文里残留的半边中文引号换成英文引号
    return lead + hit + tail;
  },

  skip(el) { return !el || !el.closest || el.closest(this.SKIP); },
  node(n) {
    if (n.nodeType !== 3 || this.skip(n.parentElement)) return;
    const v = this.t(n.nodeValue);
    if (v !== n.nodeValue) n.nodeValue = v;
  },
  attr(el, a) {
    if (this.skip(el)) return;
    const v = el.getAttribute(a);
    if (v) { const t = this.t(v); if (t !== v) el.setAttribute(a, t); }
  },
  walk(root) {
    if (root.nodeType === 3) return this.node(root);
    if (root.nodeType !== 1 || this.skip(root)) return;
    for (const a of this.ATTRS) if (root.hasAttribute && root.hasAttribute(a)) this.attr(root, a);
    for (const c of root.childNodes) this.walk(c);
  },
};
function tr(s) { return I18N.t(s); }
