"""Model backends. All real backends speak HTTP to an external runtime (ADR-0004).

- ``llama.cpp``: OpenAI-compatible ``/v1/chat/completions`` with a per-request GBNF ``grammar``.
- ``ollama``: native ``/api/chat`` with a JSON-schema ``format`` envelope ``{"formula": "..."}``
  (Ollama documents JSON-schema structured outputs, not GBNF), validated by the parser/verifier.
- ``openai-compatible``: any server; JSON-schema ``response_format`` envelope (vLLM, LM Studio…).
- ``fake``: deterministic, offline, rule-based formula writer for tests, demos and CI.
- ``scripted``: replays given formulas (tests).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from spanql.config import ModelConfig
from spanql.engine import extract as ex
from spanql.engine.handles import HandleTable
from spanql.harness.prompts import RELEVANCE_PROMPT
from spanql.retrieve.retriever import keywords

FORMULA_SCHEMA = {
    "type": "object",
    "properties": {"formula": {"type": "string"}},
    "required": ["formula"],
    "additionalProperties": False,
}


class BackendError(RuntimeError):
    pass


@dataclass
class Generation:
    text: str
    raw: Any = None
    usage: dict[str, Any] | None = None


class Backend(Protocol):
    name: str
    model: str
    supports_grammar: bool

    def generate(
        self,
        messages: list[dict[str, str]],
        *,
        grammar: str | None,
        question: str,
        handles: HandleTable,
        hints: list[str],
        temperature: float | None = None,
    ) -> Generation: ...

    def relevant(self, question: str, passage: str) -> bool: ...

    def health(self) -> dict[str, Any]: ...


def _unwrap(text: str) -> str:
    """Extract the formula from a JSON envelope or a code fence, if present."""
    t = text.strip()
    if t.startswith("{"):
        try:
            obj = json.loads(t)
            if isinstance(obj, dict) and isinstance(obj.get("formula"), str):
                return str(obj["formula"]).strip()
        except ValueError:
            pass
    t = re.sub(r"^<think>.*?</think>\s*", "", t, flags=re.S)
    m = re.search(r"```(?:\w+)?\s*(.*?)```", t, flags=re.S)
    if m:
        t = m.group(1).strip()
    m = re.search(r"\b(ANSWER|NOT_FOUND|AMBIGUOUS)\(", t)
    return t[m.start() :].strip() if m else t


class _HTTPBackend:
    supports_grammar = False

    def __init__(self, cfg: ModelConfig, client: httpx.Client | None = None):
        self.cfg = cfg
        self.model = cfg.model
        headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        self.client = client or httpx.Client(timeout=cfg.timeout_s, headers=headers)

    def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            r = self.client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise BackendError(f"model endpoint unreachable ({url}): {exc}") from exc
        if r.status_code >= 400:
            raise BackendError(f"model endpoint error {r.status_code}: {r.text[:300]}")
        data: dict[str, Any] = r.json()
        return data

    def relevant(self, question: str, passage: str) -> bool:
        msgs = [
            {"role": "system", "content": RELEVANCE_PROMPT},
            {"role": "user", "content": f"Question: {question}\nPassage: {passage[:1500]}"},
        ]
        out = self._chat(msgs, grammar='root ::= "yes" | "no"\n', max_tokens=3, schema=None)
        return out.strip().lower().startswith("y")

    def _chat(
        self,
        messages: list[dict[str, str]],
        *,
        grammar: str | None,
        max_tokens: int,
        schema: dict[str, Any] | None,
        temperature: float | None = None,
    ) -> str:
        raise NotImplementedError

    def generate(
        self,
        messages: list[dict[str, str]],
        *,
        grammar: str | None,
        question: str,
        handles: HandleTable,
        hints: list[str],
        temperature: float | None = None,
    ) -> Generation:
        text = self._chat(
            messages,
            grammar=grammar,
            max_tokens=512,
            schema=FORMULA_SCHEMA,
            temperature=temperature,
        )
        return Generation(_unwrap(text), raw=text)


class OpenAICompatBackend(_HTTPBackend):
    name = "openai-compatible"

    def base(self) -> str:
        return self.cfg.endpoint.rstrip("/")

    def _payload(
        self, messages: list[dict[str, str]], max_tokens: int, temperature: float | None = None
    ) -> dict[str, Any]:
        temp = self.cfg.temperature if temperature is None else temperature
        return {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": temp,
            "max_tokens": max_tokens,
            "stream": False,
            "seed": 7,
        }

    def _chat(
        self,
        messages: list[dict[str, str]],
        *,
        grammar: str | None,
        max_tokens: int,
        schema: dict[str, Any] | None,
        temperature: float | None = None,
    ) -> str:
        payload = self._payload(messages, max_tokens, temperature)
        if schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "formula", "schema": schema, "strict": True},
            }
        data = self._post(f"{self.base()}/chat/completions", payload)
        try:
            return str(data["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError) as exc:
            raise BackendError(f"unexpected response: {str(data)[:200]}") from exc

    def health(self) -> dict[str, Any]:
        try:
            r = self.client.get(f"{self.base()}/models", timeout=5)
            ok = r.status_code < 400
            models = [m.get("id") for m in r.json().get("data", [])] if ok else []
            return {"ok": ok, "backend": self.name, "endpoint": self.base(), "models": models}
        except (httpx.HTTPError, ValueError) as exc:
            return {"ok": False, "backend": self.name, "endpoint": self.base(), "error": str(exc)}


class LlamaCppBackend(OpenAICompatBackend):
    """llama.cpp ``llama-server``: GBNF via the ``grammar`` field on chat completions."""

    name = "llama.cpp"
    supports_grammar = True

    def _chat(
        self,
        messages: list[dict[str, str]],
        *,
        grammar: str | None,
        max_tokens: int,
        schema: dict[str, Any] | None,
        temperature: float | None = None,
    ) -> str:
        payload = self._payload(messages, max_tokens, temperature)
        if grammar:
            payload["grammar"] = grammar
        # Qwen3.x: non-thinking mode for short, direct output
        payload["chat_template_kwargs"] = {"enable_thinking": False}
        data = self._post(f"{self.base()}/chat/completions", payload)
        try:
            return str(data["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError) as exc:
            raise BackendError(f"unexpected response: {str(data)[:200]}") from exc

    def health(self) -> dict[str, Any]:
        root = self.base().removesuffix("/v1")
        try:
            r = self.client.get(f"{root}/health", timeout=5)
            return {
                "ok": r.status_code == 200,
                "backend": self.name,
                "endpoint": self.base(),
                "status": r.json().get("status") if r.status_code < 500 else r.text[:100],
            }
        except (httpx.HTTPError, ValueError) as exc:
            return {"ok": False, "backend": self.name, "endpoint": self.base(), "error": str(exc)}


class OllamaBackend(_HTTPBackend):
    name = "ollama"

    def base(self) -> str:
        return self.cfg.endpoint.rstrip("/").removesuffix("/v1")

    def _chat(
        self,
        messages: list[dict[str, str]],
        *,
        grammar: str | None,
        max_tokens: int,
        schema: dict[str, Any] | None,
        temperature: float | None = None,
    ) -> str:
        temp = self.cfg.temperature if temperature is None else temperature
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "stream": False,
            "think": False,
            "options": {"temperature": temp, "num_predict": max_tokens, "seed": 7},
        }
        if schema is not None:
            payload["format"] = schema
        data = self._post(f"{self.base()}/api/chat", payload)
        try:
            return str(data["message"]["content"] or "")
        except KeyError as exc:
            raise BackendError(f"unexpected response: {str(data)[:200]}") from exc

    def health(self) -> dict[str, Any]:
        try:
            r = self.client.get(f"{self.base()}/api/tags", timeout=5)
            models = [m.get("name") for m in r.json().get("models", [])]
            return {
                "ok": r.status_code == 200,
                "backend": self.name,
                "endpoint": self.base(),
                "models": models,
                "model_present": self.cfg.model in models,
            }
        except (httpx.HTTPError, ValueError) as exc:
            return {"ok": False, "backend": self.name, "endpoint": self.base(), "error": str(exc)}


class ScriptedBackend:
    """Replays formulas in order (tests). A callable may compute the formula from inputs."""

    name = "scripted"
    model = "scripted"
    supports_grammar = True

    def __init__(self, outputs: Iterable[str | Callable[..., str]], relevant: bool = True):
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []
        self._relevant = relevant

    def generate(
        self,
        messages: list[dict[str, str]],
        *,
        grammar: str | None,
        question: str,
        handles: HandleTable,
        hints: list[str],
        temperature: float | None = None,
    ) -> Generation:
        self.calls.append({"messages": messages, "grammar": grammar, "handles": dict(handles)})
        if not self.outputs:
            raise BackendError("scripted backend exhausted")
        out = self.outputs.pop(0)
        text = out(question=question, handles=handles, hints=hints) if callable(out) else out
        return Generation(text)

    def relevant(self, question: str, passage: str) -> bool:
        return self._relevant

    def health(self) -> dict[str, Any]:
        return {"ok": True, "backend": self.name}


class FakeBackend:
    """Deterministic rule-based formula writer: no model, no network.

    It exists so the whole pipeline can be exercised offline (CI, `spanql selftest`, demos).
    It is intentionally simple; it is not a stand-in for model quality.
    """

    name = "fake"
    model = "fake-rules-v1"
    supports_grammar = True

    def generate(
        self,
        messages: list[dict[str, str]],
        *,
        grammar: str | None,
        question: str,
        handles: HandleTable,
        hints: list[str],
        temperature: float | None = None,
    ) -> Generation:
        return Generation(self.write(question, handles, hints, messages))

    def relevant(self, question: str, passage: str) -> bool:
        return True

    def health(self) -> dict[str, Any]:
        return {"ok": True, "backend": self.name, "note": "offline rule-based writer"}

    # --------------------------------------------------------------- rules
    def write(
        self,
        question: str,
        handles: HandleTable,
        hints: list[str],
        messages: list[dict[str, str]] | None = None,
    ) -> str:
        q = question.strip()
        ql = q.lower()
        retry = bool(messages and "PREVIOUS FORMULA WAS REJECTED" in messages[-1]["content"])
        words = [w for w in keywords(q) if not w[0].isdigit()]
        if not handles:
            return 'NOT_FOUND("No candidate passages matched the question.")'
        searched = ", ".join(f"${n}" for n in list(handles)[:5])

        m = re.search(
            r"(?i)which (?:functions?|methods?) (?:call|use|invoke)s? `?([A-Za-z_]\w*)`?", q
        )
        if m:
            name = m.group(1)
            return (
                f'ANSWER(DISTINCT(SYMBOLS(kind:"callable", calls:"{name}")), '
                f'COUNT(DISTINCT(SYMBOLS(kind:"callable", calls:"{name}"))), '
                f'text:"{{1}} callers of {name}: {{0}}.")'
            )

        if re.search(r"\b(how many|count|number of)\b", ql) and not retry:
            day = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", q)
            log_handles = [h for h in handles.values() if h.block_kind == "log_record"]
            if log_handles:
                term = self._rare_term(words, [h.text for h in log_handles])
                if term:
                    day_kw = f', day:"{day.group(1)}"' if day else ""
                    return (
                        f'ANSWER(COUNT(FIND(kind:"log", re:"{re.escape(term)}"{day_kw})), '
                        f'text:"{{0}} matching log records.")'
                    )

        # single value lookup: the best candidate with exactly the kind of value asked for
        want = (
            "money"
            if re.search(r"(?i)\b(revenue|cost|price|spend|expenses?|income|\$)", q)
            else "date"
            if re.search(r"(?i)\b(when|date|published|day)\b", q)
            else "pct"
            if re.search(r"(?i)\b(percent|rate|%)", q)
            else "num"
        )
        qwords = {w.lower() for w in words}
        best: tuple[int, int, str] | None = None
        for h in handles.values():
            overlap = len(qwords & {w.lower() for w in re.findall(r"[\w$.%]+", h.text)})
            sentences = re.split(r"(?<=[.;])\s+|\n", h.text)
            pieces = [p for snt in sentences for p in re.split(r"(?<=,)\s+", snt)]
            for sentence in dict.fromkeys(sentences + pieces):
                quote = self._value_quote(sentence, want)
                if quote is None:
                    continue
                s_overlap = len(qwords & {w.lower().strip(".,") for w in sentence.split()})
                score = s_overlap * 10 + overlap
                if best is None or score > best[0]:
                    best = (score, h.n, quote)
        if best is None or best[0] < 10:
            return f'NOT_FOUND("No candidate states this.", searched:[{searched}])'
        _, n, quote = best
        fn = {"money": "MONEY", "date": "DATE", "pct": "PCT_OF", "num": "NUM"}[want]
        qq = json.dumps(quote, ensure_ascii=False)
        return f"ANSWER({fn}(${n}.{qq}))"

    @staticmethod
    def _rare_term(words: list[str], texts: list[str]) -> str | None:
        best = None
        for w in words:
            if len(w) < 4:
                continue
            hits = sum(1 for t in texts if w.lower() in t.lower())
            if hits:
                exact = next(
                    (m.group(0) for t in texts for m in [re.search(re.escape(w), t, re.I)] if m), w
                )
                if best is None or hits < best[0]:
                    best = (hits, exact)
        return best[1] if best else None

    @staticmethod
    def _value_quote(text: str, want: str) -> str | None:
        try:
            if want == "date":
                r = ex.parse_date(text)
            elif want == "money":
                r = ex.parse_quantity(text, require="money")
            elif want == "pct":
                r = ex.parse_quantity(text, require="%")
            else:
                r = ex.parse_quantity(text)
        except Exception:
            return None
        return text[r.start : r.end]


def make_backend(cfg: ModelConfig) -> Backend:
    b = cfg.backend.lower()
    if b in ("llama.cpp", "llamacpp", "llama-cpp", "llama-server"):
        return LlamaCppBackend(cfg)
    if b == "ollama":
        return OllamaBackend(cfg)
    if b in ("openai-compatible", "openai", "vllm", "lmstudio", "mlx"):
        return OpenAICompatBackend(cfg)
    if b == "fake":
        return FakeBackend()
    raise ValueError(f"unknown model backend {cfg.backend!r}")
