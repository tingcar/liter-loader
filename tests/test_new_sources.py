"""新增检索渠道（Scopus / Web of Science）与配置写回（LLM配置弹窗）的测试。

- 数据源响应解析用固定 JSON 夹具；
- 请求构造用本地假服务端，覆盖 200 / 401 / 429 / 缺 key 四种路径；
- 配置写回测「只改点名项 + 备份 + 写前校验回滚 + 不泄露密钥」。
"""

from __future__ import annotations

import json
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import SearchConfig, load_config  # noqa: E402
from app.config_store import ConfigWriteError, update_config_file  # noqa: E402
from app.query_scope import (  # noqa: E402
    scopus_query,
    source_catalog,
    wos_query,
)
from app.sources import scopus, wos  # noqa: E402
from app.sources.base import SearchContext  # noqa: E402

WORKSPACE = Path(__file__).resolve().parent.parent
TMP = WORKSPACE / ".tmp_new_sources"

GROUPS = [["large language model", "LLM"], ["climate"]]

SCOPUS_FIXTURE = {
    "search-results": {
        "opensearch:totalResults": "2",
        "entry": [
            {
                "dc:identifier": "SCOPUS_ID:85123456789",
                "eid": "2-s2.0-85123456789",
                "dc:title": "Large language models for climate adaptation planning",
                "dc:creator": "Doe J.",
                "prism:publicationName": "Environmental Research Letters",
                "prism:issn": "1748-9326",
                "prism:coverDate": "2024-05-01",
                "prism:doi": "10.1088/1748-9326/ad0123",
                "citedby-count": "17",
                "prism:url": "https://www.scopus.com/record/display.uri?eid=2-s2.0-85123456789",
                "openaccess": "1",
                "subtypeDescription": "Article",
                "dc:description": "We study LLM-based climate planning tools.",
                "author": [
                    {"authname": "Jane Doe", "given-name": "Jane", "surname": "Doe"},
                    {"authname": "Li Wei", "given-name": "Wei", "surname": "Li"},
                ],
            },
            {
                "dc:identifier": "SCOPUS_ID:85111111111",
                "dc:title": "Second climate paper",
                "prism:publicationName": "Nature Climate Change",
                "prism:issn": "1758-678X",
                "prism:coverDate": "2023-01-15",
                "prism:doi": "10.1038/s41558-023-0001-2",
                "citedby-count": "3",
                "author": {"authname": "Solo Author"},
            },
        ],
    }
}

WOS_FIXTURE = {
    "Metadata": {"total": 2, "page": 1, "limit": 10},
    "Data": {
        "Records": [
            {
                "UID": "WOS:001234567800001",
                "Title": "Large language models in sustainability reporting",
                "Abstract": "We examine LLM use in sustainability reporting.",
                "Source": {
                    "SourceTitle": "Journal of Cleaner Production",
                    "SourceAbbrev": "J CLEAN PROD",
                    "Published.BiblioYear": "2024",
                    "Publisher": "Elsevier",
                },
                "Names": [
                    {"DisplayName": "Anna Smith", "FirstName": "Anna", "LastName": "Smith"},
                    {"DisplayName": "Bo Chen"},
                ],
                "Doi": "10.1016/j.jclepro.2024.140001",
                "Identifiers": [{"value": "10.1016/j.jclepro.2024.140001"}, {"value": "0959-6526"}],
                "DocumentType": "Article",
                "Citations": [{"Count": 9}],
            },
            {
                "UID": "WOS:001234567800002",
                "Title": {"Title": "Nested title form"},
                "Source": {"SourceTitle": "Nature", "Published.Year": "2023"},
                "Names": [{"FirstName": "Cy", "LastName": "Rong"}],
                "Issn": ["0028-0836"],
            },
        ]
    },
}


# ------------------------------------------------------------------ 检索式


def test_scopus_query_per_scope() -> None:
    assert scopus_query("title", GROUPS).startswith("(")
    assert "TITLE(" in scopus_query("title", GROUPS)
    assert "ABS(" not in scopus_query("title", GROUPS)
    tak = scopus_query("title_abstract_keywords", GROUPS)
    assert "TITLE(" in tak and "ABS(" in tak and "KEY(" in tak
    all_query = scopus_query("all", GROUPS)
    assert "ALL(" in all_query and "TITLE(" not in all_query, "all 范围应只保留最宽的 ALL()"
    assert "PUBYEAR > 2022 AND PUBYEAR < 2027" in scopus_query("title", GROUPS, (2023, 2026))


def test_wos_query_per_scope() -> None:
    assert wos_query("title", GROUPS) == '(TI="large language model" OR TI=LLM) AND (TI=climate)'
    tak = wos_query("title_abstract_keywords", GROUPS)
    assert "TI=" in tak and "AB=" in tak and "AK=" in tak
    all_query = wos_query("all", GROUPS)
    assert "ALL=" in all_query and "TI=" not in all_query, "all 范围应只保留最宽的 ALL="


def test_source_catalog_flags() -> None:
    catalog = {item["name"]: item for item in source_catalog({"scopus": "k"})}
    assert catalog["scopus"]["requires_key"] is True
    assert catalog["scopus"]["has_key"] is True and catalog["scopus"]["available"] is True
    assert catalog["wos"]["has_key"] is False and catalog["wos"]["available"] is False
    assert catalog["openalex"]["requires_key"] is False
    assert catalog["pubmed"]["requires_key"] is False
    assert {"openalex", "crossref", "europepmc", "pubmed", "scopus", "wos"} == set(catalog)


# ------------------------------------------------------------------ 假服务端


class FakeSourceHandler(BaseHTTPRequestHandler):
    """按路径区分返回 Scopus / WoS 的固定响应，并记录收到的请求。"""

    scopus_status = 200
    wos_status = 200
    scopus_body = SCOPUS_FIXTURE
    wos_body = WOS_FIXTURE
    requests_log: list[dict] = []

    def log_message(self, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        FakeSourceHandler.requests_log.append(
            {
                "path": self.path,
                "headers": {key.lower(): value for key, value in self.headers.items()},
            }
        )
        if "scopus" in self.path:
            status, body = FakeSourceHandler.scopus_status, FakeSourceHandler.scopus_body
        else:
            status, body = FakeSourceHandler.wos_status, FakeSourceHandler.wos_body
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def start_fake() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeSourceHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def make_context(api_keys: dict[str, str], scope: str = "title_abstract") -> SearchContext:
    search = SearchConfig(year_from=2023, year_to=2026, api_keys=api_keys)
    return SearchContext(
        keywords_raw="x",
        groups=GROUPS,
        year_from=2023,
        year_to=2026,
        max_results_per_source=10,
        search=search,
        scope=scope,
    )


def test_scopus_parsing_via_fake_server() -> None:
    server, base_url = start_fake()
    original = scopus.API_URL
    scopus.API_URL = base_url + "/content/search/scopus"
    try:
        FakeSourceHandler.scopus_status = 200
        FakeSourceHandler.requests_log.clear()
        outcome = scopus.search(make_context({"scopus": "test-key"}))
        assert outcome.error == "", outcome.error
        assert len(outcome.records) == 2
        first = outcome.records[0]
        assert first.title == "Large language models for climate adaptation planning"
        assert first.doi == "10.1088/1748-9326/ad0123"
        assert first.year == 2024
        assert first.journal == "Environmental Research Letters"
        assert first.issns == ["1748-9326"]
        assert first.oa_status == "open"
        assert first.cited_by_count == 17
        assert "Doe" in first.authors and "Li" in first.authors
        assert first.landing_page_url.startswith("https://www.scopus.com/record")
        assert first.article_url == "https://doi.org/10.1088/1748-9326/ad0123"
        # 第二个条目的 author 是单个对象而不是数组，也要能处理
        assert outcome.records[1].authors == "Solo Author"

        request = FakeSourceHandler.requests_log[-1]
        assert request["headers"].get("x-els-apikey") == "test-key"
        from urllib.parse import parse_qs, urlparse

        query_param = parse_qs(urlparse(request["path"]).query).get("query", [""])[0]
        assert query_param.startswith("((") and "TITLE(" in query_param and "ABS(" in query_param
        assert request["headers"].get("accept") == "application/json"
        params = parse_qs(urlparse(request["path"]).query)
        assert params.get("count") == ["10"]
        assert params.get("view") == ["COMPLETE"]
        assert params.get("sort") == ["relevancy"]
    finally:
        scopus.API_URL = original
        server.shutdown()


def test_scopus_missing_key_and_auth_error() -> None:
    outcome = scopus.search(make_context({}))
    assert outcome.error == "missing_api_key"
    assert outcome.count == 0
    assert any("Scopus" in item and "key" in item for item in outcome.warnings)

    server, base_url = start_fake()
    original = scopus.API_URL
    scopus.API_URL = base_url + "/content/search/scopus"
    try:
        FakeSourceHandler.scopus_status = 401
        FakeSourceHandler.scopus_body = {"service-error": {"status": {"statusCode": "AUTHENTICATION_ERROR"}}}
        outcome = scopus.search(make_context({"scopus": "bad"}))
        assert outcome.error == "http_401"
        assert any("401" in item for item in outcome.warnings)

        FakeSourceHandler.scopus_status = 429
        outcome = scopus.search(make_context({"scopus": "k"}))
        assert outcome.error == "http_429"
        assert any("429" in item for item in outcome.warnings)
    finally:
        scopus.API_URL = original
        FakeSourceHandler.scopus_status = 200
        FakeSourceHandler.scopus_body = SCOPUS_FIXTURE
        server.shutdown()


def test_wos_parsing_via_fake_server() -> None:
    server, base_url = start_fake()
    original = wos.API_URL
    wos.API_URL = base_url + "/apis/wos-starter/v1/documents"
    try:
        FakeSourceHandler.wos_status = 200
        FakeSourceHandler.requests_log.clear()
        outcome = wos.search(make_context({"wos": "test-key"}))
        assert outcome.error == "", outcome.error
        assert len(outcome.records) == 2
        first = outcome.records[0]
        assert first.title == "Large language models in sustainability reporting"
        assert first.doi == "10.1016/j.jclepro.2024.140001"
        assert first.journal == "Journal of Cleaner Production"
        assert first.journal_short == "J CLEAN PROD"
        assert first.year == 2024
        assert first.cited_by_count == 9
        assert "Anna Smith" in first.authors and "Bo Chen" in first.authors
        assert "0959-6526" in first.issns
        # 第二个条目的 Title 是嵌套对象、Issn 是数组、Names 只有 First/Last
        assert outcome.records[1].title == "Nested title form"
        assert outcome.records[1].year == 2023
        assert outcome.records[1].issns == ["0028-0836"]
        assert outcome.records[1].authors == "Cy Rong"

        request = FakeSourceHandler.requests_log[-1]
        assert request["headers"].get("x-apikey") == "test-key"
        assert "db=WOS" in request["path"]
        assert "publishTimeSpan=2023-01-01" in request["path"]
    finally:
        wos.API_URL = original
        server.shutdown()


def test_wos_missing_key_and_auth_error() -> None:
    outcome = wos.search(make_context({}))
    assert outcome.error == "missing_api_key"
    assert any("Web of Science" in item for item in outcome.warnings)

    server, base_url = start_fake()
    original = wos.API_URL
    wos.API_URL = base_url + "/apis/wos-starter/v1/documents"
    try:
        FakeSourceHandler.wos_status = 401
        FakeSourceHandler.wos_body = {"message": "No API key found in request"}
        outcome = wos.search(make_context({"wos": "bad"}))
        assert outcome.error == "http_401"
        assert any("401" in item for item in outcome.warnings)
    finally:
        wos.API_URL = original
        FakeSourceHandler.wos_status = 200
        FakeSourceHandler.wos_body = WOS_FIXTURE
        server.shutdown()


# ------------------------------------------------------------------ 配置写回


def write_config(path: Path, *, scopus_key: str = "", model_key: str = "") -> Path:
    payload = {
        "version": 1,
        "search": {
            "year_from": 2023,
            "year_to": 2026,
            "sources": {"openalex": True, "scopus": False, "wos": False},
            "api_keys": {"pubmed": "", "scopus": scopus_key, "wos": ""},
        },
        "download": {"output_root": str(TMP / "paperdown")},
        "llm": {
            "enabled": True,
            "default_model": "deepseek-flash",
            "providers": [{"name": "deepseek", "type": "deepseek", "base_url": "https://api.deepseek.com", "api_key": ""}],
            "models": [
                {"name": "deepseek-flash", "provider": "deepseek", "model": "deepseek-flash", "api_key": model_key}
            ],
        },
        "journals": [{"name": "Scientific Reports", "issn": ["2045-2322"]}],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_update_source_and_model_keys() -> None:
    path = write_config(TMP / "cfg1.json")
    result = update_config_file(
        path,
        source_keys={"scopus": "sk-scopus-secret", "wos": "sk-wos-secret"},
        model_keys={"deepseek-flash": "sk-llm-secret"},
    )
    assert "search.api_keys.scopus" in result.changed
    assert "llm.models[deepseek-flash].api_key" in result.changed
    assert Path(result.backup_path).exists(), "应当留下备份"

    config = load_config(path)
    assert config.search.api_keys["scopus"] == "sk-scopus-secret"
    assert config.search.api_keys["wos"] == "sk-wos-secret"
    assert config.llm.models[0].api_key == "sk-llm-secret"
    # 未点名的字段原样保留
    assert config.search.sources["openalex"] is True
    assert config.journals[0].name == "Scientific Reports"

    # 对外视图不能出现明文
    view = json.dumps(config.public_view(), ensure_ascii=False)
    assert "sk-scopus-secret" not in view and "sk-llm-secret" not in view
    assert config.public_view()["search"]["api_key_configured"]["scopus"] is True


def test_update_clears_key_with_empty_string() -> None:
    path = write_config(TMP / "cfg2.json", scopus_key="sk-old")
    update_config_file(path, source_keys={"scopus": ""})
    config = load_config(path)
    assert config.search.api_keys["scopus"] == ""
    assert config.public_view()["search"]["api_key_configured"]["scopus"] is False


def test_update_adds_new_provider_and_model_key() -> None:
    path = write_config(TMP / "cfg3.json")
    update_config_file(
        path,
        new_provider={"name": "my-endpoint", "type": "openai-compatible", "base_url": "https://x.example.com/v1", "api_key": "sk-p"},
        model_keys={"my-model": "sk-m"},
    )
    config = load_config(path)
    provider_names = [item.name for item in config.llm.providers]
    assert "my-endpoint" in provider_names
    assert any(item.name == "my-model" for item in config.llm.models)


def test_update_emails() -> None:
    path = write_config(TMP / "cfg4.json")
    update_config_file(path, email="me@example.com", unpaywall_email="me@example.com")
    config = load_config(path)
    assert config.search.email == "me@example.com"
    assert config.search.unpaywall_email == "me@example.com"


def test_update_rejects_empty_change_set() -> None:
    path = write_config(TMP / "cfg5.json")
    try:
        update_config_file(path)
    except ConfigWriteError as exc:
        assert "没有需要修改" in str(exc)
    else:
        raise AssertionError("空改动应当报错")


def test_update_rolls_back_on_invalid_result() -> None:
    """写前校验：如果写出来的配置无法解析，应当回滚并报错。"""
    path = TMP / "cfg6.json"
    path.write_text(
        json.dumps(
            {
                "search": {"api_keys": "not-an-object"},
                "llm": {"models": []},
                "journals": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    before = path.read_text(encoding="utf-8")
    try:
        update_config_file(path, email="x@example.com")
    except ConfigWriteError:
        pass
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"应当抛 ConfigWriteError，实际是 {type(exc).__name__}") from exc
    else:
        raise AssertionError("非法配置应当报错")
    assert path.read_text(encoding="utf-8") == before, "校验失败时必须回滚原文件"


# ------------------------------------------------------------------ API 层


def test_update_source_toggle() -> None:
    """配置弹窗里的「保存并启用 / 停用该渠道」只切换开关，不动 key。"""
    path = write_config(TMP / "cfg_toggle.json", scopus_key="sk-keep-me")
    result = update_config_file(path, sources={"scopus": True, "wos": True})
    assert "search.sources.scopus" in result.changed and "search.sources.wos" in result.changed
    config = load_config(path)
    assert config.search.sources["scopus"] is True and config.search.sources["wos"] is True
    assert config.search.api_keys["scopus"] == "sk-keep-me", "切换开关不应清掉已存的 key"

    update_config_file(path, sources={"scopus": False, "wos": False})
    config = load_config(path)
    assert config.search.sources["scopus"] is False and config.search.sources["wos"] is False
    assert config.search.api_keys["scopus"] == "sk-keep-me"


def test_source_catalog_marks_optional() -> None:
    """Scopus/WoS 是可选渠道：缺 key 不代表配置出错，只是暂不可用。"""
    from app.query_scope import source_catalog

    catalog = {item["name"]: item for item in source_catalog({})}
    assert catalog["scopus"]["optional"] is True and catalog["scopus"]["available"] is False
    assert catalog["wos"]["optional"] is True and catalog["wos"]["available"] is False
    assert catalog["openalex"]["optional"] is True and catalog["openalex"]["available"] is True
    with_key = {item["name"]: item for item in source_catalog({"scopus": "k"})}
    assert with_key["scopus"]["available"] is True


def test_api_config_edit_sources_toggle() -> None:
    from starlette.testclient import TestClient

    from app.server import create_app

    path = write_config(TMP / "cfg_api_toggle.json")
    client = TestClient(create_app(path))
    response = client.post(
        "/api/config/edit",
        json={"source_keys": {"scopus": "sk-s"}, "sources": {"scopus": True}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "search.sources.scopus" in body["changed"] and "search.api_keys.scopus" in body["changed"]
    assert "sk-s" not in response.text
    catalog = {item["name"]: item for item in body["config"]["search"]["source_catalog"]}
    assert catalog["scopus"]["available"] is True
    assert body["config"]["search"]["sources"]["scopus"] is True
    assert load_config(path).search.sources["scopus"] is True

    off = client.post("/api/config/edit", json={"sources": {"scopus": False}})
    assert off.status_code == 200
    assert load_config(path).search.sources["scopus"] is False


def test_api_config_edit_endpoint() -> None:
    from starlette.testclient import TestClient

    from app.server import create_app

    path = write_config(TMP / "cfg_api.json", scopus_key="sk-old")
    client = TestClient(create_app(path))

    before = client.get("/api/config").json()["config"]["search"]["source_catalog"]
    scopus_before = next(item for item in before if item["name"] == "scopus")
    assert scopus_before["has_key"] is True and scopus_before["available"] is True

    response = client.post("/api/config/edit", json={"source_keys": {"wos": "sk-wos-new"}})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert "sk-wos-new" not in response.text, "接口返回不能回显密钥"
    after = body["config"]["search"]["source_catalog"]
    wos_after = next(item for item in after if item["name"] == "wos")
    assert wos_after["has_key"] is True and wos_after["available"] is True
    # 重新读盘也应生效
    assert load_config(path).search.api_keys["wos"] == "sk-wos-new"

    bad = client.post("/api/config/edit", json={})
    assert bad.status_code == 400

    # 写入后不会把密钥带进运行快照
    snapshot_path = TMP / "snapshot_check"
    snapshot_path.mkdir(parents=True, exist_ok=True)
    from app.runs import RunStore

    config = load_config(path)
    store = RunStore(config)
    run = store.create_run(
        keywords="x", groups=[["x"]], query_text="x", year_from=2023, year_to=2026, sources=["openalex"]
    )
    store.save_run(run)
    snapshot = (run.directory / "config.snapshot.json").read_text(encoding="utf-8")
    assert "sk-wos-new" not in snapshot and "sk-old" not in snapshot


def test_full_pipeline_with_scopus_and_wos() -> None:
    """把两个新渠道接进完整检索流程：检索式 → 解析 → 白名单过滤 → 三份交付文件。"""
    import importlib
    import os

    from starlette.testclient import TestClient

    from app import query_scope
    from app.sources import base as sources_base

    server, base_url = start_fake()
    os.environ["LITLOADER_SCOPUS_ENDPOINT"] = base_url + "/content/search/scopus"
    os.environ["LITLOADER_WOS_ENDPOINT"] = base_url + "/apis/wos-starter/v1/documents"
    try:
        # 端点常量在模块导入时读取环境变量，这里重新导入一次拿到假地址
        importlib.reload(sources_base)
        scopus_module = importlib.reload(scopus)
        wos_module = importlib.reload(wos)
        runner = importlib.reload(importlib.import_module("app.sources.runner"))
        assert scopus_module.API_URL.startswith(base_url)
        assert wos_module.API_URL.startswith(base_url)

        from app.server import create_app

        from app.analysis import apply_whitelist, merge_records
        from app.config import load_config
        from app.journal_match import JournalMatcher
        from app.report import generate_all

        path = TMP / "cfg_pipeline.json"
        payload = {
            "version": 1,
            "search": {
                "year_from": 2023,
                "year_to": 2026,
                "max_results_per_source": 5,
                "scope": "title_abstract",
                "sources": {
                    "openalex": False,
                    "crossref": False,
                    "europepmc": False,
                    "pubmed": False,
                    "scopus": True,
                    "wos": True,
                },
                "api_keys": {"scopus": "scopus-key", "wos": "wos-key"},
            },
            "download": {"output_root": str(TMP / "pipeline_runs")},
            "llm": {"enabled": False},
            "journals": [
                {"name": "Environmental Research Letters", "issn": ["1748-9326"], "indexing": "SCIE"},
                {"name": "Journal of Cleaner Production", "issn": ["0959-6526"], "indexing": "SCIE"},
                {"name": "Nature Climate Change", "issn": ["1758-678X"]},
            ],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        client = TestClient(create_app(path))
        response = client.post(
            "/api/search",
            json={"keywords": "large language model|LLM; climate|sustainability", "sources": None},
        )
        assert response.status_code == 200, response.text
        run = response.json()["run"]
        stats = run["stats"]
        assert stats["by_source"]["scopus"] == 2, stats["by_source"]
        assert stats["by_source"]["wos"] == 2, stats["by_source"]
        assert stats["raw"] == 4
        assert len(response.json()["candidates"]) == 3, "三个期刊在白名单内，Nature Climate Change 也应在内"
        assert all(item["matched_by"] for item in response.json()["candidates"])
        journals = {item["journal"] for item in response.json()["candidates"]}
        assert "Environmental Research Letters" in journals
        assert "Journal of Cleaner Production" in journals

        # 报告里应写明两个新渠道的实际检索式
        queries = stats["source_queries"]
        assert "TITLE(" in queries["scopus"] and "PUBYEAR" in queries["scopus"]
        assert "TI=" in queries["wos"]
        run_dir = Path(run["directory"])
        assert (run_dir / "下载报告.html").exists()
        assert (run_dir / "文章地址总表.csv").exists()
        assert (run_dir / "文献清单.md").exists()
    finally:
        os.environ.pop("LITLOADER_SCOPUS_ENDPOINT", None)
        os.environ.pop("LITLOADER_WOS_ENDPOINT", None)
        server.shutdown()
        importlib.reload(sources_base)
        importlib.reload(scopus)
        importlib.reload(wos)


def main() -> int:
    import traceback

    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    failures = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                func()
                print(f"PASS {name}")
            except Exception:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{'OK' if failures == 0 else f'{failures} failed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
