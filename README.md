# liter-loader

**关键词检索 → 页面看清单 → 勾选 → 一键下载 → 三份汇总文件 + 全文落到 `paperdown/`。**

- 检索限定于 `config.json` 期刊白名单内的文献
- 只下载合法可直接访问的开放获取（OpenAlex / Europe PMC / Unpaywall）或出版社直连全文
- 不绕过付费墙、不使用盗版来源、不共享账号。

---

## 1. 快速开始

```powershell
# 在工作区根目录（本文件所在目录）执行
pwsh -File .\scripts\start.ps1
# 或
python -m app.server
```

终端会打印可点击地址（默认 `http://127.0.0.1:8765/`）。端口被占用时会自动向后找一个可用端口并打印实际地址。

依赖：Python 3.10+，`fastapi`、`uvicorn`、`requests`（`pip install -r requirements.txt`，`start.ps1` 会自动检查安装）。

---

## 2. 需求对照

| 需求 | 实现 |
| --- | --- |
| 1. config 里定义文献清单，检索仅限清单内 | 根目录 `config.json` 的 `journals` 字段＝**期刊白名单**（支持 `name`/`aliases`/`issn`）。检索结果按 ISSN 精确 → 规范化名称 → 缩写 → 模糊词集 四级匹配过滤，白名单外文献不进结果、不下文件，界面上只统计被过滤条数和代表性期刊。改完白名单**无需重启**，下次检索立即生效（网页右上角「重载配置」也可强制重读）。 |
| 2. 检索结果先展示，勾选后再下载 | 结果页表格展示题名、年份、期刊、影响因子、JCR 分区、指标年份/来源、索引情况、DOI、文章地址、来源库、白名单命中方式、**关键词命中字段**、优先级、全文状态；支持全选/只选开放获取/只选高优先级/清空，以及优先级、OA、关键字三维筛选；只有勾选项会被下载（未勾选项记为「未勾选」）。 |
| 3. 输入是若干关键词，用 `;` 分割 | 关键词框按 `;` 拆成若干概念块（兼容全角 `；` 与半角 `,`），块内用 `\|` 或 `/` 写同义词；块间 `AND`、块内 `OR`，自动拼成标准 Boolean 检索式，并行查询 OpenAlex、Crossref、Europe PMC、PubMed、Scopus、Web of Science 六个检索渠道。 |
| 4. 检索可匹配标题/关键词/摘要 | 网页「检索范围」下拉 + `config.json` 的 `search.scope`，四个取值见下方「检索范围」一节：仅标题 / 标题+摘要（默认）/ 标题+摘要+关键词 / 全部字段（含全文）。每个数据源会按自己的字段语法生成检索式，界面与报告里都会显示实际检索式与每条文献的命中字段。 |
| 5. 检索前用 LLM 生成新关键词 | 点「AI 扩展关键词」（或勾选「每次检索自动扩展」）：把每个概念块、检索范围与期刊白名单领域一起交给大模型，为**每个概念块生成 8-10 个**相关新词；扩展结果先以可勾选标签列出，确认后才与用户关键词一起检索。|
| 6. 可配置 LLM 模型与 key | 两种方式：(1) 网页右上角 **「LLM配置」按钮** 打开弹窗直接填 key（写回 `config.json`，写前校验 + 自动备份，界面不回显明文）；(2) 手工编辑 `config.json` 的 `llm` 段（`providers`/`models`/`api_key`/`api_key_env`）。 |
| 7. Scopus 与 Web of Science 渠道需要API key | 两者都需要**机构订阅的 API key**（Scopus 用 Elsevier key、WoS 用 Clarivate key），在「LLM配置」弹窗里填入即可启用；未配置时会明确提示并跳过，不影响其他渠道。|
| 8. 生成汇总文件 + 全文存到 `paperdown/` | 每次检索生成一个运行目录，包含 `下载报告.html`、`文章地址总表.csv`、`文献清单.md`（检索完成先出一版，下载完成后覆盖更新）；下载的 PDF/HTML/XML 全部保存在 `paperdown/runs/<运行编号>/papers/`。 |

---

## 3. 工具设置内容

### 3.1 `config.json` 内容
```json
{
  "search": {
    "year_from": 2021,
    "year_to": 2026,
    "max_results_per_source": 100,
    "scope": "title_abstract",   // 见下方「检索范围」
    "sources": {
      "openalex": true, "crossref": true, "europepmc": true, "pubmed": true,
      "scopus": false, "wos": false        // 需要订阅 key，配好后再打开（或在网页上勾选）
    },
    "exclude_types": ["editorial", "commentary", "news", "conference abstract", "patent"],
    "email": "",             // 填上你的邮箱，进入 Crossref/OpenAlex/NCBI 的 polite pool，更稳定
    "unpaywall_email": "",   // 填上邮箱后启用 Unpaywall 兜底找 OA PDF；留空则跳过
    "api_keys": {
      "pubmed": "",          // 可选：提高 NCBI 限流额度
      "scopus": "",          // Elsevier Scopus API key（X-ELS-APIKey）
      "wos": ""              // Clarivate Web of Science API key（X-ApiKey）
    }
  },
  "download": {
    "output_root": "paperdown",   // 全文与报告的根目录（相对工作区，也可写绝对路径）
    "timeout_seconds": 30,
    "max_attempts_per_record": 4, // 每条最多尝试几个全文入口
    "delay_seconds": 0.6,         // 每次请求之间的等待，避免被限流
    "concurrency": 2
  },
  "journal_match": { "allow_abbrev_prefix": true, "allow_fuzzy_tokens": true },
  "journals": [
    {
      "name": "Science of the Total Environment",
      "aliases": ["Sci Total Environ", "Sci. Total Environ.", "STOTEN"],
      "issn": ["0048-9697", "1879-1026"],
      "impact_factor": "",
      "jcr_quartile": "",
      "metric_year": "",
      "metric_source": "",
      "indexing": "SCIE",
      "note": ""
    }
  ]
}
```

要点：

- `journals` 即「文献清单」：只有期刊命中清单的检索结果才会出现、才会被下载。
- 匹配顺序：`issn` 精确 → 归一化后的 `name`/`aliases` 完全一致 → 缩写前缀（容忍 `Sci. Total Environ.` = `Science of the Total Environment`）→ 词集合 Jaccard ≥ 0.72。命中方式会显示在结果表「白名单命中」列，便于核对。
- 期刊指标（影响因子、分区、指标年份、指标来源、索引情况）**留空就显示「待核验」**，脚本绝不凭空生成数字。填了就会合并进每一行清单，不用另外对照表。
- 白名单为空时，检索会直接返回 400 并提示先编辑 `config.json`。

---

### 3.2 检索范围（关键词匹配）

网页「检索范围」下拉与 `config.json` 的 `search.scope` 是同一个东西（前端选择覆盖配置，只对本次检索生效）：

| `scope` 取值 | 界面显示 | 含义 |
| --- | --- | --- |
| `title` | 仅标题 | 关键词必须出现在标题里，最精确、结果最少 |
| `title_abstract` | 标题 + 摘要 | 默认，兼顾精确与召回 |
| `title_abstract_keywords` | 标题 + 摘要 + 关键词 | 再加上作者关键词/主题词 |
| `all` | 全部字段（含全文） | 召回最多，噪声也最多 |

各数据源的字段写法（程序自动生成，界面与报告里都会展示实际检索式）：

| 数据源 | 标题 | 摘要 | 关键词 | 全文 |
| --- | --- | --- | --- | --- |
| OpenAlex | `title:` | `abstract:` | `keyword:` | 默认 `search` |
| Europe PMC | `TITLE:` | `ABSTRACT:` | `KW:` | 默认字段 |
| PubMed | `[ti]` | `[tiab]` | `[ot]` | `[tw]` |
| Crossref | — | — | — | — |

Crossref 的 API 不支持字段限定，只能按相关度返回。因此：

- 选择较窄范围时，界面会明确提示「Crossref 不支持严格限定标题/摘要字段」；
- 我们自己会对每条结果做**按字段的关键词打分**，只有命中本次范围内的字段才计分，命中不了的概念块不给你加分——所以 Crossref 带回来的「只在摘要里出现」的记录不会被误算成标题命中，结果表里会显示每条实际命中的字段（`关键词命中字段` 列）。

---

### 3.3 AI 关键词扩展（LLM）

#### 3.3.1 使用方法

1. 在「关键词检索」里先填你已有的关键词（`;` 分概念块，`|` 写同义词），并选好「检索范围」。
2. 点 **「AI 扩展关键词」**：后端把概念块、检索范围（标题/摘要/关键词）和期刊白名单的领域范围一起交给模型，要求为**每个概念块补充 8-10 个**同义词/缩写/变体/相关术语（提示词里明确要求不改变概念、不与其它块串味、不重复用户已有的词）。
3. 结果以标签形式展示：**浅绿色 = 你输入的原词（不可取消）**，白底方块 = 模型新增词（默认全选，可逐个取消）。每个概念块还带一句模型的说明。
4. 确认后点 **「用这些关键词开始检索」**（或直接点「开始检索」，此时也会带上你勾选的扩展词）。界面上的检索式、`文献清单.md` 与 `下载报告.html` 都会写明哪些词来自你、哪些来自 AI。
5. 想让每次检索都自动先扩展一次，就勾上 **「每次检索自动扩展」**（也可以在 `config.json` 里把 `llm.auto_expand` 设为 `true` 作为默认值）。

#### 3.3.2 配置模型与 key

```json
"llm": {
  "enabled": true,
  "auto_expand": false,
  "default_model": "deepseek-flash",
  "timeout_seconds": 90,
  "max_new_terms_per_group": 10,
  "topic": "",
  "language_hint": "以英文关键词为主",
  "providers": [
    { "name": "deepseek", "type": "deepseek", "base_url": "https://api.deepseek.com", "api_key": "" },
    { "name": "custom",  "type": "openai-compatible", "base_url": "", "api_key": "" }
  ],
  "models": [
    { "name": "deepseek-flash", "label": "DeepSeek Flash", "provider": "deepseek", "model": "deepseek-flash",
      "thinking": "disabled", "json_mode": true, "max_tokens": 2048, "api_key": "", "api_key_env": "DEEPSEEK_API_KEY" },
    { "name": "deepseek-v4-pro", "provider": "deepseek", "model": "deepseek-v4-pro", "thinking": "disabled", "json_mode": true },
    { "name": "my-endpoint-model", "provider": "custom", "model": "你的模型名",
      "base_url": "https://your-endpoint.example.com/v1", "json_mode": false, "temperature": 0.3 }
  ]
}
```

- **填 key 的三种方式**（优先级从高到低）：
  1. 直接写 `"api_key": "sk-..."`（注意 `config.json` 已被 `.gitignore` 忽略，别把 key 提交到仓库；要分享配置请用 `config.example.json`）；
  2. `"api_key": "env:DEEPSEEK_API_KEY"` 或 `"api_key_env": "DEEPSEEK_API_KEY"` —— 从环境变量读；
  3. 都不写时，自动探测常见环境变量（如 `DEEPSEEK_API_KEY`、`LITLOADER_<模型名>_API_KEY`）。
- **字面 `api_key` 永远优先于环境变量。** 写在 `api_key_env` 里的变量若未设置，**不会**把已填写的字面 key 清空（早期版本正是在这里清空了密钥，导致「弹窗保存 key 成功，紧接着测试连接却报没有 api_key」）。这种情况只会在配置警告里提示一句，不影响已填的 key；界面上模型的 key 来源也会显示为「config.json（api_key）」或「环境变量 XXX」。
- **`base_url` / `model` 的取用顺序**：模型级 > 提供商级 > DeepSeek 官方默认 `https://api.deepseek.com`；端点会自动补 `/chat/completions`（`.../v1` 会补成 `.../v1/chat/completions`）。
- **DeepSeek 专用参数**：`"thinking": "disabled"`（默认，关键词扩展用不上思考模式，更快更省）、要更高质量可设 `"thinking": "enabled"` 并加 `"reasoning_effort": "high"`；思考模式下会忽略 `temperature`。`json_mode: true` 会带 `response_format={"type":"json_object"}`（部分兼容端点不支持，可关掉，程序会退回解析文本）。
- 任何 OpenAI 兼容端点（自建 vLLM/Ollama、其它厂商）只要在 `providers` 里设 `type: "openai-compatible"` + `base_url`，并在 `models` 里指向它即可。`provider_presets` 会同步给前端用于提示。
- 改完 `config.json` 在网页上点「重载配置」，然后点 **「测试模型连接」** 验证 key/base_url/模型名；成功会显示端点与模型回复预览，失败会给出具体原因与排查清单（缺少 api_key、HTTP 401、超时、域名不可达等）。


#### 3.3.3 安全与降级

- `api_key` **只存在于后端**：`/api/config`、`/api/llm/models` 只返回 `has_api_key: true/false`；错误信息不回显请求头；运行目录的 `config.snapshot.json` 与三份交付文件里只有模型名，没有任何 key。
- **LLM 不可用不影响检索**：未配置 key、模型名写错、网络失败、返回空内容或格式不符时，接口会返回明确的中文错误、界面给出提示，检索仍然只用你自己的关键词正常进行（`stats.llm_used = false`）。
- 模型返回的词会先清洗（去掉布尔运算符、括号、引号、超长内容、与用户重复的词），并限制**每块最多 10 个、整体最多 30 个**，避免把检索式撑爆。
- 没有 key 也想看界面效果时，可以用本地假端点自测：

  ```powershell
  python scripts\mock_llm_server.py --port 8899
  python -m app.server --config config.mock-llm.json   # 该配置已把 base_url 指向假端点
  ```

---

### 3.4 Scopus 与 Web of Science（可选）

两个渠道都通过**官方 API** 检索（不是网页抓取），因此需要机构订阅对应的 API key。**它们是可选的**：不配 key 也能用 OpenAlex / Crossref / Europe PMC / PubMed 四个免费渠道，只是这两个渠道会被跳过并给出提示。

| 渠道 | 端点 | 认证 | 申请 | 检索式字段 |
| --- | --- | --- | --- | --- |
| Scopus | `api.elsevier.com/content/search/scopus` | 请求头 `X-ELS-APIKey` | Elsevier 开发者门户，机构需有 Scopus 订阅 | `TITLE()` / `ABS()` / `KEY()` / `ALL()`；年份用 `PUBYEAR` |
| Web of Science | `api.clarivate.com/apis/wos-starter/v1/documents` | 请求头 `X-ApiKey` | Clarivate 开发者门户（WoS Starter 计划） | `TI=` / `AB=` / `AK=` / `ALL=`；年份用 `publishTimeSpan` |

每种检索范围映射到两个渠道的写法（界面与报告里都会显示实际检索式）：

| 范围 | Scopus | Web of Science |
| --- | --- | --- |
| 仅标题 | `TITLE("...")` | `TI="..."` |
| 标题 + 摘要 | `(TITLE(..) OR ABS(..))` | `(TI=.. OR AB=..)` |
| + 关键词 | 再加 `KEY(..)` | 再加 `AK=..` |
| 全部字段 | `ALL(..)`（整条记录，含参考文献） | `ALL=..`（全记录字段） |

行为约定：

- 渠道**默认关闭**（`sources.scopus/wos = false`）；在界面上勾选或在 `config.json` 打开即可。
- 没有配 key 时：勾选框旁标注「需 key」，检索时会直接跳过该渠道并给出中文提示（`source_errors: {scopus: missing_api_key}`），**不影响其他渠道**；报告里的「各数据源实际检索式」与提示区也会写明。
- key 无效或机构无订阅权限时：Scopus 返回 `AUTHENTICATION_ERROR`、WoS 返回 401，都会转成「API key 无效或该账号没有订阅权限」的可读提示，而不是静默空结果。
- 触发限流（429）时会提示稍后重试或减少每源条数。
- 离线自测：端点可用环境变量 `LITLOADER_SCOPUS_ENDPOINT` / `LITLOADER_WOS_ENDPOINT` 覆盖，`tests/test_new_sources.py` 就是用本地假服务端跑通了「六渠道配置 → 检索式 → 响应解析 → 白名单过滤 → 三份交付文件」的完整链路。

---

## 4. 网页使用示例

1. **检索配置**：显示当前白名单期刊（含 ISSN 与指标）、年份范围、数据源状态，以及 AI 关键词扩展的模型与 key 状态。改完 `config.json` 点右上角「重载配置」；点「LLM配置」可以直接填各种 key（见3.3, 3.4）。
2. **关键词检索**：填关键词，例如

   ```text
   large language model|LLM|ChatGPT; environmental science|climate|sustainability
   ```

   可选：检索范围（仅标题 / 标题+摘要 / +关键词 / 全部字段）、起止年份、每个数据源最多条数、数据源开关、「只在有开放获取信号的文献中检索」，以及 AI 关键词扩展（见3.3）。
3. **检索结果**：先看统计（检索范围、白名单内条数、白名单外过滤条数、去重合并条数）与「各数据源实际检索式」，再勾选要的文献。默认「只选开放获取」最省事。表里的「关键词命中字段」列告诉你这条是靠标题、摘要还是关键词命中的。
4. **下载进度与交付文件**：点「下载文献」后进度条实时更新，结束后出现三份文件的下载链接、「已下载全文」清单，以及「打开 paperdown 运行目录」按钮。报告/清单文件也始终保存在运行目录里，可以直接到 `paperdown/runs/<运行编号>/` 查看。

---

## 5. 输出目录与三份交付文件

```text
paperdown/
├─ runs/
│  └─ 20260915_203222_large-language-model/
│     ├─ 下载报告.html        ← 交付物 1：汇总的下载报告
│     ├─ 文章地址总表.csv      ← 交付物 2：每篇文献的地址与状态
│     ├─ 文献清单.md          ← 交付物 3：可编辑的清单/笔记底稿
│     ├─ 候选文献总表.csv       ← 检索全量（含未勾选、被排除项）
│     ├─ 下载日志.csv          ← 每个候选论文的最终状态
│     ├─ 下载尝试日志.jsonl     ← 每次尝试的具体 URL、HTTP 状态与结果（技术追溯）
│     ├─ config.snapshot.json  ← 本次运行用的白名单快照（可复现）
│     ├─ run.meta.json         ← 运行元信息（检索式、统计、过滤清单）
│     └─ papers/              ← 实际下载到的 PDF / HTML / XML
└─ index.json                 ← 历次运行索引
```

- `文章地址总表.csv`：列＝`序号, 题名, 年份, 期刊或来源, 影响因子, JCR分区, 指标年份, 指标来源, 索引情况, DOI, 文章地址, 是否勾选, 是否下载成功, 全文状态, 文件或原因`（UTF-8-SIG）。
- `文献清单.md`：检索元信息（含检索范围与各数据源实际检索式）+ **本次检索关键词（区分用户输入与 AI 扩展，按概念块列表）** + 结果概览 + 按优先级分组逐条明细（含关键词命中字段）+ 全部文献总表 + 待处理文献与合法获取路线 + 检索日志 + 合规说明。
- `下载报告.html`：汇总卡片（白名单期刊数、候选数、勾选数、PDF 数、网页/XML 数、需后续处理数、已标注指标数）、检索范围与各数据源检索式、**本次检索关键词（用户输入 / AI 扩展）**、「已经拿到的全文」表、「需要后续处理」表（含下一步建议）、给学生的下一步、合规说明；表格里带「命中字段」列，可直接浏览器打开或打印成 PDF。

状态包含：`已下载 PDF`、`已保存网页全文`、`已保存 XML 全文`、`只找到题录`、`平台限制直连下载`、`链接失效`、`被限流`、`已排除`、`尝试失败`。

---

## 6. 全文获取顺序

对每条勾选的文献，按顺序尝试（最多 `download.max_attempts_per_record` 个）：

1. OpenAlex 的 `best_oa_location.pdf_url`（开放获取直链）
2. Europe PMC 的 OA 全文链接（`fullTextUrlList` 里 `availabilityCode=OA`、优先 PDF）
3. Europe PMC REST 全文 XML：`www.ebi.ac.uk/europepmc/webservices/rest/<PMCID>/fullTextXML`
4. Unpaywall（需在配置里填 `unpaywall_email`）
5. `https://doi.org/<DOI>` 解析跳转；落到 PDF 就保存，落到 HTML 则提取页面里的 `citation_pdf_url` / `.pdf` 链接再试一次

判定规则：

- `%PDF` 魔数或 `application/pdf` → `success_pdf`
- XML → `success_xml`；有效 HTML 全文页 → `success_html`
- 命中登录页/订阅页/付费墙关键词 → `inaccessible`（原因 `paywall_or_login_detected`）
- HTML 小于 12 KB → `inaccessible`（原因 `html_stub_not_fulltext`，这是跳转页/占位页）
- 401/402/403 → `inaccessible`；404/410 → `broken_link`；429 → `rate_limited`

---

## 7. 自检与测试

```powershell
python tests\test_journal_match.py        # 白名单匹配：ISSN / 缩写 / 模糊 / 拒绝
python tests\test_query_scope.py          # 四种检索范围 ↔ 各数据源字段语法
python tests\test_analysis.py             # 关键词拆分、多源合并去重、按字段打分、白名单过滤
python tests\test_llm.py                  # LLM 返回解析、提示词、DeepSeek 请求体、密钥不泄露、mock 端到端
python tests\test_key_precedence.py       # api_key 与 api_key_env 的优先级（保存 key 后必须立刻可用）
python tests\test_new_sources.py          # Scopus/WoS 检索式与解析、缺 key/401/429、配置写回与回滚、六渠道全链路
python tests\test_runs_paths.py           # 运行目录、路径穿越防护、统计
python tests\test_fulltext_classify.py    # PDF/XML/HTML 分类、付费墙/拦截页识别、候选排序
python scripts\check_frontend.py          # 前端 id 接线、括号配平、接口引用
python scripts\check_render_units.mjs     # 可选渠道区块渲染与按钮映射（Node，无需浏览器）
python scripts\mock_dom_check.mjs         # 用最小 DOM 模拟器跑真实 app.js：弹窗开关/保存/toast（需服务已启动）
python scripts\check_secrets.py           # 敏感信息检查：会不会把 api_key 之类推到远端仓库
python scripts\smoke_test.py              # 端到端（联网）：真实检索 → 勾选 → 下载 → 校验三份文件
```

`test_llm.py` **不需要真实 API key**：它自带一个假的 OpenAI 兼容端点，覆盖「连接测试成功/失败」「返回分组 JSON / 扁平列表 / 纯文本 / 空内容 / 401」以及「扩展词合并进检索并写入统计」的全过程。

`smoke_test.py` 说明：

- 默认使用根目录 `config.json`（示例白名单较窄）。若白名单内没有可下载的开放获取条目，会**临时扩充白名单**（只作用于内存，不写回 `config.json`）以验证下载与报告链路，运行目录的 `run.meta.json` 会记录 `whitelist_expanded_for_test: true`。加 `--no-auto-expand` 可关闭该行为。
- `--scope title|title_abstract|title_abstract_keywords|all` 可切换检索范围；`--scope title` 时会额外断言「命中字段只能是标题」。
- `config.smoke.json` 是一份覆盖环境/可持续领域常见期刊的更宽白名单（含指标示例），可用于验证真实 PDF 下载：`python scripts\smoke_test.py --config config.smoke.json --limit 5`。


## 8. 自检与测试

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/config` | 当前配置（脱敏：不返回 api key 明文；含检索范围选项、检索渠道清单 `source_catalog`、LLM 概况） |
| POST | `/api/config/reload` | 重新读盘 |
| POST | `/api/config/edit` | 写回 key/邮箱：`{source_keys?, model_keys?, provider_keys?, provider?, email?, unpaywall_email?}`；写前校验 + 原子替换 + 备份，失败自动回滚，返回 `{changed, backup, config}` |
| GET | `/api/llm/models` | 可用 LLM 模型/提供商（`has_api_key` 布尔值，不含明文 key）与是否就绪 |
| POST | `/api/llm/test` | `{model?}`：用极小请求验证 base_url/key/模型名，返回端点与回复预览 |
| POST | `/api/expand-keywords` | `{keywords, model?, scope?, topic?, language_hint?}`：按概念块返回 AI 扩展词 |
| POST | `/api/search` | `{keywords, keyword_groups?, llm_model?, scope?, year_from?, year_to?, sources?, max_results_per_source?, oa_only?}`；`keyword_groups` 为界面确认后的「概念块 → 词 + 来源标记」，`scope` 取 `title`/`title_abstract`/`title_abstract_keywords`/`all` |
| POST | `/api/runs/{run_id}/download` | `{record_ids: [...]}`，返回 `job_id`（后台线程执行） |
| GET | `/api/runs/{run_id}/download/status?job_id=` | 进度、统计、三份文件路径 |
| GET | `/api/runs/{run_id}/file/{name}` | 下载交付文件（只允许固定文件名，禁止路径穿越） |
| GET | `/api/runs/{run_id}/papers` | 已下载全文清单 |
| POST | `/api/open-folder` | `{run_id}`，在资源管理器里打开运行目录 |

---

## 9. 项目结构

```text
liter-loader/
├─ config.json            # 唯一需要你编辑的文件（期刊白名单 + 参数 + LLM 模型/key）
├─ config.example.json    # 配置模板（含 LLM 段示例；分享配置用这个，别用带 key 的 config.json）
├─ requirements.txt
├─ app/
│  ├─ server.py           # FastAPI 接口 + 后台下载任务 + 启动入口
│  ├─ config.py           # config.json 读取与校验（含 LLM 与密钥解析/脱敏）
│  ├─ config_store.py     # 网页「LLM配置」写回：只改点名项、写前校验、原子替换与备份
│  ├─ models.py           # 数据模型、状态枚举、中文状态映射
│  ├─ llm.py              # LLM 客户端、提示词、关键词扩展与返回解析
│  ├─ query_scope.py      # 检索范围 ↔ 各数据源字段语法
│  ├─ journal_match.py    # 期刊白名单匹配
│  ├─ analysis.py         # 关键词拆分、合并去重、打分、过滤
│  ├─ fulltext.py         # 全文候选排序与合法下载
│  ├─ report.py           # 三份交付文件生成
│  ├─ runs.py             # 运行目录、路径安全、索引
│  ├─ sources/            # openalex / crossref / europepmc / pubmed / scopus / wos
│  └─ web/                # index.html + app.js + style.css（原生，无构建）
├─ scripts/
│  ├─ start.ps1           # 一键启动
│  ├─ smoke_test.py       # 端到端冒烟
│  ├─ mock_llm_server.py  # 本地假 LLM 端点（无 key 时自测关键词扩展）
│  ├─ check_frontend.py   # 前端静态自检（id 接线/括号/接口）
│  ├─ check_render_units.mjs  # 纯渲染函数单测（可选渠道区块、按钮映射）
│  ├─ check_secrets.py    # 提交前敏感信息扫描
│  ├─ install_git_hooks.py    # 安装 pre-commit 钩子（提交前自动扫描）
│  ├─ git-hooks/pre-commit    # 钩子模板（钩子本身只写在本机 .git 里）
│  └─ mock_dom_check.mjs  # 最小 DOM 模拟器跑真实 app.js，验证弹窗开关与保存
├─ tests/                 # 单元测试
├─ .gitignore             # 敏感信息守则：配置/密钥/产物一律不入库
├─ .gitattributes         # 统一换行，避免跨平台整文件差异
└─ config.example.json    # 唯一会提交的配置模板（不含任何密钥）
```

---

## 10. 常见问题解决

- **Q:** 检索结果很少？**A:** 一般是由于白名单太窄造成的。看报告里「白名单外被过滤的期刊」小节，把要的期刊加进 `journals` 再检索。
- **Q:** 中文关键词几乎没结果？ **A:** 本工具以英文期刊发表为目标，建议使用英文检索。
- **Q:** 显示「平台限制直连下载」？ **A:** 多数是订阅期刊的常见情况。
- **Q:** Scopus / Web of Science 没结果？ **A:** 需要配置检索渠道的 API key。
- **Q:** LLM如何配置？ **A:** 点右上角「LLM配置」，填完保存即可（会自动备份 `config.json.bak`；留空=不改，填 `-`=清空）。
- **Q:** 如何修改输出路径？ **A:** 修改 `config.json` 的 `download.output_root`（支持绝对路径）。
- **Q:** 如何调整某数据源？ **A:**`search.sources` 里对应项设为 `false`，或在界面上取消勾选。
- **Q:** 端口不是 8765无法使用？**A:** 可能是默认端口被占用，用终端打印的实际端口（也可以通过`--port` 指定起始端口）。
