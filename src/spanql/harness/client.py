"""Model clients. The harness talks to any OpenAI-compatible chat endpoint (ADR-0004).

``llama.cpp`` backends get the per-request GBNF grammar in the ``grammar`` field, so
invalid tokens are masked at decode time. Other backends work without it; the
verifier simply costs them more retries (MODEL.md section 6).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Callable, Protocol

USER_AGENT = "spanql"


class ClientError(Exception):
    """The model endpoint could not be reached or returned something unusable."""


class ModelClient(Protocol):
    model_id: str

    def complete(self, messages: list[dict], *, grammar: str | None = None, max_tokens: int = 700) -> str: ...


class OpenAICompatClient:
    def __init__(
        self,
        endpoint: str,
        model: str = "spanql",
        *,
        backend: str = "llama.cpp",
        api_key: str | None = None,
        timeout: float = 60.0,
        thinking: bool = False,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.model_id = model
        self.backend = backend
        self.api_key = api_key
        self.timeout = timeout
        self.thinking = thinking

    def payload(self, messages: list[dict], grammar: str | None, max_tokens: int) -> dict:
        body: dict = {"model": self.model_id, "messages": messages, "temperature": 0, "max_tokens": max_tokens,
                      "stream": False}
        if grammar and self.backend == "llama.cpp":
            body["grammar"] = grammar
        if self.backend == "llama.cpp":
            body["chat_template_kwargs"] = {"enable_thinking": self.thinking}  # non-thinking: short, no hidden CoT
        return body

    def complete(self, messages: list[dict], *, grammar: str | None = None, max_tokens: int = 700) -> str:
        data = json.dumps(self.payload(messages, grammar, max_tokens)).encode()
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.endpoint + "/chat/completions", data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise ClientError(f"model endpoint returned HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise ClientError(f"cannot reach the model endpoint {self.endpoint}: {e}") from e
        except ValueError as e:
            raise ClientError(f"model endpoint returned invalid JSON: {e}") from e
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise ClientError(f"unexpected model response shape: {str(body)[:200]}") from e
        return content or ""


class ScriptedClient:
    """Deterministic stand-in for a model (tests, offline demos, ``selftest``).

    Each response is a string or a callable ``(messages, grammar) -> str``. Calls are recorded.
    """

    def __init__(self, responses: list[str | Callable[[list[dict], str | None], str]], model_id: str = "scripted"):
        self.responses = list(responses)
        self.model_id = model_id
        self.calls: list[dict] = []

    def complete(self, messages: list[dict], *, grammar: str | None = None, max_tokens: int = 700) -> str:
        self.calls.append({"messages": messages, "grammar": grammar, "max_tokens": max_tokens})
        if not self.responses:
            raise ClientError("scripted client has no more responses")
        r = self.responses.pop(0)
        return r(messages, grammar) if callable(r) else r


_FENCE = re.compile(r"^```[a-zA-Z]*\s*\n?|\n?```\s*$")
_START = re.compile(r"\b(ANSWER|NOT_FOUND|AMBIGUOUS)\s*\(")


def extract_formula(raw: str) -> str:
    """Pull the formula out of a model reply (strips code fences and chatter before it)."""
    text = raw.strip()
    text = _FENCE.sub("", text).strip()
    m = _START.search(text)
    return text[m.start():].strip() if m else text
