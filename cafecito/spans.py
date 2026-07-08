"""Symbol-span extraction for non-Python languages — stdlib only, by design.

The oracle needs one thing per file: which named top-level symbol (function,
class, method, type) encloses each changed line. That is a much easier problem
than parsing, so these are conservative line scanners, not parsers:

- comments and string literals are stripped by a small state machine first
  (including JS template literals and Go raw strings, which span lines);
- declarations are recognized at module level (and one level deep for class
  bodies) by regex; spans close when brace depth returns to the opening level;
- ANY confusion — unbalanced braces at EOF, a span that never closes —
  returns None and the caller degrades to whole-file granularity.

Imprecision is safe by construction: the oracle only chooses parallelism, and
uncertainty always widens the write set; the landing gate remains the safety
mechanism (see PLAN "Safety model"). tree-sitter precision can replace this
later without changing any caller.
"""

from __future__ import annotations

import re

Span = tuple[str, int, int]  # (qualified name, start line, end line) — 1-indexed

PREFIX_BY_EXT = {
    ".py": "py",
    ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js",
    ".ts": "ts", ".tsx": "ts", ".mts": "ts", ".cts": "ts",
    ".go": "go",
}

LANG_BY_EXT = {
    ".py": "python",
    ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js",
    ".ts": "js", ".tsx": "js", ".mts": "js", ".cts": "js",
    ".go": "go",
}

_JS_DECL = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?"
    r"(?:(?P<kind>class|interface|enum|namespace)\s+(?P<cname>[A-Za-z_$][\w$]*)"
    r"|(?:async\s+)?function\s*\*?\s*(?P<fname>[A-Za-z_$][\w$]*)"
    r"|(?:const|let|var)\s+(?P<vname>[A-Za-z_$][\w$]*)\s*(?::[^=]{0,120})?=\s*"
    r"(?:async\b|\(|function\b|[A-Za-z_$][\w$]*\s*=>|<))"
)
_JS_METHOD = re.compile(
    r"^\s*(?:public\s+|private\s+|protected\s+|readonly\s+)*(?:static\s+)?"
    r"(?:async\s+)?(?:get\s+|set\s+)?\*?\s*"
    r"(?P<mname>[A-Za-z_$][\w$]*)\s*(?:<[^>]{0,80}>)?\s*(?:\(|=\s*(?:async\b|\(|function\b))"
)
_JS_METHOD_SKIP = {"if", "for", "while", "switch", "catch", "return", "new",
                   "typeof", "await", "yield", "else", "do", "case", "constructor"}

_GO_DECL = re.compile(
    r"^func\s+(?:\((?P<recv>[^)]*)\)\s+)?(?P<fname>[A-Za-z_]\w*)"
    r"|^type\s+(?P<tname>[A-Za-z_]\w*)\s+(?:struct|interface)\b"
)
_GO_RECV_TYPE = re.compile(r"\*?\s*([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*$")


def _strip_code(source: str, lang: str) -> list[str] | None:
    """Blank out comments and string contents, preserving line structure and
    braces. Returns lines, or None if the file ends inside a construct."""
    out: list[str] = []
    line: list[str] = []
    state = "code"          # code | line_comment | block_comment | str
    quote = ""              # active string delimiter: ' " ` (js) or ' " ` (go raw)
    i, n = 0, len(source)
    while i < n:
        c = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if c == "\n":
            out.append("".join(line))
            line = []
            if state == "line_comment":
                state = "code"
            # ordinary strings don't span lines; template/raw literals do
            if state == "str" and quote in ("'", '"'):
                state = "code"
            i += 1
            continue
        if state == "code":
            if c == "/" and nxt == "/":
                state = "line_comment"
                i += 2
                continue
            if c == "/" and nxt == "*":
                state = "block_comment"
                i += 2
                continue
            if c in ("'", '"', "`"):
                state, quote = "str", c
                line.append(" ")
                i += 1
                continue
            line.append(c)
        elif state == "block_comment":
            if c == "*" and nxt == "/":
                state = "code"
                i += 1
        elif state == "str":
            if c == "\\" and quote != "`" and lang == "js":
                i += 2
                continue
            if c == "\\" and lang == "go" and quote != "`":
                i += 2
                continue
            if c == quote:
                state = "code"
            else:
                line.append(" ")
                i += 1
                continue
        i += 1
    out.append("".join(line))
    if state in ("block_comment", "str") and quote not in ("`",):
        return None if state == "block_comment" else out
    return out


def _close_spans(open_decls: list, depth: int, lineno: int, spans: list) -> None:
    while open_decls and depth <= open_decls[-1][2]:
        name, start, _d, _entered = open_decls.pop()
        spans.append((name, start, lineno))


def js_spans(source: str) -> list[Span] | None:
    lines = _strip_code(source, "js")
    if lines is None:
        return None
    spans: list[Span] = []
    open_decls: list = []   # [name, start_line, start_depth, entered_block]
    class_ctx: list = []    # (class_name, class_depth)
    depth = 0
    for lineno, raw in enumerate(lines, start=1):
        at_module = depth == 0
        in_class = bool(class_ctx) and depth == class_ctx[-1][1] + 1
        decl_name = None
        if at_module:
            m = _JS_DECL.match(raw)
            if m:
                decl_name = m.group("cname") or m.group("fname") or m.group("vname")
                if m.group("kind") in ("class", "interface", "enum", "namespace") \
                        and m.group("cname"):
                    class_ctx.append((m.group("cname"), depth))
        elif in_class:
            m = _JS_METHOD.match(raw)
            if m and m.group("mname") not in _JS_METHOD_SKIP:
                decl_name = f"{class_ctx[-1][0]}.{m.group('mname')}"
        if decl_name:
            open_decls.append([decl_name, lineno, depth, False])
        opens, closes = raw.count("{"), raw.count("}")
        if opens and open_decls and not open_decls[-1][3]:
            open_decls[-1][3] = True
        depth += opens - closes
        if depth < 0:
            return None
        # close finished declarations (only ones that actually entered a block)
        while open_decls and open_decls[-1][3] and depth <= open_decls[-1][2]:
            name, start, _d, _e = open_decls.pop()
            spans.append((name, start, lineno))
        while class_ctx and depth <= class_ctx[-1][1]:
            class_ctx.pop()
        # single-expression decls (no block): close at end of the decl line
        if open_decls and not open_decls[-1][3] and open_decls[-1][1] == lineno \
                and raw.rstrip().endswith((";", ")")):
            name, start, _d, _e = open_decls.pop()
            spans.append((name, start, lineno))
    if depth != 0:
        return None
    for name, start, _d, _e in open_decls:  # unterminated no-block decls
        spans.append((name, start, start))
    return spans


def go_spans(source: str) -> list[Span] | None:
    lines = _strip_code(source, "go")
    if lines is None:
        return None
    spans: list[Span] = []
    open_decl = None        # [name, start_line, start_depth]
    depth = 0
    for lineno, raw in enumerate(lines, start=1):
        if depth == 0 and open_decl is None:
            m = _GO_DECL.match(raw)
            if m:
                if m.group("fname"):
                    name = m.group("fname")
                    recv = m.group("recv")
                    if recv:
                        rm = _GO_RECV_TYPE.search(recv)
                        if rm:
                            name = f"{rm.group(1)}.{name}"
                    open_decl = [name, lineno, depth]
                elif m.group("tname"):
                    open_decl = [m.group("tname"), lineno, depth]
        depth += raw.count("{") - raw.count("}")
        if depth < 0:
            return None
        if open_decl is not None and depth <= open_decl[2]:
            name, start, _d = open_decl
            spans.append((name, start, lineno))
            open_decl = None
    if depth != 0:
        return None
    return spans


def symbol_spans(source: str, lang: str) -> list[Span] | None:
    """Spans for `lang` ('js' | 'go'). None = could not analyze (degrade to
    file granularity). Python uses writeset.python_symbols (ast) instead."""
    if lang == "js":
        return js_spans(source)
    if lang == "go":
        return go_spans(source)
    return None
