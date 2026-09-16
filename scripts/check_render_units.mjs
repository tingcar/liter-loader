/**
 * 直接对 app/web/app.js 里的纯函数做断言，不依赖浏览器：
 *   - sourceRowHtml：可选渠道（Scopus / Web of Science）区块的按钮与接线
 *   - renderConfig：勾选区不把「需 key」当成不可用
 *   - testLLMConnection：测试连接不关弹窗、先存 key、失败给排查清单
 *
 * 用法：node scripts/check_render_units.mjs
 */

const fs = await import('node:fs');
const path = await import('node:path');

const js = fs.readFileSync(path.resolve('app/web/app.js'), 'utf8');

/*
 * 按「下一个顶层声明」切出函数源码。
 * 注意：不能用花括号配对来切——函数体里有模板字符串，字符串里的 {} 会让配对提前结束，
 * 把后半段代码误判为不在函数内（这正是本脚本曾经误报的原因）。
 */
function extractFunction(source, name) {
  const start = source.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`找不到函数 ${name}`);
  const rest = source.slice(start);
  const nextMatch = /\n(?=(?:async\s+)?function\s|const\s+\w+\s*=\s*\(|const\s+\w+\s*=\s*async)/.exec(rest);
  return nextMatch ? rest.slice(0, nextMatch.index + 1) : rest;
}

const escapeHtml = (value) =>
  String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');

const factory = new Function(
  'escapeHtml',
  `${extractFunction(js, 'sourceRowHtml')}\nreturn { sourceRowHtml };`
);
const { sourceRowHtml } = factory(escapeHtml);

const results = [];
function check(label, ok, detail = '') {
  results.push({ label, ok });
  console.log(`${ok ? 'PASS' : 'FAIL'} ${label}${ok || !detail ? '' : ` -> ${detail}`}`);
}

const scopusItem = {
  name: 'scopus',
  label: 'Scopus',
  requires_key: true,
  optional: true,
  has_key: false,
  key_label: 'Elsevier Scopus API key（X-ELS-APIKey）',
  how_to: '可选渠道：需要 Elsevier 开发者 API key。',
};

const offRow = sourceRowHtml(scopusItem, { sources: { scopus: false } });
check('未启用时显示「未启用」', offRow.includes('（未启用）'));
check('未启用时没有「停用该渠道」按钮', !offRow.includes('data-disable-source'));
check('未配置 key 时说明是可选、不填只跳过', offRow.includes('可选'), offRow.includes('未配置 key（可选'));

const onRow = sourceRowHtml({ ...scopusItem, has_key: true }, { sources: { scopus: true } });
check('已启用时显示「已启用」', onRow.includes('（已启用）'));
check('已配置 key 时显示已配置', onRow.includes('已配置 key'));
check('已启用时提供「停用该渠道」按钮', onRow.includes('data-disable-source="scopus"'));

// 关键：三个按钮的 data 属性必须各自独立，不能被别的属性带上
for (const [label, attr] of [
  ['保存 key', 'data-save-source="scopus"'],
  ['保存并启用', 'data-save-enable="scopus"'],
  ['停用该渠道', 'data-disable-source="scopus"'],
]) {
  const tagOf = (attr) => {
    const idx = onRow.indexOf(attr);
    if (idx < 0) return '';
    const open = onRow.lastIndexOf('<button', idx);
    const close = onRow.indexOf('</button>', idx);
    return onRow.slice(open, close + '</button>'.length);
  };
  const tag = tagOf(attr);
  const others = [
    'data-save-source=',
    'data-save-enable=',
    'data-disable-source=',
  ].filter((other) => other !== `${attr.split('=')[0]}=` && tag.includes(other));
  check(`「${label}」按钮只带自己的 data 属性`, others.length === 0, `按钮标签为 ${tag}`);
  check(`「${label}」按钮文案正确`, tag.includes(label), tag);
}

check('输入框 id 与渠道名对应', onRow.includes('id="config-key-scopus"') && onRow.includes('data-source="scopus"'));

// ---------- renderConfig 的渠道勾选提示 ----------
const renderConfigSrc = extractFunction(js, 'renderConfig');
check(
  '可选渠道在勾选区标注为“需 key（可选）”',
  renderConfigSrc.includes('需 key（可选）'),
  '勾选框旁的提示文案没提到“可选”'
);
check(
  '可选渠道缺失 key 时不置灰（仍可勾选）',
  renderConfigSrc.includes('needs-key') && !renderConfigSrc.includes('disabled'),
  '缺失 key 时不应把渠道置灰/禁用'
);
check(
  '可选渠道在 title 提示里说明“不配也能用其它渠道”',
  renderConfigSrc.includes('这是可选渠道'),
  '缺少可选说明'
);

// ---------- 测试连接不应关闭弹窗 ----------
const testSrc = extractFunction(js, 'testLLMConnection');
check(
  '「测试该模型连接」不会关闭弹窗',
  !/closeConfigModal\s*\(/.test(testSrc),
  'testLLMConnection 里出现了 closeConfigModal()，会在测试后把弹窗关掉'
);
check(
  '测试连接支持弹窗内联结果（fromModal）',
  testSrc.includes('fromModal') && testSrc.includes('config-test-result'),
  '测试结果应写在弹窗内的 #config-test-result'
);
check(
  '测试前会先保存输入框里的 key',
  testSrc.includes('/api/config/edit') && testSrc.includes('model_keys'),
  '输入框里新填的 key 必须先落盘，否则测的还是旧配置'
);
check(
  '测试失败时给出排查清单',
  testSrc.includes('base_url') && testSrc.includes('deepseek-flash'),
  '失败提示应包含 base_url / 模型名 / 网络等排查点'
);

const bindSrc = extractFunction(js, 'bindEvents');
check(
  '测试按钮以 fromModal 方式调用',
  bindSrc.includes('fromModal: true'),
  'btn-test-current-model 应按弹窗模式调用测试'
);

const html = fs.readFileSync(path.resolve('app/web/index.html'), 'utf8');
check('弹窗里有测试结果容器', html.includes('id="config-test-result"'));

const failed = results.filter((r) => !r.ok);
console.log(`\n${failed.length === 0 ? '全部通过' : `${failed.length} 项未通过`}`);
process.exitCode = failed.length === 0 ? 0 : 1;
