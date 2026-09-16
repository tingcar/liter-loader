/* 文献下载器网页版前端逻辑（原生 JS，无构建步骤）。 */

const state = {
  config: null,
  run: null,
  candidates: [],
  filteredPreview: [],
  jobId: null,
  pollTimer: null,
  scopeOptions: [],
  llm: { models: [], ready: false, enabled: false, autoExpand: false, defaultModel: "", providers: [] },
  expanded: null, // 最近一次 LLM 扩展结果（确认后用于检索）
  filter: { priority: "", oa: "", text: "" },
};

const STATUS_CLASS = {
  success_pdf: "ok",
  success_html: "warn",
  success_xml: "warn",
  inaccessible: "bad",
  broken_link: "bad",
  failed: "bad",
  metadata_only: "neutral",
  rate_limited: "warn",
  excluded: "neutral",
  not_selected: "neutral",
  pending: "neutral",
};

const STATUS_ZH = {
  pending: "待下载",
  not_selected: "未勾选",
  success_pdf: "已下载 PDF",
  success_html: "已保存网页全文",
  success_xml: "已保存 XML 全文",
  metadata_only: "只找到题录",
  inaccessible: "平台限制直连下载",
  broken_link: "链接失效",
  rate_limited: "被限流",
  excluded: "已排除",
  failed: "尝试失败",
};

const PRIORITY_ZH = { high: "高", medium: "中", low: "低" };

const $ = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value === null || value === undefined ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch (error) {
    payload = { detail: text };
  }
  if (!response.ok) {
    const detail = (payload && (payload.detail || payload.message)) || `HTTP ${response.status}`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return payload;
}

/* ---------------- 配置面板 ---------------- */

function renderConfig(config) {
  state.config = config;
  const search = config.search || {};
  const download = config.download || {};
  const journals = config.journals || [];

  const journalRows = journals
    .map((entry) => {
      const metrics = [entry.impact_factor ? `IF ${entry.impact_factor}` : "", entry.jcr_quartile, entry.indexing]
        .filter(Boolean)
        .join(" · ");
      const issn = (entry.issn || []).join(", ");
      return `<div class="j"><span>${escapeHtml(entry.name)}${issn ? ` <code>${escapeHtml(issn)}</code>` : ""}</span>
              <span>${escapeHtml(metrics || "指标待核验")}</span></div>`;
    })
    .join("");

  const scopeLabel = (search.scope_options || []).find((item) => item.key === search.scope);
  const llm = config.llm || {};
  const llmModels = llm.models || [];
  const llmRows = llmModels
    .map(
      (model) =>
        `<div class="j"><span>${escapeHtml(model.label || model.name)} <code>${escapeHtml(model.model || model.name)}</code></span>
         <span>${model.has_api_key ? "已配置 key" : "缺少 key"} · ${escapeHtml(model.provider)}</span></div>`
    )
    .join("");
  const providerRows = (llm.providers || [])
    .map(
      (provider) =>
        `<div class="j"><span>${escapeHtml(provider.name)} <span class="muted">${escapeHtml(provider.type)}</span></span>
         <span class="muted">${escapeHtml(provider.base_url || "（未设置 base_url）")}${provider.has_api_key ? " · 有 key" : ""}</span></div>`
    )
    .join("");

  $("config-body").innerHTML = `
    <div class="config-grid">
      <div class="config-item">
        <b>配置文件</b>
        <div class="muted">${escapeHtml(config.path)}</div>
        <div class="muted">全文输出根目录：${escapeHtml(config.output_root)}</div>
      </div>
      <div class="config-item">
        <b>检索与下载参数</b>
        <div class="muted">默认范围：${escapeHtml(scopeLabel ? scopeLabel.label : search.scope || "")}</div>
        <div class="muted">默认年份 ${search.year_from}–${search.year_to}，每源最多 ${search.max_results_per_source} 条</div>
        <div class="muted">下载：超时 ${download.timeout_seconds}s，每条最多 ${download.max_attempts_per_record} 次尝试</div>
      </div>
      <div class="config-item">
        <b>期刊白名单（${journals.length} 种）</b>
        <div class="muted">只有这里的期刊会被检索、展示和下载</div>
        <div class="journal-list">${journalRows || '<div class="muted">白名单为空，请编辑 config.json 的 journals 字段</div>'}</div>
      </div>
      <div class="config-item">
        <b>AI 关键词扩展（${llm.enabled ? "已启用" : "已关闭"}）</b>
        <div class="muted">默认模型：${escapeHtml(llm.default_model || "（未设置）")} · 自动扩展：${llm.auto_expand ? "开" : "关"}</div>
        <div class="journal-list">${llmRows || '<div class="muted">llm.models 为空，请编辑 config.json</div>'}</div>
        <div class="muted">提供商：</div>
        <div class="journal-list">${providerRows || '<div class="muted">llm.providers 为空</div>'}</div>
        <div class="muted">api_key 保存在 config.json（或 ${escapeHtml("DEEPSEEK_API_KEY 环境变量")}），只在后端使用。</div>
      </div>
    </div>
    ${buildWarnings(config.warnings || [])}
    ${buildFilteredPreview(state.filteredPreview)}
  `;

  renderScopeOptions(search);
  renderLLMConfig(llm);

  $("input-year-from").value = search.year_from ?? "";
  $("input-year-to").value = search.year_to ?? "";
  $("input-max").value = search.max_results_per_source ?? 100;
  $("input-oa-only").checked = Boolean(search.prefer_oa_only);

  const labels = { openalex: "OpenAlex", crossref: "Crossref", europepmc: "Europe PMC", pubmed: "PubMed", scopus: "Scopus", wos: "Web of Science" };
  const catalog = search.source_catalog || [];
  const byName = new Map(catalog.map((item) => [item.name, item]));
  const order = catalog.length ? catalog.map((item) => item.name) : Object.keys(labels);
  $("source-toggles").innerHTML = order
    .map((name) => {
      const meta = byName.get(name) || {};
      const checked = search.sources && search.sources[name] ? "checked" : "";
      const needsKey = Boolean(meta.requires_key) && !meta.has_key;
      const title = [
        meta.note || "",
        meta.how_to || "",
        meta.optional ? "这是可选渠道：不配 key 也能用其它渠道。" : "",
        needsKey ? "尚未配置 API key，勾选后检索会跳过并给出提示。" : "",
      ]
        .filter(Boolean)
        .join(" ");
      const hint = needsKey ? ' <span class="keyhint">需 key（可选）</span>' : "";
      const titleAttr = title ? ` title="${escapeHtml(title)}"` : "";
      return `<label${titleAttr} class="${needsKey ? "needs-key" : ""}"><input type="checkbox" class="source-toggle" value="${name}" ${checked}> ${labels[name] || escapeHtml(meta.label || name)}${hint}</label>`;
    })
    .join("");
  $("source-toggles").dataset.catalog = JSON.stringify(catalog);
}

/* 检索范围下拉：四个选项来自后端，保证与各数据源字段语法一致 */
function renderScopeOptions(search) {
  const options = search.scope_options || [];
  state.scopeOptions = options;
  const selected = search.scope || "title_abstract";
  $("input-scope").innerHTML = options
    .map(
      (item) =>
        `<option value="${escapeHtml(item.key)}" ${item.key === selected ? "selected" : ""}>${escapeHtml(item.label)} · ${escapeHtml(
          (item.fields || []).join("+")
        )}</option>`
    )
    .join("");
  updateScopeHint();
}

function currentScope() {
  return state.scopeOptions.find((item) => item.key === $("input-scope").value) || null;
}

/* ---------------- LLM 关键词扩展 ---------------- */

function renderLLMConfig(llm) {
  const models = llm.models || [];
  state.llm = {
    models,
    providers: llm.providers || [],
    ready: models.some((model) => model.has_api_key),
    enabled: Boolean(llm.enabled),
    autoExpand: Boolean(llm.auto_expand),
    defaultModel: llm.default_model || "",
  };

  const selected = state.llm.defaultModel || (models[0] ? models[0].name : "");
  $("llm-model").innerHTML = models.length
    ? models
        .map((model) => {
          const suffix = model.has_api_key ? "" : "（未配置 key）";
          return `<option value="${escapeHtml(model.name)}" ${model.name === selected ? "selected" : ""}>${escapeHtml(
            model.label || model.name
          )}${suffix}</option>`;
        })
        .join("")
    : '<option value="">（config.json 里没有配置 LLM 模型）</option>';
  $("input-auto-expand").checked = state.llm.autoExpand;

  if (!state.llm.enabled) {
    $("llm-status").textContent = "已在 config.json 里关闭（llm.enabled = false）";
    $("btn-expand").disabled = true;
  } else if (!models.length) {
    $("llm-status").textContent = "llm.models 为空，请先编辑 config.json";
    $("btn-expand").disabled = true;
  } else if (!state.llm.ready) {
    $("llm-status").textContent = "还没配置 api_key：可在 config.json 的 llm.models[].api_key 填写，或设置 DEEPSEEK_API_KEY 环境变量后点「重载配置」";
    $("btn-expand").disabled = false;
  } else {
    $("llm-status").textContent = `就绪 · 当前模型 ${selected}`;
    $("btn-expand").disabled = false;
  }
}

function keywordGroupsFromInput() {
  return $("input-keywords")
    .value.split(/[;；]/)
    .map((group) => group.trim())
    .filter(Boolean)
    .map((group) => group.split(/[|/]/).map((term) => term.trim()).filter(Boolean));
}

async function expandKeywords() {
  const keywords = $("input-keywords").value.trim();
  if (!keywords) {
    $("search-status").textContent = "请先填写关键词，再让 AI 扩展。";
    return;
  }
  $("btn-expand").disabled = true;
  $("btn-search").disabled = true;
  $("expand-result").innerHTML = '<div class="muted">正在调用模型生成关键词，通常需要几秒…</div>';
  try {
    const payload = await api("/api/expand-keywords", {
      method: "POST",
      body: JSON.stringify({
        keywords,
        model: $("llm-model").value || null,
        scope: $("input-scope").value || null,
      }),
    });
    state.expanded = payload;
    renderExpandResult(payload);
    $("search-status").textContent = `AI 扩展完成：新增 ${payload.llm_term_count} 个关键词（模型 ${payload.model}，用时 ${payload.elapsed_seconds}s），勾选后点「开始检索」。`;
  } catch (error) {
    state.expanded = null;
    $("expand-result").innerHTML = `<div class="warnbox"><b>关键词扩展失败</b><div>${escapeHtml(
      error.message
    )}</div><div class="muted">可以继续用原来的关键词检索，不影响其他功能。</div></div>`;
    $("search-status").textContent = `关键词扩展失败：${error.message}`;
  } finally {
    $("btn-expand").disabled = false;
    $("btn-search").disabled = false;
    updateScopeHint();
  }
}

function renderExpandResult(payload) {
  const groups = payload.groups || [];
  const cards = groups
    .map((group) => {
      const userChips = group.user_terms
        .map((term) => `<span class="chip chip-user" title="你输入的关键词">${escapeHtml(term)}</span>`)
        .join("");
      const llmChips = group.llm_terms.length
        ? group.llm_terms
            .map(
              (term) =>
                `<label class="chip chip-llm"><input type="checkbox" class="llm-term" data-group="${group.index}" value="${escapeHtml(
                  term
                )}" checked> ${escapeHtml(term)}</label>`
            )
            .join("")
        : '<span class="muted">模型没有给出新的同义词</span>';
      return `<div class="exp-group">
        <div class="exp-head">概念块 ${group.index + 1}${group.reason ? ` · ${escapeHtml(group.reason)}` : ""}
          <span class="muted">（保留 ${group.user_terms.length} 个原关键词，可再补充 ${group.llm_terms.length} 个）</span></div>
        <div class="chips">${userChips}${llmChips}</div>
      </div>`;
    })
    .join("");

  const warnings = (payload.warnings || []).length
    ? `<div class="warnbox"><ul>${payload.warnings.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div>`
    : "";

  $("expand-result").innerHTML = `
    <div class="tipbox">
      <b>模型：</b>${escapeHtml(payload.model_label || payload.model)} ·
      <b>用时：</b>${payload.elapsed_seconds}s ·
      <b>新增词：</b>${payload.llm_term_count} ·
      <b>参考范围：</b>${escapeHtml(payload.scope_label || "")} ·
      <b>参考白名单期刊：</b>${(payload.journals_used || []).length} 种
      <div class="muted">浅绿色是你输入的关键词（不可取消），白底方块是模型新增的词，默认全选；取消勾选即不使用。</div>
    </div>
    ${warnings}
    ${cards}
    <div class="row actions">
      <button class="primary" id="btn-confirm-search">用这些关键词开始检索</button>
      <button class="ghost" id="btn-expand-reset">清除扩展结果</button>
    </div>
  `;
  $("btn-confirm-search").addEventListener("click", () => doSearch({ useExpanded: true }));
  $("btn-expand-reset").addEventListener("click", () => {
    state.expanded = null;
    $("expand-result").innerHTML = "";
  });
}

function collectKeywordGroups() {
  const base = keywordGroupsFromInput();
  if (!state.expanded || !(state.expanded.groups || []).length) {
    return null;
  }
  const checked = new Map();
  document.querySelectorAll(".llm-term").forEach((box) => {
    const index = Number(box.dataset.group);
    if (!checked.has(index)) checked.set(index, []);
    if (box.checked) checked.get(index).push(box.value);
  });
  const groups = [];
  state.expanded.groups.forEach((group, position) => {
    const index = Number.isFinite(group.index) ? group.index : position;
    const terms = group.user_terms.map((term) => ({ term, source: "user" }));
    const selected = checked.has(index) ? checked.get(index) : group.llm_terms;
    selected.forEach((term) => terms.push({ term, source: "llm" }));
    groups.push({ terms });
  });
  // 用户可能在扩展后又改了输入框：把新增的概念块补进来
  for (let index = groups.length; index < base.length; index += 1) {
    groups.push({ terms: base[index].map((term) => ({ term, source: "user" })) });
  }
  return groups;
}

function updateScopeHint() {
  const scope = currentScope();
  if (scope) {
    const crossrefNote = scope.field_scoped_by_source && scope.field_scoped_by_source.crossref === false
      ? "　注意：Crossref 不支持限定字段，会按相关度返回，界面上会标注实际命中字段。"
      : "";
    $("scope-hint").textContent = `${scope.description}。仅影响「关键词匹配哪些字段」，期刊白名单始终生效。${crossrefNote}`;
  } else {
    $("scope-hint").textContent = "";
  }
}

function buildWarnings(warnings) {
  if (!warnings || !warnings.length) return "";
  return `<div class="warnbox"><b>提示</b><ul>${warnings.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div>`;
}

function buildFilteredPreview(preview) {
  if (!preview || !preview.length) return "";
  const items = preview
    .slice(0, 8)
    .map((item) => `<li>${escapeHtml(item.journal)}：${item.count} 条（示例：${escapeHtml(item.example)}）</li>`)
    .join("");
  return `<div class="warnbox"><b>白名单外被过滤的期刊</b><ul>${items}</ul>
    如果其中有你要的期刊，把它加进 <code>config.json</code> 的 <code>journals</code>，然后点「重载配置」重新检索。</div>`;
}

/* 展示每个数据源实际使用的检索式，方便复现和排查 */
function buildSourceQueries(stats) {
  const queries = stats && stats.source_queries;
  if (!queries || !Object.keys(queries).length) return "";
  const labels = { openalex: "OpenAlex", crossref: "Crossref", europepmc: "Europe PMC", pubmed: "PubMed" };
  const rows = Object.keys(queries)
    .map((name) => `<li><b>${escapeHtml(labels[name] || name)}</b>：<code>${escapeHtml(queries[name])}</code></li>`)
    .join("");
  return `<div class="tipbox"><b>各数据源实际检索式（范围：${escapeHtml(stats.scope_label || "")}，字段：${escapeHtml(
    stats.scope_fields || ""
  )}）</b><ul>${rows}</ul></div>`;
}

async function loadConfig() {
  try {
    const payload = await api("/api/config");
    renderConfig(payload.config);
    $("search-status").textContent = "";
  } catch (error) {
    $("config-body").innerHTML = `<div class="warnbox"><b>读取 config.json 失败</b><div>${escapeHtml(error.message)}</div></div>`;
  }
}

/* ---------------- 检索 ---------------- */

function selectedSources() {
  const toggles = document.querySelectorAll(".source-toggle");
  if (!toggles.length) return null;
  const result = {};
  toggles.forEach((item) => {
    result[item.value] = item.checked;
  });
  return result;
}

async function doSearch(options = {}) {
  const keywords = $("input-keywords").value.trim();
  if (!keywords) {
    $("search-status").textContent = "请先填写关键词。";
    return;
  }
  // 勾选了「每次检索自动扩展」且还没有扩展结果时，先跑一次扩展再检索
  if (!options.useExpanded && $("input-auto-expand").checked && !state.expanded) {
    await expandKeywords();
    if (!state.expanded) {
      $("search-status").textContent += "（自动扩展失败，已改用原关键词检索）";
    }
  }
  const keywordGroups = options.useExpanded ? collectKeywordGroups() : state.expanded ? collectKeywordGroups() : null;
  $("btn-search").disabled = true;
  $("search-status").textContent = "正在检索多个数据源，请稍候…";
  try {
    const payload = await api("/api/search", {
      method: "POST",
      body: JSON.stringify({
        keywords,
        keyword_groups: keywordGroups,
        llm_model: $("llm-model").value || null,
        year_from: Number($("input-year-from").value) || null,
        year_to: Number($("input-year-to").value) || null,
        sources: selectedSources(),
        max_results_per_source: Number($("input-max").value) || null,
        scope: $("input-scope").value || null,
        oa_only: $("input-oa-only").checked,
      }),
    });
    state.run = payload.run;
    state.candidates = payload.candidates || [];
    state.filteredPreview = payload.filtered_preview || [];
    state.jobId = null;
    $("panel-results").hidden = false;
    $("panel-download").hidden = true;
    renderResults();
    loadConfig();
    const scopeInfo = payload.scope || {};
    const stats = payload.run.stats || {};
    const rawCounts = stats.raw || 0;
    const llmNote = stats.llm_used
      ? ` 关键词：用户 ${(stats.user_terms || []).length} 个 + AI 扩展 ${(stats.llm_terms || []).length} 个（模型 ${stats.llm_model || "-"}）。`
      : " 关键词：仅使用你输入的词。";
    $("search-status").textContent =
      `检索完成（范围：${scopeInfo.label || ""}）：白名单内 ${state.candidates.length} 条，白名单外过滤 ${stats.filtered_out || 0} 条，去重前 ${rawCounts} 条。` + llmNote;
    if (!state.candidates.length) {
      $("search-status").textContent += " 结果为空时建议放宽「检索范围」或让 AI 再扩一批同义词。";
    }
    $("panel-results").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    $("search-status").textContent = `检索失败：${error.message}`;
  } finally {
    $("btn-search").disabled = false;
  }
}

/* ---------------- 结果表 ---------------- */

function visibleCandidates() {
  const text = state.filter.text.trim().toLowerCase();
  return state.candidates.filter((item) => {
    if (state.filter.priority && item.priority !== state.filter.priority) return false;
    if (state.filter.oa === "oa" && !hasOa(item)) return false;
    if (state.filter.oa === "no-oa" && hasOa(item)) return false;
    if (text) {
      const haystack = `${item.title} ${item.journal} ${item.doi} ${item.authors}`.toLowerCase();
      if (!haystack.includes(text)) return false;
    }
    return true;
  });
}

function hasOa(item) {
  return Boolean(item.pdf_url_candidate) || ["open", "oa", "true", "yes"].includes(String(item.oa_status || "").toLowerCase());
}

function renderResults() {
  const rows = visibleCandidates();
  const body = rows
    .map((item) => {
      const index = state.candidates.indexOf(item) + 1;
      const status = item.download_status || "pending";
      const statusClass = STATUS_CLASS[status] || "neutral";
      const address = item.landing_page_url || item.article_url || item.pdf_url_candidate || (item.doi ? `https://doi.org/${item.doi}` : "");
      const doiLink = item.doi ? `https://doi.org/${item.doi}` : "";
      const classes = [];
      if (item.exclusion_reason_if_any) classes.push("excluded");
      if (String(status).startsWith("success")) classes.push("downloaded");
      const metrics = [item.impact_factor ? `IF ${item.impact_factor}` : "", item.jcr_quartile || "", item.metric_year || "", item.metric_source || ""]
        .filter(Boolean)
        .join(" / ");
      return `<tr class="${classes.join(" ")}" data-record-id="${escapeHtml(item.record_id)}">
        <td><input type="checkbox" class="row-check" data-record-id="${escapeHtml(item.record_id)}" ${item.selected ? "checked" : ""}></td>
        <td>${index}</td>
        <td class="titlecell">
          ${escapeHtml(item.title)}
          <div class="subline">${address ? `<a href="${escapeHtml(address)}" target="_blank" rel="noreferrer">${escapeHtml(address)}</a>` : "（无地址）"}</div>
          ${item.exclusion_reason_if_any ? `<div class="subline">已排除原因：${escapeHtml(item.exclusion_reason_if_any)}</div>` : ""}
          ${item.final_path ? `<div class="subline">文件：${escapeHtml(item.final_path.split(/[\\\\/]/).pop())}</div>` : ""}
        </td>
        <td>${escapeHtml(item.year || "")}</td>
        <td>${escapeHtml(item.journal || "")}${item.issn ? `<div class="subline">${escapeHtml(item.issn)}</div>` : ""}</td>
        <td>${escapeHtml(item.impact_factor || "待核验")}</td>
        <td>${escapeHtml(item.jcr_quartile || "待核验")}</td>
        <td>${escapeHtml(metrics || "待核验")}</td>
        <td>${doiLink ? `<a href="${escapeHtml(doiLink)}" target="_blank" rel="noreferrer">${escapeHtml(item.doi)}</a>` : ""}</td>
        <td>${escapeHtml(item.source_database || "")}</td>
        <td>${escapeHtml(item.matched_by || "")}</td>
        <td>${escapeHtml(item.keyword_field_hits || "—")}</td>
        <td><span class="badge ${escapeHtml(item.priority || "low")}">${escapeHtml(PRIORITY_ZH[item.priority] || "-")}</span></td>
        <td><span class="badge ${statusClass}">${escapeHtml(STATUS_ZH[status] || status)}</span></td>
      </tr>`;
    })
    .join("");

  $("candidates-body").innerHTML = rows.length ? body : `<tr><td colspan="14" class="muted">没有符合当前筛选条件的文献。</td></tr>`;

  const stats = (state.run && state.run.stats) || {};
  $("result-meta").textContent =
    `检索范围 ${stats.scope_label || ""}（${stats.scope_fields || ""}） · 白名单内 ${state.candidates.length} 条 · 白名单外过滤 ${stats.filtered_out || 0} 条 · 去重合并 ${stats.duplicates || 0} 条`;
  $("result-warnings").innerHTML =
    buildWarnings((state.run && state.run.warnings) || []) +
    buildSourceQueries(stats) +
    buildFilteredPreview(state.filteredPreview);
  updateSelectionUI();
  document.querySelectorAll(".row-check").forEach((box) => {
    box.addEventListener("change", onRowCheck);
  });
}

function updateSelectionUI() {
  const selected = state.candidates.filter((item) => item.selected).length;
  const total = state.candidates.length;
  $("btn-download").disabled = selected === 0;
  $("table-foot").textContent = `已选 ${selected} / 共 ${total} 条（当前筛选显示 ${visibleCandidates().length} 条）。只有勾选的文献会被下载。`;
  const allVisible = visibleCandidates();
  const allChecked = allVisible.length > 0 && allVisible.every((item) => item.selected);
  $("check-all").checked = allChecked;
}

function onRowCheck(event) {
  const id = event.target.dataset.recordId;
  const item = state.candidates.find((candidate) => candidate.record_id === id);
  if (item) item.selected = event.target.checked;
  updateSelectionUI();
}

function bulkSelect(predicate) {
  visibleCandidates().forEach((item) => {
    if (predicate(item)) item.selected = true;
  });
  renderResults();
}

/* ---------------- 下载 ---------------- */

async function startDownload() {
  if (!state.run) return;
  const ids = state.candidates.filter((item) => item.selected).map((item) => item.record_id);
  if (!ids.length) return;
  $("btn-download").disabled = true;
  $("panel-download").hidden = false;
  $("progress-text").textContent = "正在启动下载任务…";
  $("download-errors").innerHTML = "";
  try {
    const payload = await api(`/api/runs/${state.run.run_id}/download`, {
      method: "POST",
      body: JSON.stringify({ record_ids: ids }),
    });
    state.jobId = payload.job_id;
    pollDownload();
  } catch (error) {
    $("progress-text").textContent = `启动失败：${error.message}`;
    $("btn-download").disabled = false;
  }
}

function pollDownload() {
  if (state.pollTimer) clearTimeout(state.pollTimer);
  const tick = async () => {
    try {
      const query = state.jobId ? `?job_id=${state.jobId}` : "";
      const payload = await api(`/api/runs/${state.run.run_id}/download/status${query}`);
      renderDownload(payload);
      if (payload.state === "queued" || payload.state === "running") {
        state.pollTimer = setTimeout(tick, 1000);
      } else {
        state.candidates = payload.candidates || state.candidates;
        renderResults();
        $("btn-download").disabled = false;
      }
    } catch (error) {
      $("progress-text").textContent = `查询进度失败：${error.message}`;
      $("btn-download").disabled = false;
    }
  };
  tick();
}

function renderDownload(payload) {
  const total = payload.total || 0;
  const done = payload.processed || 0;
  const percent = total ? Math.round((done / total) * 100) : 0;
  $("progress-bar").style.width = `${percent}%`;
  const stateText = { queued: "排队中", running: "下载中", done: "已完成", failed: "异常结束", cancelled: "已取消" }[payload.state] || payload.state;
  $("progress-text").textContent =
    `${stateText}　${done}/${total}（${percent}%）　用时 ${payload.elapsed_seconds || 0}s` +
    (payload.current_title ? `　当前：${payload.current_title.slice(0, 60)}` : "");

  const summary = payload.summary || {};
  const cards = [
    ["候选", summary.total],
    ["勾选", summary.selected],
    ["PDF", summary.success_pdf],
    ["网页/XML", (summary.success_html || 0) + (summary.success_xml || 0)],
    ["需后续处理", summary.unresolved],
  ];
  $("download-stats").innerHTML = cards
    .map(([label, num]) => `<div class="stat"><div class="num">${num ?? 0}</div><div class="label">${label}</div></div>`)
    .join("");

  const links = Object.entries(payload.deliverables || {})
    .map(([name, path]) => `<a href="/api/runs/${state.run.run_id}/file/${encodeURIComponent(name)}" target="_blank">${escapeHtml(name)}<span class="muted">下载</span></a>`)
    .join("");
  $("deliverables").innerHTML = links
    ? `${links}<button class="ghost" id="btn-open-folder">打开 paperdown 运行目录</button>`
    : "";
  const openButton = $("btn-open-folder");
  if (openButton) {
    openButton.addEventListener("click", async () => {
      try {
        await api("/api/open-folder", { method: "POST", body: JSON.stringify({ run_id: state.run.run_id }) });
      } catch (error) {
        alert(`打开文件夹失败：${error.message}`);
      }
    });
  }

  const errors = payload.errors || [];
  $("download-errors").innerHTML = errors.length
    ? `<b>需要注意的条目</b><ul>${errors.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`
    : "";

  loadPaperFiles();
}

/* 下载完成后列出本次实际落盘的全文文件 */
async function loadPaperFiles() {
  if (!state.run) return;
  try {
    const payload = await api(`/api/runs/${state.run.run_id}/papers`);
    const files = payload.papers || [];
    if (!files.length) {
      $("download-files").innerHTML = '<div class="muted">本次没有下载到全文文件。</div>';
      return;
    }
    const rows = files
      .map(
        (file) =>
          `<li>${escapeHtml(file.name)} <span class="muted">(${Math.max(1, Math.round(file.size / 1024))} KB)</span></li>`
      )
      .join("");
    $("download-files").innerHTML = `<b>已下载全文（${files.length}）</b><div class="muted">目录：${escapeHtml(
      payload.directory
    )}</div><ul>${rows}</ul>`;
  } catch (error) {
    $("download-files").innerHTML = "";
  }
}

/* ---------------- 事件绑定 ---------------- */

function bindEvents() {
  $("btn-search").addEventListener("click", () => doSearch());
  $("input-keywords").addEventListener("keydown", (event) => {
    if (event.key === "Enter") doSearch();
  });
  $("btn-quick1").addEventListener("click", () => {
    $("input-keywords").value = "large language model|LLM|ChatGPT; environmental science|climate|sustainability";
  });
  $("btn-expand").addEventListener("click", expandKeywords);
  $("btn-llm-test").addEventListener("click", testLLMConnection);
  $("input-auto-expand").addEventListener("change", () => {
    state.llm.autoExpand = $("input-auto-expand").checked;
  });
  $("llm-model").addEventListener("change", () => {
    $("llm-status").textContent = `已选择模型 ${$("llm-model").value}（下次扩展生效）`;
  });
  $("btn-llm-config").addEventListener("click", openConfigModal);
  $("btn-config-close").addEventListener("click", closeConfigModal);
  $("config-modal").addEventListener("click", (event) => {
    if (event.target === $("config-modal")) closeConfigModal();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("config-modal").hidden) closeConfigModal();
  });
  $("btn-save-model-key").addEventListener("click", saveModelKey);
  $("btn-save-provider").addEventListener("click", saveProvider);
  $("btn-config-done").addEventListener("click", closeConfigModal);
  // 渠道区块用事件委托：innerHTML 重绘后按钮依然有效
  $("config-source-rows").addEventListener("click", (event) => {
    handleSourceRowClick(event.target);
  });
  // 弹窗里按回车也能触发对应保存（避免「点了没反应」的错觉）
  $("config-model-key").addEventListener("keydown", (event) => {
    if (event.key === "Enter") saveModelKey();
  });
  $("config-provider-key").addEventListener("keydown", (event) => {
    if (event.key === "Enter") saveProvider();
  });
  $("btn-test-current-model").addEventListener("click", () => testLLMConnection({ fromModal: true }));
  $("btn-download").addEventListener("click", startDownload);
  $("btn-select-all").addEventListener("click", () => bulkSelect(() => true));
  $("btn-select-oa").addEventListener("click", () => bulkSelect((item) => hasOa(item) && !item.exclusion_reason_if_any));
  $("btn-select-high").addEventListener("click", () => bulkSelect((item) => item.priority === "high" && !item.exclusion_reason_if_any));
  $("btn-clear").addEventListener("click", () => {
    state.candidates.forEach((item) => {
      item.selected = false;
    });
    renderResults();
  });
  $("check-all").addEventListener("change", (event) => {
    visibleCandidates().forEach((item) => {
      item.selected = event.target.checked;
    });
    renderResults();
  });
  $("filter-priority").addEventListener("change", (event) => {
    state.filter.priority = event.target.value;
    renderResults();
  });
  $("filter-oa").addEventListener("change", (event) => {
    state.filter.oa = event.target.value;
    renderResults();
  });
  $("filter-text").addEventListener("input", (event) => {
    state.filter.text = event.target.value;
    renderResults();
  });
  $("btn-reload-config").addEventListener("click", async () => {
    try {
      const payload = await api("/api/config/reload", { method: "POST" });
      renderConfig(payload.config);
      $("search-status").textContent = "配置已重载。";
    } catch (error) {
      alert(`重载失败：${error.message}`);
    }
  });
  $("btn-toggle-config").addEventListener("click", (event) => {
    const body = $("config-body");
    body.hidden = !body.hidden;
    event.target.textContent = body.hidden ? "展开" : "收起";
  });
  $("input-scope").addEventListener("change", updateScopeHint);
}

/* 测试模型连接。
   - 弹窗里点「测试该模型连接」时：先自动保存输入框里刚填的 key（否则测的还是旧配置），
     结果就地显示在弹窗里，**不关闭弹窗**（关闭与否由用户决定）。
   - 工具栏里点「测试模型连接」时：结果仍写到扩展结果区。 */
async function testLLMConnection(options = {}) {
  const fromModal = Boolean(options.fromModal);
  const modelName = options.model || $("llm-model").value || $("config-model-select").value || null;
  const key =
    options.saveKey !== undefined ? options.saveKey : fromModal ? maskValue("config-model-key") : undefined;
  const resultBox = fromModal ? $("config-test-result") : $("expand-result");
  const button = fromModal ? $("btn-test-current-model") : $("btn-llm-test");
  const original = button ? button.textContent : "";
  if (button) {
    button.disabled = true;
    button.textContent = "测试中…";
  }
  if (resultBox) {
    resultBox.innerHTML = '<div class="muted">正在连接模型，通常几秒内返回…（连接不上时会等到超时）</div>';
  }

  try {
    // 输入框里填了新 key → 先落盘，再测试，保证测的就是当前输入
    if (fromModal && key) {
      const saved = await api("/api/config/edit", {
        method: "POST",
        body: JSON.stringify({ model_keys: { [modelName]: key } }),
      });
      state.config = saved.config;
      renderConfig(saved.config);
      renderConfigModal();
      const input = $("config-model-key");
      if (input) input.value = "";
      await loadLLMModels();
      if (resultBox) resultBox.innerHTML = '<div class="muted">已保存新 key，正在测试连接…</div>';
    }

    const payload = await api("/api/llm/test", {
      method: "POST",
      body: JSON.stringify({ model: modelName }),
    });
    const html = `<div class="tipbox"><b>连接成功</b>
      <div class="muted">模型 ${escapeHtml(payload.model_label || payload.model)} ·
        提供商 ${escapeHtml(payload.provider)}</div>
      <div class="muted">端点 <code>${escapeHtml(payload.endpoint)}</code> · 用时 ${payload.elapsed_seconds}s</div>
      <div class="muted">模型回复预览：${escapeHtml(payload.reply_preview || "")}</div></div>`;
    if (resultBox) resultBox.innerHTML = html;
    if (fromModal) {
      $("llm-status").textContent = `连接成功 · ${payload.model}`;
      // 工具栏上的按钮也刷新一下，避免显示旧的“缺少 key”
      $("config-modal").hidden = false;
    } else {
      $("llm-status").textContent = `连接成功 · ${payload.model}`;
    }
    toast(`模型 ${payload.model} 连接成功`);
    await loadLLMModels();
  } catch (error) {
    if (resultBox) {
      resultBox.innerHTML = `<div class="warnbox"><b>连接失败</b><div>${escapeHtml(error.message)}</div>
        <div class="muted">请检查：① API key 是否已保存且有效；② base_url 是否正确（DeepSeek 官方为
        <code>https://api.deepseek.com</code>）；③ 模型名是否为 <code>deepseek-flash</code> 或
        <code>deepseek-v4-pro</code>；④ 网络/代理是否可访问该域名。</div></div>`;
    }
    $("llm-status").textContent = "连接失败";
    toast(`连接失败：${error.message}`, "error");
    await loadLLMModels();
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = original;
    }
  }
}

async function loadLLMModels() {
  try {
    const payload = await api("/api/llm/models");
    state.llm = {
      models: payload.models || [],
      providers: payload.providers || [],
      ready: Boolean(payload.ready),
      enabled: Boolean(payload.enabled),
      autoExpand: Boolean(payload.auto_expand),
      defaultModel: payload.default_model || "",
    };
    renderLLMConfig({
      models: payload.models,
      providers: payload.providers,
      enabled: payload.enabled,
      auto_expand: payload.auto_expand,
      default_model: payload.default_model,
    });
  } catch (error) {
    $("llm-status").textContent = `读取 LLM 配置失败：${error.message}`;
  }
}

/* ---------------- 配置弹窗（LLM 配置 / 检索渠道密钥） ---------------- */

const MASK_KEEP = ""; // 留空 = 不改动
const MASK_CLEAR = "-"; // 输入 - = 清空该项

function openConfigModal() {
  $("config-modal").hidden = false;
  $("config-edit-message").innerHTML = "";
  renderConfigModal();
}

function closeConfigModal() {
  $("config-modal").hidden = true;
}

function renderConfigModal() {
  const config = state.config || {};
  const llm = config.llm || {};
  const models = llm.models || [];
  const providers = llm.providers || [];
  const search = config.search || {};

  const selected = $("config-model-select").value || llm.default_model || (models[0] ? models[0].name : "");
  $("config-model-select").innerHTML = models.length
    ? models
        .map(
          (model) =>
            `<option value="${escapeHtml(model.name)}" ${model.name === selected ? "selected" : ""}>${escapeHtml(
              model.label || model.name
            )}（${escapeHtml(model.provider)}）</option>`
        )
        .join("")
    : '<option value="">（config.json 里没有模型）</option>';

  $("config-model-rows").innerHTML =
    models.length
      ? `<div class="journal-list">${models
          .map((model) => {
            const state = model.has_api_key
              ? `已配置 key${model.key_source ? `（来源：${escapeHtml(model.key_source)}）` : ""}`
              : '<b class="warn-text">未配置 key</b>';
            return `<div class="j"><span>${escapeHtml(model.label || model.name)} <code>${escapeHtml(model.model || model.name)}</code></span>
               <span>${state} · ${escapeHtml(model.provider)}</span></div>`;
          })
          .join("")}</div>`
      : '<div class="muted">llm.models 为空。可以在下面第 3 节新增一个 OpenAI 兼容端点。</div>';
  if (providers.length) {
    $("config-model-rows").innerHTML += `<div class="muted">提供商：${providers
      .map((p) => `${escapeHtml(p.name)}（${escapeHtml(p.base_url || "未设置 base_url")}${p.has_api_key ? "，有 key" : "，无 key"}）`)
      .join("；")}</div>`;
  }

  // 只渲染「需要 key 的渠道」（Scopus / Web of Science）；PubMed 等免费渠道不再出现在这里
  const catalog = (search.source_catalog || []).filter((item) => item.requires_key);
  $("config-source-rows").innerHTML = catalog.length
    ? catalog.map((item) => sourceRowHtml(item, search)).join("")
    : '<div class="muted">当前配置里没有需要密钥的检索渠道。</div>';

  // 渠道区块用事件委托绑定（见 bindEvents 里的 config-source-rows 监听），
  // 这样每次 innerHTML 重绘都不会丢事件，也不会出现「按钮点了没反应」。
}

/* 单个「可选渠道」区块的 HTML（Scopus / Web of Science）。
   抽成独立函数是为了能被单测直接调用，避免只靠肉眼验证按钮接线。 */
function sourceRowHtml(item, search) {
  const name = String(item.name || "");
  const enabled = Boolean((search.sources || {})[name]);
  const keyState = item.has_key ? "已配置 key" : "未配置 key（可选，不填只跳过该渠道）";
  return `<div class="src-row">
    <div class="src-head">
      <b>${escapeHtml(item.label || name)}${enabled ? "（已启用）" : "（未启用）"}</b>
      <span class="muted">${escapeHtml(keyState)}</span>
    </div>
    <div class="muted">${escapeHtml(item.how_to || "")}</div>
    <div class="row">
      <label class="field wide"><span>${escapeHtml(item.key_label || "API key")}</span>
        <input id="config-key-${escapeHtml(name)}" type="password" data-source="${escapeHtml(name)}"
               placeholder="留空=不改；填 - =清空"></label>
    </div>
    <div class="row">
      <button class="ghost small" data-save-source="${escapeHtml(name)}">保存 key</button>
      <button class="primary small" data-save-enable="${escapeHtml(name)}">保存并启用</button>
      ${enabled ? `<button class="ghost small" data-disable-source="${escapeHtml(name)}">停用该渠道</button>` : ""}
    </div>
  </div>`;
}

/* 渠道区块里三个按钮的动作表：以 data-* 属性为键，供事件委托与单测共用 */
const SOURCE_ROW_ACTIONS = {
  saveSource: (name) => saveSourceKey(name, { enable: false }),
  saveEnable: (name) => saveSourceKey(name, { enable: true }),
  disableSource: (name) => setSourceEnabled(name, false),
};

function handleSourceRowClick(target) {
  const dataset = (target && target.dataset) || {};
  for (const [key, action] of Object.entries(SOURCE_ROW_ACTIONS)) {
    const value = dataset[key];
    if (value) {
      action(value);
      return true;
    }
  }
  return false;
}

function maskValue(inputId) {
  const raw = ($(inputId) ? $(inputId).value : "").trim();
  if (raw === MASK_KEEP) return undefined; // 不改
  if (raw === MASK_CLEAR) return ""; // 清空
  return raw;
}

function configMessage(html, kind = "tip") {
  const cls = kind === "warn" ? "warnbox" : "tipbox";
  const box = $("config-edit-message");
  if (box) box.innerHTML = `<div class="${cls}">${html}</div>`;
}

/* 轻量 toast：即使忽略弹窗里的提示区，也能看到保存结果 */
function toast(text, kind = "ok") {
  try {
    let box = $("toast-box");
    if (!box) {
      box = document.createElement("div");
      box.id = "toast-box";
      box.className = "toast-box";
      document.body.appendChild(box);
    }
    const item = document.createElement("div");
    item.className = `toast toast-${kind}`;
    item.textContent = text;
    box.appendChild(item);
    setTimeout(() => item.remove(), kind === "error" ? 8000 : 4000);
  } catch (error) {
    // 提示失败不影响保存结果本身
    void error;
  }
}

async function submitConfigEdit(body, successText) {
  try {
    const payload = await api("/api/config/edit", { method: "POST", body: JSON.stringify(body) });
    state.config = payload.config;
    renderConfig(payload.config);
    renderConfigModal();
    const detail = (payload.changed || []).join("、");
    configMessage(
      `<b>${escapeHtml(successText)}</b><div class="muted">改动：${escapeHtml(detail)}${
        payload.backup ? `　备份：${escapeHtml(payload.backup)}` : ""
      }</div><div class="muted">${escapeHtml(payload.note || "")}</div>`
    );
    toast(`${successText}（${detail}）`);
    return true;
  } catch (error) {
    configMessage(`<b>保存失败</b><div>${escapeHtml(error.message)}</div>`, "warn");
    toast(`保存失败：${error.message}`, "error");
    return false;
  }
}

async function withBusy(buttonId, label, task) {
  const button = $(buttonId);
  const original = button ? button.textContent : "";
  if (button) {
    button.disabled = true;
    button.textContent = label;
  }
  try {
    return await task();
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = original;
    }
  }
}

async function saveModelKey() {
  const name = $("config-model-select").value;
  const key = maskValue("config-model-key");
  if (!name) {
    configMessage("请先在 config.json 里配置一个 LLM 模型。", "warn");
    return;
  }
  if (key === undefined) {
    configMessage("密钥输入框是空的：留空表示不修改。若要清空请填 <code>-</code>。", "warn");
    return;
  }
  await withBusy("btn-save-model-key", "保存中…", async () => {
    const ok = await submitConfigEdit({ model_keys: { [name]: key } }, `模型 ${name} 的 key 已保存`);
    if (ok) {
      $("config-model-key").value = "";
      await loadLLMModels();
    }
  });
}

/* 保存渠道 key；options.enable=true 时同时打开该渠道 */
async function saveSourceKey(sourceName, options = {}) {
  const key = maskValue(`config-key-${sourceName}`);
  if (key === undefined) {
    configMessage("密钥输入框是空的：留空表示不修改。若要清空请填 <code>-</code>。", "warn");
    return;
  }
  const body = { source_keys: { [sourceName]: key } };
  if (options.enable) body.sources = { [sourceName]: true };
  const label = options.enable ? `渠道 ${sourceName} 已保存并启用` : `渠道 ${sourceName} 的 key 已保存`;
  const ok = await submitConfigEdit(body, label);
  if (ok && options.enable) {
    const toggle = document.querySelector(`.source-toggle[value="${sourceName}"]`);
    if (toggle) toggle.checked = true;
  }
}

/* 只切换渠道开关，不碰 key */
async function setSourceEnabled(sourceName, enabled) {
  const ok = await submitConfigEdit(
    { sources: { [sourceName]: enabled } },
    `渠道 ${sourceName} 已${enabled ? "启用" : "停用"}`
  );
  if (ok) {
    const toggle = document.querySelector(`.source-toggle[value="${sourceName}"]`);
    if (toggle) toggle.checked = enabled;
  }
}

async function saveProvider() {
  const name = $("config-provider-name").value.trim();
  if (!name) {
    configMessage("请填写提供商名称，例如 my-endpoint。", "warn");
    return;
  }
  const body = { provider: { name } };
  const baseUrl = $("config-provider-url").value.trim();
  const key = maskValue("config-provider-key");
  if (baseUrl) body.provider.base_url = baseUrl;
  if (key !== undefined) body.provider.api_key = key;
  if (!baseUrl && key === undefined) {
    configMessage("请至少填写 base_url 或 API key。", "warn");
    return;
  }
  const ok = await submitConfigEdit(body, `提供商 ${name} 已保存`);
  if (ok) {
    $("config-provider-key").value = "";
    await loadLLMModels();
  }
}

bindEvents();
loadConfig();
