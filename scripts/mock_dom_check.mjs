/**
 * 用最小 DOM 模拟器加载真实的 app/web/app.js，复现「配置弹窗无法关闭/保存」这类前端运行时错误。
 *
 * 用法：node scripts/mock_dom_check.mjs [baseUrl]
 * 退出码非 0 表示初始化或弹窗交互时抛了异常。
 */

const BASE = process.argv[2] || 'http://127.0.0.1:8765';
const fs = await import('node:fs');
const path = await import('node:path');

const WEB = path.resolve('app/web');
const html = fs.readFileSync(path.join(WEB, 'index.html'), 'utf8');
const js = fs.readFileSync(path.join(WEB, 'app.js'), 'utf8');
const css = fs.readFileSync(path.join(WEB, 'style.css'), 'utf8');

const errors = [];
const logs = [];

// ---------- 极简 DOM ----------
class El {
  constructor(tag = 'div', attrs = {}) {
    this.tagName = tag.toUpperCase();
    this.id = attrs.id || '';
    this.className = attrs.class || '';
    this.attrs = attrs;
    this._html = '';
    this.value = attrs.value !== undefined ? attrs.value : '';
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.style = {};
    this.dataset = {};
    this.listeners = {};
    this.textContent = '';
  }
  get classList() {
    return { add: () => {}, remove: () => {}, contains: () => false };
  }
  get innerHTML() { return this._html; }
  set innerHTML(v) {
    this._html = String(v);
    dynHtml.push(this._html);
    // 解析新插入元素里的 id，让后续 $(id) 能拿到
    for (const m of this._html.matchAll(/id="([^"]+)"/g)) {
      if (!registry.has(m[1])) registry.set(m[1], new El('div', { id: m[1] }));
    }
    // 记录 data-* 按钮，便于统计
    for (const m of this._html.matchAll(/data-save-source="([^"]+)"/g)) {
      if (!inserted.datasetButtons.includes(m[1])) inserted.datasetButtons.push(m[1]);
    }
  }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  removeEventListener() {}
  querySelectorAll(sel) { return query(sel, this._html); }
  querySelector(sel) { return query(sel, this._html)[0] || null; }
  insertAdjacentHTML(_pos, s) { this.innerHTML = this._html + String(s); }
  fire(type, ev = {}) {
    const list = this.listeners[type] || [];
    if (!list.length) return false;
    for (const fn of list) fn({ target: this, key: ev.key, preventDefault() {}, ...ev });
    return true;
  }
  focus() {}
  get scrollIntoView() { return () => {}; }
  get children() { return this._children || []; }
  appendChild(child) {
    this._children ||= [];
    this._children.push(child);
    return child;
  }
  remove() {}
  removeChild(child) { return child; }
}

const registry = new Map();
const inserted = { datasetButtons: [] };
const dynHtml = []; // 运行期通过 innerHTML 插入的内容，供 query 匹配
for (const m of html.matchAll(/id="([^"]+)"[^>]*/g)) {
  const tag = /<(\w+)[^>]*id="/.exec(html.slice(Math.max(0, m.index - 60), m.index + 40));
  registry.set(m[1], new El(tag ? tag[1] : 'div', { id: m[1] }));
}

function query(sel, scopeHtml) {
  // 支持 .class / #id / [data-x] / [data-x="y"] 的简单选择器；同时搜索静态 HTML 与运行期插入的内容
  const source = scopeHtml || html + '\n' + dynHtml.join('\n');
  const out = [];
  for (const m of source.matchAll(/<(\w+)([^>]*)>/g)) {
    const [, tag, rawAttrs] = m;
    const attrText = rawAttrs;
    const idMatch = /id="([^"]+)"/.exec(attrText);
    const classMatch = /class="([^"]+)"/.exec(attrText);
    const dataMatches = [...attrText.matchAll(/data-([\w-]+)(?:="([^"]*)")?/g)];
    let ok = true;
    if (sel.startsWith('.')) {
      ok = Boolean(classMatch && classMatch[1].split(/\s+/).includes(sel.slice(1)));
    } else if (sel.startsWith('#')) {
      ok = Boolean(idMatch && idMatch[1] === sel.slice(1));
    } else if (sel.startsWith('[')) {
      const inner = sel.slice(1, -1);
      const [name, value] = inner.split('=');
      const key = name.replace(/^data-/, '').replace(/^\[|\]$/g, '');
      ok = dataMatches.some((d) => d[1] === key && (value === undefined || d[2] === value.replace(/"/g, '')));
    } else {
      ok = tag.toLowerCase() === sel.toLowerCase();
    }
    if (!ok) continue;
    const key = idMatch ? idMatch[1] : `${tag}-${out.length}`;
    if (!registry.has(key)) registry.set(key, new El(tag, { id: idMatch?.[1] || '', class: classMatch?.[1] || '' }));
    const el = registry.get(key);
    if (dataMatches.length) {
      for (const d of dataMatches) el.dataset[d[1].replace(/-(\w)/g, (_, c) => c.toUpperCase())] = d[2] ?? '';
      if (!inserted.datasetButtons.includes(el.dataset.saveSource) && el.dataset.saveSource) {
        inserted.datasetButtons.push(el.dataset.saveSource);
      }
    }
    if (!out.includes(el)) out.push(el);
  }
  return out;
}

const body = new El('body', {});
const document = {
  body,
  getElementById: (id) => {
    if (!registry.has(id)) return null;
    return registry.get(id);
  },
  querySelectorAll: (sel) => query(sel),
  querySelector: (sel) => query(sel)[0] || null,
  addEventListener: (type, fn) => {
    (document._listeners[type] ||= []).push(fn);
  },
  _listeners: {},
  createElement: (tag) => new El(tag),
};

const calls = [];
async function fetchStub(url, init = {}) {
  calls.push({ url, method: init.method || 'GET', body: init.body || null });
  const full = url.startsWith('http') ? url : BASE + url;
  const response = await fetch(full, {
    method: init.method || 'GET',
    headers: init.headers || {},
    body: init.body,
  });
  const text = await response.text();
  return {
    ok: response.ok,
    status: response.status,
    text: async () => text,
    json: async () => JSON.parse(text),
  };
}

process.on('unhandledRejection', (err) => {
  errors.push(`unhandledRejection: ${err && err.stack ? err.stack : err}`);
});

// ---------- 装载 app.js ----------
const consoleShim = { log: (...a) => logs.push(a.join(' ')), error: (...a) => errors.push(a.join(' ')), warn: () => {}, info: () => {} };
try {
  // 用同目录下的文件内容执行（不开沙箱，靠显式参数隔离）
  const fn = new Function('document', 'fetch', 'console', 'alert', 'setTimeout', 'clearTimeout', 'encodeURIComponent', 'JSON',
    js + '\n//# sourceURL=app.js');
  fn(document, fetchStub, consoleShim, (msg) => logs.push(`alert: ${msg}`), setTimeout, clearTimeout, encodeURIComponent, JSON);
} catch (err) {
  errors.push(`初始化 app.js 抛错：${err.stack || err}`);
}

// 等待 loadConfig/loadLLMModels 的 fetch 落地
await new Promise((r) => setTimeout(r, 2500));

const results = [];
function check(label, ok, detail = '') {
  results.push({ label, ok, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'} ${label}${ok || !detail ? '' : ` -> ${detail}`}`);
}

check('app.js 初始化无异常', errors.length === 0, errors.join(' | '));
check('/api/config 已请求', calls.some((c) => c.url.includes('/api/config')), JSON.stringify(calls.map((c) => c.url)));
check('配置面板渲染成功（config-body 有内容）', (document.getElementById('config-body')?.innerHTML || '').length > 200);
check('渠道勾选框渲染成功', (document.getElementById('source-toggles')?.innerHTML || '').includes('source-toggle'));

// 这是「弹窗盖住页面、点不动」的根因：类选择器的 display 覆盖了 [hidden]
check('CSS 显式处理了 [hidden]', /\[hidden\]\s*\{[^}]*display:\s*none\s*!important/.test(css), 'style.css 缺少 [hidden] { display:none !important }');
check('弹窗初始带 hidden 属性', /id="config-modal"[^>]*hidden/.test(html) || /hidden[^>]*id="config-modal"/.test(html));
check('配置弹窗里不再有邮箱区块', !html.includes('config-email') && !html.includes('保存邮箱'));
check('配置弹窗里不再有 PubMed 密钥区块', !html.includes('config-key-pubmed') && !js.includes('config-key-pubmed'));

// ---------- 交互：打开弹窗 ----------
const openBtn = document.getElementById('btn-llm-config');
check('找到「LLM配置」按钮', Boolean(openBtn));
check('按钮上挂着 click 监听', Boolean(openBtn && (openBtn.listeners.click || []).length));
if (openBtn) openBtn.fire('click');
check('点击后弹窗显示', document.getElementById('config-modal')?.hidden === false);
check('弹窗内容已渲染模型行', (document.getElementById('config-model-rows')?.innerHTML || '').includes('key'));
check('弹窗渲染了渠道密钥行', inserted.datasetButtons.length > 0, `data-save-source=${JSON.stringify(inserted.datasetButtons)}`);
check(
  'Scopus/WoS 均按可选渠道渲染',
  inserted.datasetButtons.includes('scopus') && inserted.datasetButtons.includes('wos'),
  JSON.stringify(inserted.datasetButtons)
);
check('「保存并启用」按钮已渲染', (document.getElementById('config-source-rows')?.innerHTML || '').includes('data-save-enable'));

// ---------- 交互：关闭 ----------
const closeBtn = document.getElementById('btn-config-close');
check('找到关闭按钮', Boolean(closeBtn));
if (closeBtn) closeBtn.fire('click');
check('点击关闭后 hidden=true', document.getElementById('config-modal')?.hidden === true);

const doneBtn = document.getElementById('btn-config-done');
if (openBtn) openBtn.fire('click');
check('找到底部「完成并关闭」按钮', Boolean(doneBtn));
if (doneBtn) doneBtn.fire('click');
check('「完成并关闭」可关闭弹窗', document.getElementById('config-modal')?.hidden === true);

// ---------- 交互：保存模型 key ----------
if (openBtn) openBtn.fire('click');
const modelSelect = document.getElementById('config-model-select');
const modelKey = document.getElementById('config-model-key');
if (modelSelect) modelSelect.value = 'deepseek-flash';
if (modelKey) modelKey.value = 'sk-mock-not-a-real-key';
calls.length = 0;
const saveBtn = document.getElementById('btn-save-model-key');
check('找到保存按钮', Boolean(saveBtn));
if (saveBtn) saveBtn.fire('click');
await new Promise((r) => setTimeout(r, 1200));
const editCall = calls.find((c) => c.url.includes('/api/config/edit'));
check('点击保存后发出了 /api/config/edit 请求', Boolean(editCall), JSON.stringify(calls.map((c) => c.url)));
check('请求体包含 model_keys', Boolean(editCall && editCall.body && editCall.body.includes('model_keys')), editCall?.body || '');
check('保存过程中无未捕获异常', errors.length === 0, errors.join(' | '));
if (errors.length) {
  console.log('\n[提示] 出现未捕获异常，后续交互结果可能不可靠；请先看上面的异常栈。');
}

// ---------- 交互：保存渠道 key（Scopus 可选渠道 + 保存并启用） ----------
if (openBtn) openBtn.fire('click');
// 注意：抽出按钮所在的完整标签，避免把同一行里另一个按钮的 data 属性误认成它自己的
function buttonByDataAttr(sourceHtml, attr) {
  const html = sourceHtml + '\n' + dynHtml.join('\n');
  for (const m of html.matchAll(/<button\b[^>]*data-[\w-]+="[^"]*"[^>]*>/g)) {
    if (m[0].includes(attr)) {
      const idMatch = /id="([^"]+)"/.exec(m[0]);
      const key = idMatch ? idMatch[1] : `dyn-btn-${attr}-${m.index}`;
      if (!registry.has(key)) registry.set(key, new El('button', { id: idMatch?.[1] || '' }));
      return registry.get(key);
    }
  }
  return null;
}

const scopusInput = document.getElementById('config-key-scopus') || new El('input', { id: 'config-key-scopus' });
if (!registry.has('config-key-scopus')) registry.set('config-key-scopus', scopusInput);
scopusInput.value = '--cleanup--';
// 渠道区块是事件委托：对容器派发一次 click，target 是「保存并启用」按钮
const scopusEnableButton = new El('button', {});
scopusEnableButton.dataset = { saveEnable: 'scopus' };
const rowsContainer = document.getElementById('config-source-rows');
check('渠道容器上挂着委托点击监听', Boolean(rowsContainer && (rowsContainer.listeners.click || []).length));
calls.length = 0;
if (rowsContainer) rowsContainer.fire('click', { target: scopusEnableButton });
await new Promise((r) => setTimeout(r, 1200));
const enableCall = calls.find((c) => c.url.includes('/api/config/edit'));
check('「保存并启用」发出 /api/config/edit', Boolean(enableCall), JSON.stringify(calls.map((c) => c.url)));
check(
  '请求体同时包含 source_keys 与 sources 开关',
  Boolean(enableCall && enableCall.body && enableCall.body.includes('source_keys') && enableCall.body.includes('"sources"')),
  enableCall?.body || ''
);
// 弹窗内提示区（这里必须能看到保存结果，比 toast 更关键）
check(
  '保存结果显示在弹窗提示区',
  (document.getElementById('config-edit-message')?.innerHTML || '').includes('已保存'),
  (document.getElementById('config-edit-message')?.innerHTML || '(空)').slice(0, 120)
);

// 纯文本按钮也能通过委托走到正确的动作（dataset 键名映射）
const invalidClick = handleSourceRowCheck();
check('未知 data 属性不会误触发保存', invalidClick === false, 'delegation 对未知属性应返回 false');

// ---------- 「测试该模型连接」按钮：必须存在且已绑定（不允许自动关闭弹窗） ----------
check('找到「测试该模型连接」按钮', Boolean(document.getElementById('btn-test-current-model')));
check(
  '测试按钮已绑定点击事件',
  Boolean((document.getElementById('btn-test-current-model')?.listeners.click || []).length)
);
check('弹窗里存在测试结果容器', Boolean(document.getElementById('config-test-result')));
check(
  '测试连接不会自动关闭弹窗（源码不含 closeConfigModal 调用）',
  !/closeConfigModal\s*\(/.test(extract(js, 'testLLMConnection')),
  'testLLMConnection 里不应调用 closeConfigModal'
);
function extract(source, name) {
  const start = source.indexOf(`function ${name}(`);
  if (start < 0) return '';
  let depth = 0;
  for (let i = source.indexOf('{', start); i < source.length; i += 1) {
    if (source[i] === '{') depth += 1;
    else if (source[i] === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  return source.slice(start);
}

function handleSourceRowCheck() {
  let handled = null;
  for (const list of Object.values(document._listeners ?? {})) void list;
  const container = document.getElementById('config-source-rows');
  const before = calls.length;
  if (container) container.fire('click', { target: { dataset: { unknown: 'x' } } });
  handled = calls.length > before;
  return handled;
}
check('保存流程无未捕获异常', errors.length === 0, errors.join(' | '));

// ---------- 交互：按 Escape 关闭 ----------
if (openBtn) openBtn.fire('click');
let escHandler = null;
for (const list of Object.values(document._listeners)) {
  for (const fn of list) if (fn.length >= 1) escHandler ||= fn;
}
if (escHandler) escHandler({ key: 'Escape' });
check('Escape 可关闭弹窗', document.getElementById('config-modal')?.hidden === true);

console.log('\n--- 交互期间调用过的接口 ---');
for (const c of calls) console.log(`  ${c.method} ${c.url}`);

const failed = results.filter((r) => !r.ok);
console.log(`\n${failed.length === 0 ? '全部通过' : `${failed.length} 项未通过`}`);
if (errors.length) {
  console.log('\n--- 捕获到的异常 ---');
  for (const e of errors) console.log(e);
}

// 该脚本会真的调用 /api/config/edit，这里把渠道开关恢复原状
try {
  await fetch(`${BASE}/api/config/edit`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sources: { scopus: false, wos: false }, source_keys: { scopus: '', wos: '' } }),
  });
  console.log('\n已把 scopus/wos 渠道恢复为：关闭且无 key');
} catch (err) {
  console.log(`\n恢复渠道失败（请手工检查 config.json）：${err}`);
}

// 注意：不调用 process.exit()（Node 在 Windows 上带着未关闭的 keep-alive socket 退出会崩），
// 用 exitCode 让进程自然结束。
process.exitCode = failed.length === 0 ? 0 : 1;
