"""Per-request GBNF grammar for llama.cpp constrained decoding.

Only the handles actually retrieved ($1..$k) are admissible, so the model cannot cite a span
that wasn't retrieved. llama.cpp documents that a grammar constrains output but is not shown
to the model; the prompt still describes the language.
"""

from __future__ import annotations

from spanql.formula.spec import ALL_KWARGS, CMP_OPS, FUNCTIONS


def _alts(items: list[str] | tuple[str, ...]) -> str:
    # longest first keeps the grammar readable; GBNF alternation is not ordered anyway
    return " | ".join(f'"{x}"' for x in sorted(items, key=lambda s: (-len(s), s)))


def build_gbnf(handles: list[int] | range, *, allow_cellrefs: bool = True) -> str:
    hs = sorted(set(handles))
    lines = [
        "root ::= answer | notfound | ambiguous",
        'answer ::= "ANSWER(" ws cmp ( ws "," ws cmp )* ( ws "," ws "text:" ws string )? ws ")"',
        'notfound ::= "NOT_FOUND(" ws string ( ws "," ws "searched:" ws hlist )? ws ")"',
        'ambiguous ::= "AMBIGUOUS(" ws string ws "," ws hlist ws ")"',
        "cmp ::= expr ( ws cmpop ws expr )?",
        f"cmpop ::= {_alts(CMP_OPS)}",
        'expr ::= term ( ws ( "+" | "-" ) ws term )*',
        'term ::= factor ( ws ( "*" | "/" ) ws factor )*',
        'factor ::= "-"? primary',
    ]
    prim = ["call", "number", "string", "bool", '"(" ws cmp ws ")"']
    if hs:
        prim.insert(1, "ref")
    if allow_cellrefs:
        prim.append("cellref")
    lines.append("primary ::= " + " | ".join(prim))
    if hs:
        lines.append('ref ::= handle ( "." string ( "#" [1-9] [0-9]? )? )?')
        lines.append(f'handle ::= "$" ( {_alts([str(h) for h in hs])} )')
        lines.append('hlist ::= "[" ws handle ( ws "," ws handle )* ws "]"')
    else:
        # no candidates: the only admissible answers are refusals without handle lists
        lines[0] = "root ::= notfound | answer"
        lines[2] = 'notfound ::= "NOT_FOUND(" ws string ws ")"'
        lines[3] = 'ambiguous ::= "AMBIGUOUS(" ws string ws ")"'
    lines += [
        'call ::= fname "(" ws ( arg ( ws "," ws arg )* )? ws ")"',
        "arg ::= kwarg | cmp",
        'kwarg ::= kwname ":" ws cmp',
        f"kwname ::= {_alts(ALL_KWARGS)}",
        f"fname ::= {_alts(FUNCTIONS)}",
        'number ::= [0-9]+ ( "." [0-9]+ )?',
        r'string ::= "\"" ( [^"\\\x00-\x1F] | "\\" ["\\/bfnrtdswDSW.()\[\]{}*+?|^$-] )* "\""',
        'bool ::= "TRUE" | "FALSE"',
    ]
    if allow_cellrefs:
        lines.append('cellref ::= "[" [a-zA-Z_] [a-zA-Z0-9_]* "]"')
    lines.append(r"ws ::= ( [ \t\n] [ \t]* )?")
    return "\n".join(lines) + "\n"
