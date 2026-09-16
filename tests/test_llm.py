"""LLM 关键词扩展的单元测试与 mock 端到端测试（不需要真实 API key）。

覆盖：
1. 返回解析：分组 JSON / 扁平列表 / 纯文本兜底 / 脏词清洗；
2. 提示词：包含概念块下标、白名单期刊、检索范围；
3. 请求体：DeepSeek 的 thinking 开关、json_object、temperature 处理；
4. 密钥处理：不在 public_view/sanitized/错误信息里泄露；
5. 降级：未配置 key / 未知模型 / 服务报错时给出可读错误；
6. mock 端到端：起一个假的 OpenAI 兼容服务，跑通 /api/llm/test、
   /api/expand-keywords、以及把 LLM 关键词合并进 /api/search。
"""

from __future__ import annotations

import json
import shutil
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import (  # noqa: E402
    LLMConfig,
    LLMModelConfig,
    LLMProviderConfig,
    load_config,
)
from app.llm import (  # noqa: E402
    LLMError,
    build_request_payload,
    build_user_prompt,
    cap_total_terms,
    chat_endpoint,
    effective_base_url,
    expand_keywords,
    parse_expansion_payload,
    resolve_model,
    test_connection as check_connection,
)

WORKSPACE = Path(__file__).resolve().parent.parent
TMP = WORKSPACE / ".tmp_llm_tests"

GROUP_JSON = json.dumps(
    {
        "groups": [
            {"index": 0, "terms": ["LLM", "GPT-4", "generative AI", "foundation model"], "reason": "模型简称"},
            {"index": 1, "terms": ["climate change", "carbon emission"], "reason": "环境词"},
        ]
    },
    ensure_ascii=False,
)


# ------------------------------------------------------------------ 解析


def make_llm_config(**overrides: object) -> LLMConfig:
    model = LLMModelConfig(
        name="mock-model",
        provider="mock",
        model="mock-model",
        api_key="sk-test-secret",
        base_url="",
        temperature=None,
        max_tokens=512,
        json_mode=True,
        thinking="disabled",
    )
    provider = LLMProviderConfig(name="mock", type="openai-compatible", base_url="http://127.0.0.1:1", api_key="")
    config = LLMConfig(enabled=True, default_model="mock-model", providers=[provider], models=[model], timeout_seconds=5)
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def test_parse_grouped_json() -> None:
    groups = [["large language model"], ["environment"]]
    parsed = parse_expansion_payload(GROUP_JSON, 2, groups)
    assert len(parsed) == 2
    assert parsed[0].llm_terms == ["LLM", "GPT-4", "generative AI", "foundation model"]
    assert parsed[1].llm_terms == ["climate change", "carbon emission"]
    assert parsed[0].user_terms == ["large language model"]
    assert parsed[0].reason == "模型简称"
    assert parsed[0].all_terms[0] == "large language model"


def test_parse_skips_user_duplicates() -> None:
    payload = json.dumps({"groups": [{"index": 0, "terms": ["LLM", "llm", "ChatGPT"]}]})
    parsed = parse_expansion_payload(payload, 1, [["LLM", "ChatGPT"]])
    assert parsed[0].llm_terms == []


def test_parse_flat_list_round_robin() -> None:
    payload = json.dumps({"keywords": ["a", "b", "c", "d"]})
    parsed = parse_expansion_payload(payload, 2, [[], []])
    assert parsed[0].llm_terms == ["a", "c"]
    assert parsed[1].llm_terms == ["b", "d"]


def test_parse_plain_text_fallback() -> None:
    text = "0: LLM, GPT\n1: climate change, sustainability\n"
    parsed = parse_expansion_payload(text, 2, [[], []])
    assert parsed[0].llm_terms == ["LLM", "GPT"]
    assert parsed[1].llm_terms == ["climate change", "sustainability"]


def test_parse_strips_boolean_syntax_and_long_terms() -> None:
    payload = json.dumps({"groups": [{"index": 0, "terms": ["(LLM OR GPT)", '"quoted"', "x" * 200, "ok term"]}]})
    parsed = parse_expansion_payload(payload, 1, [[]])
    assert parsed[0].llm_terms == ["LLM GPT", "quoted", "ok term"]


def test_parse_missing_group_filled_with_user_terms() -> None:
    payload = json.dumps({"groups": [{"index": 0, "terms": ["a"]}]})
    parsed = parse_expansion_payload(payload, 2, [["u1"], ["u2"]])
    assert parsed[1].llm_terms == []
    assert parsed[1].user_terms == ["u2"]


def test_parse_empty_content_raises() -> None:
    for content in ("", "   "):
        try:
            parse_expansion_payload(content, 1, [[]])
        except LLMError as exc:
            assert "空内容" in str(exc)
        else:
            raise AssertionError("空内容应当报错")


def test_parse_non_json_text_raises() -> None:
    try:
        parse_expansion_payload("抱歉，我无法完成这个请求。", 1, [[]])
    except LLMError as exc:
        assert "无法解析" in str(exc) or "空内容" in str(exc)
    else:
        raise AssertionError("无法解析的内容应当报错")


def test_cap_total_terms() -> None:
    from app.llm import ExpandedGroup

    groups = [ExpandedGroup(0, [], [f"t{i}" for i in range(10)]), ExpandedGroup(1, [], [f"u{i}" for i in range(10)])]
    capped = cap_total_terms(groups, limit=12)
    assert sum(len(group.llm_terms) for group in capped) == 12
    assert len(capped[0].llm_terms) == 10


def test_max_terms_per_group_limit() -> None:
    payload = json.dumps({"groups": [{"index": 0, "terms": [f"term {i}" for i in range(40)]}]})
    parsed = parse_expansion_payload(payload, 1, [[]])
    assert len(parsed[0].llm_terms) == 10


# ------------------------------------------------------------------ 提示词


def test_prompt_includes_groups_journals_and_scope() -> None:
    prompt = build_user_prompt(
        [["LLM"], ["climate"]],
        topic="LLM 环境应用",
        journals=["Scientific Reports", "PLOS ONE"],
        scope_label="标题 + 摘要",
        scope_fields="标题 + 摘要",
        language_hint="以英文为主",
    )
    assert "index 0" in prompt and "index 1" in prompt
    assert "Scientific Reports" in prompt
    assert "标题 + 摘要" in prompt
    assert "以英文为主" in prompt
    assert "JSON" in prompt


# ------------------------------------------------------------------ 请求体


def test_chat_endpoint_variants() -> None:
    assert chat_endpoint("https://api.deepseek.com") == "https://api.deepseek.com/chat/completions"
    assert chat_endpoint("https://api.deepseek.com/v1") == "https://api.deepseek.com/v1/chat/completions"
    assert chat_endpoint("http://x/chat/completions") == "http://x/chat/completions"
    assert chat_endpoint("http://x/") == "http://x/chat/completions"


def test_payload_deepseek_thinking_default_disabled() -> None:
    config = make_llm_config()
    model, provider = config.models[0], config.providers[0]
    provider.base_url = "https://api.deepseek.com"
    payload = build_request_payload(model, provider, [{"role": "user", "content": "hi"}])
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["response_format"] == {"type": "json_object"}
    assert "temperature" not in payload  # 未设置时不传


def test_payload_deepseek_thinking_enabled_with_effort() -> None:
    config = make_llm_config()
    model, provider = config.models[0], config.providers[0]
    provider.base_url = "https://api.deepseek.com"
    model.thinking = "enabled"
    model.reasoning_effort = "high"
    model.temperature = 0.7
    payload = build_request_payload(model, provider, [{"role": "user", "content": "hi"}])
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "high"
    assert "temperature" not in payload  # 思考模式下 temperature 无效，不传


def test_payload_non_deepseek_has_no_thinking_field() -> None:
    config = make_llm_config()
    model, provider = config.models[0], config.providers[0]
    provider.base_url = "https://my-llm.example.com/v1"
    model.temperature = 0.2
    payload = build_request_payload(model, provider, [{"role": "user", "content": "hi"}])
    assert "thinking" not in payload
    assert payload["temperature"] == 0.2


def test_payload_json_mode_can_be_disabled() -> None:
    config = make_llm_config()
    model, provider = config.models[0], config.providers[0]
    model.json_mode = False
    payload = build_request_payload(model, provider, [{"role": "user", "content": "hi"}])
    assert "response_format" not in payload


def test_effective_base_url_prefers_model() -> None:
    model = LLMModelConfig(name="m", provider="p", base_url="https://model.example.com")
    provider = LLMProviderConfig(name="p", base_url="https://provider.example.com")
    assert effective_base_url(model, provider) == "https://model.example.com"
    model.base_url = ""
    assert effective_base_url(model, provider) == "https://provider.example.com"
    provider.base_url = ""
    assert effective_base_url(model, provider) == "https://api.deepseek.com"


# ------------------------------------------------------------------ 密钥与错误


def test_resolve_model_unknown_name() -> None:
    config = make_llm_config()
    try:
        resolve_model(config, "nope")
    except LLMError as exc:
        assert "nope" in str(exc) and "mock-model" in str(exc)
    else:
        raise AssertionError("未知模型应当报错")


def test_missing_api_key_error_has_no_secret() -> None:
    config = make_llm_config()
    config.models[0].api_key = ""
    try:
        expand_keywords([["LLM"]], config, model_name="mock-model")
    except LLMError as exc:
        message = str(exc)
        assert "api_key" in message
        assert "sk-" not in message
    else:
        raise AssertionError("缺少 key 应当报错")


def test_public_view_and_sanitized_hide_keys() -> None:
    config = make_llm_config()
    config.providers[0].api_key = "sk-provider-secret"
    config.models[0].api_key = "sk-model-secret"
    view = json.dumps(config.public_view(), ensure_ascii=False)
    assert "sk-model-secret" not in view
    assert "sk-provider-secret" not in view
    assert '"has_api_key": true' in view
    sanitized = json.dumps(config.sanitized(), ensure_ascii=False)
    assert "sk-" not in sanitized


# ------------------------------------------------------------------ mock 端到端


class MockLLMHandler(BaseHTTPRequestHandler):
    """假的 OpenAI 兼容端点：记录收到的请求，按 scenario 返回不同结果。"""

    scenario = "json"
    requests_log: list[dict] = []

    def log_message(self, *args: object) -> None:  # 静默
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        MockLLMHandler.requests_log.append(
            {"path": self.path, "body": body, "authorization": self.headers.get("Authorization")}
        )
        scenario = MockLLMHandler.scenario
        if scenario == "json":
            content = GROUP_JSON
            status = 200
            payload: dict = {
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140},
            }
        elif scenario == "bad_json":
            payload = {"choices": [{"message": {"content": "抱歉，我不能输出这个。"}}]}
            status = 200
        elif scenario == "http_error":
            payload = {"error": {"message": "Unauthorized: invalid api key"}}
            status = 401
        elif scenario == "empty":
            payload = {"choices": [{"message": {"content": ""}}]}
            status = 200
        else:
            payload = {"choices": [{"message": {"content": "{}"}}]}
            status = 200
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def start_mock_llm() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockLLMHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _write_config(path: Path, base_url: str, *, with_key: bool = True) -> Path:
    payload = {
        "version": 1,
        "search": {"year_from": 2023, "year_to": 2026, "max_results_per_source": 3, "scope": "title_abstract"},
        "download": {"output_root": str(TMP / "paperdown")},
        "llm": {
            "enabled": True,
            "auto_expand": False,
            "default_model": "mock-model",
            "timeout_seconds": 10,
            "providers": [{"name": "mock", "type": "openai-compatible", "base_url": base_url}],
            "models": [
                {
                    "name": "mock-model",
                    "model": "mock-model",
                    "provider": "mock",
                    "api_key": "sk-e2e-secret" if with_key else "",
                    "json_mode": True,
                }
            ],
        },
        "journals": [{"name": "Scientific Reports", "issn": ["2045-2322"]}],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_mock_connection_and_expansion() -> None:
    server, base_url = start_mock_llm()
    try:
        MockLLMHandler.scenario = "json"
        MockLLMHandler.requests_log.clear()
        config = load_config(_write_config(TMP / "mock_config.json", base_url))

        probe = check_connection(config.llm, "mock-model")
        assert probe["ok"] and probe["model"] == "mock-model"
        assert probe["endpoint"] == f"{base_url}/chat/completions"

        result = expand_keywords(
            [["large language model"], ["environment"]],
            config.llm,
            model_name="mock-model",
            journals=["Scientific Reports"],
            scope_label="标题 + 摘要",
        )
        assert result.model == "mock-model"
        assert result.llm_term_count == 6
        assert result.groups[0].llm_terms[0] == "LLM"
        assert result.usage.get("total_tokens") == 140

        # 认证头带上了 key，请求体是 OpenAI 兼容结构
        request = MockLLMHandler.requests_log[-1]
        assert request["authorization"] == "Bearer sk-e2e-secret"
        assert request["body"]["model"] == "mock-model"
        assert request["body"]["response_format"] == {"type": "json_object"}
        assert request["body"]["messages"][0]["role"] == "system"
        assert "index 0" in request["body"]["messages"][1]["content"]
    finally:
        server.shutdown()


def test_mock_error_scenarios() -> None:
    server, base_url = start_mock_llm()
    try:
        config = load_config(_write_config(TMP / "mock_config_err.json", base_url))
        for scenario, marker in (("bad_json", "无法解析"), ("http_error", "401"), ("empty", "空内容")):
            MockLLMHandler.scenario = scenario
            try:
                expand_keywords([["LLM"]], config.llm, model_name="mock-model")
            except LLMError as exc:
                message = str(exc)
                assert marker in message or "空内容" in message, f"{scenario}: {message}"
                assert "sk-e2e-secret" not in message, "错误信息里不能出现 key"
            else:
                raise AssertionError(f"场景 {scenario} 应当报错")
    finally:
        server.shutdown()


def test_api_endpoints_with_mock() -> None:
    from starlette.testclient import TestClient

    from app.server import create_app

    server, base_url = start_mock_llm()
    try:
        MockLLMHandler.scenario = "json"
        config_path = TMP / "mock_config_api.json"
        _write_config(config_path, base_url)
        client = TestClient(create_app(config_path))

        models = client.get("/api/llm/models").json()
        assert models["ready"] is True
        assert [m["name"] for m in models["models"]] == ["mock-model"]
        assert all("api_key" not in model for model in models["models"])
        assert models["models"][0]["has_api_key"] is True

        test_result = client.post("/api/llm/test", json={"model": "mock-model"})
        assert test_result.status_code == 200
        assert test_result.json()["ok"] is True

        expanded = client.post(
            "/api/expand-keywords",
            json={"keywords": "large language model|LLM; environment|climate", "model": "mock-model"},
        )
        assert expanded.status_code == 200, expanded.text
        body = expanded.json()
        # 用户已给出的 "LLM" 会被去重，所以新增词是 5 个
        assert body["llm_term_count"] == 5, body
        assert body["groups"][0]["llm_terms"] == ["GPT-4", "generative AI", "foundation model"]
        assert body["groups"][1]["llm_terms"] == ["climate change", "carbon emission"]
        assert body["original_groups"] == [["large language model", "LLM"], ["environment", "climate"]]
        assert "sk-e2e-secret" not in expanded.text

        # 用扩展结果发起检索：LLM 词会被合并进概念块并记录来源
        keyword_groups = []
        for group in body["groups"]:
            terms = [{"term": term, "source": "user"} for term in group["user_terms"]]
            terms += [{"term": term, "source": "llm"} for term in group["llm_terms"]]
            keyword_groups.append({"terms": terms})
        search = client.post(
            "/api/search",
            json={
                "keywords": "large language model|LLM; environment|climate",
                "keyword_groups": keyword_groups,
                "llm_model": "mock-model",
                "max_results_per_source": 3,
                "sources": {"openalex": False, "crossref": False, "europepmc": False, "pubmed": False},
            },
        )
        assert search.status_code == 200, search.text
        stats = search.json()["run"]["stats"]
        assert stats["llm_used"] is True
        assert stats["llm_model"] == "mock-model"
        assert "GPT-4" in stats["llm_terms"] and "foundation model" in stats["llm_terms"]
        assert "LLM" not in stats["llm_terms"], "用户已给出的词不应被算作 LLM 新增词"
        assert "large language model" in stats["user_terms"] and "LLM" in stats["user_terms"]
        assert stats["keyword_groups"][0]["terms"][0]["source"] == "user"
        run_id = search.json()["run"]["run_id"]
        snapshot = json.loads((Path(search.json()["run"]["directory"]) / "config.snapshot.json").read_text("utf-8"))
        assert "sk-e2e-secret" not in json.dumps(snapshot, ensure_ascii=False)
        assert snapshot["llm"]["models"] == ["mock-model"]
        assert client.get(f"/api/runs/{run_id}").status_code == 200
    finally:
        server.shutdown()


def test_api_degrades_without_key() -> None:
    from starlette.testclient import TestClient

    from app.server import create_app

    config_path = TMP / "mock_config_nokey.json"
    _write_config(config_path, "http://127.0.0.1:9", with_key=False)
    client = TestClient(create_app(config_path))

    models = client.get("/api/llm/models").json()
    assert models["ready"] is False

    response = client.post("/api/expand-keywords", json={"keywords": "LLM; climate"})
    assert response.status_code == 400
    assert "api_key" in response.json()["detail"]
    assert "sk-" not in response.text

    # LLM 不可用时，检索本身照常工作（纯用户关键词）
    search = client.post(
        "/api/search",
        json={
            "keywords": "large language model|LLM; climate",
            "sources": {"openalex": False, "crossref": False, "europepmc": False, "pubmed": False},
        },
    )
    assert search.status_code == 200, search.text
    assert search.json()["run"]["stats"]["llm_used"] is False


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
