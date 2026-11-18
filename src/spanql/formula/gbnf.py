"""Per-request GBNF grammar for llama.cpp (ARCHITECTURE.md 2.3).

The grammar admits only the handles ``$1..$k`` retrieved for this question, so
a decoder cannot name a span that was not offered. Types are *not* enforced here
(that would blow up the grammar); the type checker and verifier do that and feed
errors back for a retry.

Known limit: a context-free grammar cannot forbid repeating a keyword inside one
call (``places:1, places:2``). The parser rejects that and the harness retries.
"""

from __future__ import annotations

from .functions import ALL_KWARGS, FUNCTIONS

MAX_HANDLES = 200
_NON_QUERY_KW = {"text", "searched"}


def _alts(words: list[str]) -> str:
    return " | ".join('"' + w + '"' for w in words)


def build_gbnf(n_handles: int) -> str:
    """Return a GBNF grammar string. With ``n_handles == 0`` only NOT_FOUND is derivable."""
    if not 0 <= n_handles <= MAX_HANDLES:
        raise ValueError(f"n_handles must be between 0 and {MAX_HANDLES}")
    fnames = sorted((n for n in FUNCTIONS), key=lambda s: (-len(s), s))
    kwnames = sorted(k for k in ALL_KWARGS if k not in _NON_QUERY_KW)
    lines: list[str] = []
    add = lines.append
    add('root ::= ws form ws')
    if n_handles == 0:
        add('form ::= notfound')
        add('notfound ::= "NOT_FOUND" ws "(" ws string ws ")"')
    else:
        add('form ::= answer | notfound | ambiguous')
        add('notfound ::= "NOT_FOUND" ws "(" ws string (ws "," ws "searched" ws ":" ws handle-list)? ws ")"')
        add('ambiguous ::= "AMBIGUOUS" ws "(" ws string ws "," ws handle-list ws ")"')
        add('answer ::= "ANSWER" ws "(" ws expr (ws "," ws expr)* (ws "," ws "text" ws ":" ws string)? ws ")"')
        add('handle-list ::= "[" ws handle (ws "," ws handle)* ws "]"')
        add('handle ::= "$" (' + _alts([str(i) for i in range(1, n_handles + 1)]) + ")")
        add('expr ::= add (ws cmp-op ws add)?')
        add('cmp-op ::= "==" | "!=" | ">=" | "<=" | ">" | "<"')
        add('add ::= mul (ws add-op ws mul)*')
        add('add-op ::= "+" | "-"')
        add('mul ::= unary (ws mul-op ws unary)*')
        add('mul-op ::= "*" | "/"')
        add('unary ::= ("-" ws)? primary')
        add('primary ::= call | ref | number | string | boolean | "(" ws expr ws ")" | cell-ref')
        add('call ::= fname ws "(" ws (arg (ws "," ws arg)*)? ws ")"')
        add('arg ::= kwarg | expr')
        add('kwarg ::= kwname ws ":" ws expr')
        add('ref ::= handle ("." quote ("#" [1-9] [0-9]*)?)?')
        add('cell-ref ::= "[" ws ([0-9]+ | ident) ws "]"')
        add('ident ::= [a-zA-Z_] [a-zA-Z0-9_]*')
        add('number ::= [0-9]+ ("." [0-9]+)?')
        add('boolean ::= "true" | "false"')
        add('fname ::= ' + _alts(fnames))
        add('kwname ::= ' + _alts(kwnames))
    add(r'string ::= "\"" string-char* "\""')
    if n_handles:
        add(r'quote ::= "\"" string-char+ "\""')  # a quote must be non-empty
    add(r'string-char ::= [^"\\\x00-\x1f] | "\\" (["\\/nrt] | "u" hex hex hex hex)')
    add('hex ::= [0-9a-fA-F]')
    add('ws ::= [ \\t\\n]{0,3}')
    return "\n".join(lines) + "\n"
