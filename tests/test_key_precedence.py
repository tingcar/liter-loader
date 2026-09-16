"""复现并验证 api_key / api_key_env 的解析优先级。

关键场景：config.json 里同时有
    "api_key": "sk-真实密钥",
    "api_key_env": "DEEPSEEK_API_KEY"
而环境变量**没有设置**时，密钥必须仍然取 "sk-真实密钥"。
早期实现会让未设置的环境变量把字面密钥覆盖成空字符串，于是
「在弹窗里保存 key 成功 → 立刻测试连接却报没有 api_key」。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config  # noqa: E402
from app.config_store import update_config_file  # noqa: E402

WORKSPACE = Path(__file__).resolve().parent.parent
TMP = WORKSPACE / ".tmp_key_precedence"
ENV_NAME = "LITLOADER_TEST_LLM_KEY"


def write_config(path: Path, api_key: str = "", api_key_env: str = "") -> Path:
    payload = {
        "version": 1,
        "search": {
            "year_from": 2023,
            "year_to": 2026,
            "sources": {"openalex": True},
            "api_keys": {"scopus": "", "wos": ""},
        },
        "download": {"output_root": str(TMP / "paperdown")},
        "llm": {
            "enabled": True,
            "default_model": "deepseek-flash",
            "providers": [{"name": "deepseek", "type": "deepseek", "base_url": "https://api.deepseek.com", "api_key": ""}],
            "models": [
                {
                    "name": "deepseek-flash",
                    "provider": "deepseek",
                    "model": "deepseek-flash",
                    "api_key": api_key,
                    "api_key_env": api_key_env,
                }
            ],
        },
        "journals": [{"name": "Scientific Reports", "issn": ["2045-2322"]}],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_literal_key_wins_over_unset_env() -> None:
    """环境变量不存在时，字面 key 必须保留（这正是弹窗保存后立刻可用的前提）。"""
    os.environ.pop(ENV_NAME, None)
    path = write_config(TMP / "literal_only.json", api_key="sk-literal-key", api_key_env=ENV_NAME)
    config = load_config(path)
    model = config.llm.models[0]
    assert model.api_key == "sk-literal-key", f"字面 key 被覆盖成了 {model.api_key!r}"
    assert model.has_api_key is True
    assert model.key_source == "config.json（api_key）"


def test_env_used_only_when_literal_missing() -> None:
    os.environ[ENV_NAME] = "sk-from-env"
    try:
        with_env_only = write_config(TMP / "env_only.json", api_key="", api_key_env=ENV_NAME)
        model = load_config(with_env_only).llm.models[0]
        assert model.api_key == "sk-from-env"
        assert model.key_source.startswith("环境变量")

        both = write_config(TMP / "both.json", api_key="sk-literal", api_key_env=ENV_NAME)
        model = load_config(both).llm.models[0]
        assert model.api_key == "sk-literal", "同时存在时应优先字面 key"
        assert model.key_source == "config.json（api_key）"
    finally:
        os.environ.pop(ENV_NAME, None)


def test_saved_key_survives_reload() -> None:
    """完整复现用户路径：写回 key → 重新读盘 → 必须能拿到密钥。"""
    os.environ.pop(ENV_NAME, None)
    path = write_config(TMP / "saved.json", api_key="", api_key_env=ENV_NAME)
    assert load_config(path).llm.models[0].has_api_key is False

    update_config_file(path, model_keys={"deepseek-flash": "sk-just-saved"})
    config = load_config(path)
    model = config.llm.models[0]
    assert model.api_key == "sk-just-saved", "保存后重新读盘必须能拿到密钥"
    assert model.has_api_key is True
    # 对外视图仍不泄露明文
    assert "sk-just-saved" not in json.dumps(config.public_view(), ensure_ascii=False)
    assert config.public_view()["llm"]["models"][0]["has_api_key"] is True


def test_provider_key_fallback_still_works() -> None:
    os.environ.pop(ENV_NAME, None)
    path = write_config(TMP / "provider_fallback.json", api_key="")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["llm"]["providers"][0]["api_key"] = "sk-provider-level"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    from app.llm import effective_api_key

    config = load_config(path)
    model = config.llm.models[0]
    assert model.api_key == ""
    assert effective_api_key(model, config.llm.providers[0]) == "sk-provider-level"


def test_env_prefix_form_works() -> None:
    """api_key: "env:XXX" 这种写法仍应生效。"""
    os.environ[ENV_NAME] = "sk-prefixed-env"
    try:
        path = write_config(TMP / "env_prefix.json", api_key=f"env:{ENV_NAME}")
        model = load_config(path).llm.models[0]
        assert model.api_key == "sk-prefixed-env"
        assert model.key_source.startswith("环境变量")
    finally:
        os.environ.pop(ENV_NAME, None)


def test_api_save_then_test_uses_saved_key() -> None:
    """接口层：保存 key 后 /api/llm/models 与 /api/llm/test 都应当认为 key 已配置。"""
    from starlette.testclient import TestClient

    from app.server import create_app

    os.environ.pop(ENV_NAME, None)
    path = write_config(TMP / "api_flow.json", api_key="", api_key_env=ENV_NAME)
    client = TestClient(create_app(path))

    assert client.get("/api/llm/models").json()["ready"] is False
    saved = client.post("/api/config/edit", json={"model_keys": {"deepseek-flash": "sk-api-flow"}})
    assert saved.status_code == 200, saved.text
    assert "sk-api-flow" not in saved.text

    models = client.get("/api/llm/models").json()
    assert models["models"][0]["has_api_key"] is True, "保存后应立刻认为 key 已配置"
    assert models["ready"] is True

    # 指向一个必然连不上的地址：应当报「无法连接」，而不是「没有配置 api_key」
    detail = client.post("/api/llm/test", json={"model": "deepseek-flash"}).json().get("detail", "")
    assert "api_key" not in detail, f"不应该再报缺少 api_key：{detail}"


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
