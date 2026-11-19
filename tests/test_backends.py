from __future__ import annotations

import json

import httpx
import pytest

from spanql.config import ModelConfig
from spanql.harness.backends import (
    BackendError,
    FakeBackend,
    LlamaCppBackend,
    OllamaBackend,
    OpenAICompatBackend,
    _unwrap,
    make_backend,
)

MSGS = [{"role": "user", "content": "q"}]


def mock(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_llamacpp_sends_grammar_and_disables_thinking() -> None:
    seen = {}

    def h(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": 'NOT_FOUND("x")'}}]})

    be = LlamaCppBackend(ModelConfig(endpoint="http://h:1/v1"), client=mock(h))
    out = be.generate(
        MSGS, grammar="root ::= x", question="q", handles={}, hints=[], temperature=0.3
    )
    assert out.text == 'NOT_FOUND("x")'
    assert seen["url"] == "http://h:1/v1/chat/completions"
    assert seen["body"]["grammar"] == "root ::= x"
    assert seen["body"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert seen["body"]["temperature"] == 0.3
    assert "response_format" not in seen["body"]


def test_openai_compat_uses_json_schema() -> None:
    seen = {}

    def h(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers.get("authorization")
        content = json.dumps({"formula": 'NOT_FOUND("y")'})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    cfg = ModelConfig(backend="openai-compatible", endpoint="http://v:8000/v1", api_key="k")
    be = OpenAICompatBackend(cfg, client=mock(h))
    be.client.headers["Authorization"] = "Bearer k"
    out = be.generate(MSGS, grammar=None, question="q", handles={}, hints=[])
    assert seen["body"]["response_format"]["type"] == "json_schema"
    assert "grammar" not in seen["body"]
    assert out.text == 'NOT_FOUND("y")'


def test_ollama_format_and_url() -> None:
    seen = {}

    def h(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        return httpx.Response(
            200, json={"message": {"content": '{"formula": "NOT_FOUND(\\"z\\")"}'}}
        )

    be = OllamaBackend(ModelConfig(backend="ollama", endpoint="http://o:11434/v1"), client=mock(h))
    out = be.generate(MSGS, grammar=None, question="q", handles={}, hints=[])
    assert seen["url"] == "http://o:11434/api/chat"
    assert seen["body"]["format"]["properties"]["formula"]
    assert seen["body"]["think"] is False
    assert out.text == 'NOT_FOUND("z")'


def test_http_errors_become_backend_errors() -> None:
    be = LlamaCppBackend(ModelConfig(), client=mock(lambda r: httpx.Response(500, text="boom")))
    with pytest.raises(BackendError, match="500"):
        be.generate(MSGS, grammar=None, question="q", handles={}, hints=[])

    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    be = LlamaCppBackend(ModelConfig(), client=mock(down))
    with pytest.raises(BackendError, match="unreachable"):
        be.generate(MSGS, grammar=None, question="q", handles={}, hints=[])
    assert be.health()["ok"] is False


def test_relevance_check_parses_yes_no() -> None:
    be = LlamaCppBackend(
        ModelConfig(),
        client=mock(
            lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "yes"}}]})
        ),
    )
    assert be.relevant("q", "p") is True


@pytest.mark.parametrize(
    "raw,want",
    [
        ('{"formula": "ANSWER(x)"}', "ANSWER(x)"),
        ("```\nANSWER(x)\n```", "ANSWER(x)"),
        ("<think>hmm</think>\nANSWER(x)", "ANSWER(x)"),
        ('Sure! NOT_FOUND("n")', 'NOT_FOUND("n")'),
    ],
)
def test_unwrap(raw: str, want: str) -> None:
    assert _unwrap(raw) == want


def test_make_backend() -> None:
    assert isinstance(make_backend(ModelConfig(backend="fake")), FakeBackend)
    assert isinstance(make_backend(ModelConfig(backend="vllm")), OpenAICompatBackend)
    assert isinstance(make_backend(ModelConfig(backend="llama.cpp")), LlamaCppBackend)
    with pytest.raises(ValueError):
        make_backend(ModelConfig(backend="nope"))
