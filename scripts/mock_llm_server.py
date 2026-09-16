"""本地假 LLM 端点：用于在没有真实 API key 的情况下验证「AI 关键词扩展」链路。

它实现 OpenAI 兼容的 /chat/completions，返回固定结构的概念块关键词。
只用于自测，不参与正式功能。

用法：
    python scripts/mock_llm_server.py --port 8899
然后把 config.json 里某个模型的 base_url 指向 http://127.0.0.1:8899，
api_key 随便填一个非空值，即可在网页上点「AI 扩展关键词」。
"""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

SYNONYMS: list[list[str]] = [
    ["LLM", "GPT-4", "generative AI", "foundation model", "large language models", "ChatGPT"],
    ["climate change", "carbon emission", "sustainability", "environmental impact", "net zero"],
]


def build_groups(group_count: int) -> list[dict[str, Any]]:
    groups = []
    for index in range(group_count):
        terms = SYNONYMS[index] if index < len(SYNONYMS) else [f"synonym {index}-{n}" for n in range(1, 6)]
        groups.append({"index": index, "terms": terms, "reason": f"概念块 {index} 的同义词示例"})
    return groups


class MockLLMHandler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        print(f"[mock-llm] {self.address_string()} {args[0] if args else ''}")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) or b"{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}
        messages = payload.get("messages") or []
        prompt = "\n".join(str(item.get("content") or "") for item in messages if isinstance(item, dict))
        group_count = len(re.findall(r"^- index \d+", prompt, re.M)) or 2
        content = json.dumps({"groups": build_groups(group_count)}, ensure_ascii=False)
        body = {
            "id": "mock-completion",
            "object": "chat.completion",
            "model": payload.get("model") or "mock-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 60, "total_tokens": 180},
        }
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main() -> None:
    parser = argparse.ArgumentParser(description="本地假 LLM 端点（仅用于自测）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), MockLLMHandler)
    print("=" * 60)
    print(f"假 LLM 端点已启动：http://{args.host}:{args.port}/chat/completions")
    print("把 config.json 里模型的 base_url 指到它即可测试关键词扩展。Ctrl+C 停止。")
    print("=" * 60)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
