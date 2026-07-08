import textwrap

from cafecito.spans import json_spans, symbol_spans
from cafecito.writeset import _attribute_lang

PKG = textwrap.dedent("""\
    {
      "name": "demo",
      "version": "1.2.3",
      "scripts": {
        "build": "tsc -p .",
        "test": "vitest run"
      },
      "dependencies": {
        "react": "^18.2.0",
        "left-pad": "^1.3.0",
        "zod": "^3.22.0"
      },
      "devDependencies": {
        "vitest": "^1.0.0"
      }
    }
    """)


def _by_name(spans):
    return {name: (start, end) for name, start, end in spans}


def _line_of(fragment):
    return next(i for i, l in enumerate(PKG.splitlines(), 1) if fragment in l)


def test_dep_keys_are_dotted_paths():
    spans = _by_name(json_spans(PKG))
    assert "dependencies.react" in spans
    assert "dependencies.left-pad" in spans
    assert "scripts.build" in spans
    assert "version" in spans


def test_different_dep_bumps_commute():
    spans = json_spans(PKG)
    react = _attribute_lang("package.json", [(_line_of('"react"'),) * 2], spans, "json")
    zod = _attribute_lang("package.json", [(_line_of('"zod"'),) * 2], spans, "json")
    assert react == {"json:package.json::dependencies.react"}
    assert zod == {"json:package.json::dependencies.zod"}
    assert not (react & zod)  # the whole point


def test_same_dep_collides():
    spans = json_spans(PKG)
    a = _attribute_lang("package.json", [(_line_of('"react"'),) * 2], spans, "json")
    b = _attribute_lang("package.json", [(_line_of('"react"'),) * 2], spans, "json")
    assert a & b


def test_parent_section_spans_cover_children():
    spans = _by_name(json_spans(PKG))
    ds, de = spans["dependencies"]
    rs, re_ = spans["dependencies.react"]
    assert ds <= rs and re_ <= de


def test_invalid_json_returns_none():
    assert json_spans("{ not json ]") is None


def test_minified_is_conservative():
    spans = json_spans('{"a": {"b": 1}, "c": 2}')
    # everything on one line → all spans collide there; safe, not wrong
    assert spans is not None
    got = _attribute_lang("m.json", [(1, 1)], spans, "json")
    assert got  # attributes to something rather than nothing


def test_dispatch():
    assert symbol_spans('{"a": 1}', "json") is not None
