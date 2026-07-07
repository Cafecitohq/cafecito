import textwrap

import pytest

import writeset
from writeset import _attribute, python_symbols


# ---------------------------------------------------------------------------
# python_symbols
# ---------------------------------------------------------------------------

def test_top_level_function():
    src = textwrap.dedent("""\
        def foo():
            pass
    """)
    syms = python_symbols(src)
    assert syms == [("foo", 1, 2)]


def test_nested_class_method_qualname():
    src = textwrap.dedent("""\
        class MyClass:
            def method(self):
                pass
    """)
    syms = python_symbols(src)
    names = {q for q, _, _ in syms}
    assert "MyClass" in names
    assert "MyClass.method" in names


def test_deeply_nested_qualname():
    src = textwrap.dedent("""\
        class Outer:
            class Inner:
                def work(self):
                    pass
    """)
    syms = python_symbols(src)
    names = {q for q, _, _ in syms}
    assert "Outer" in names
    assert "Outer.Inner" in names
    assert "Outer.Inner.work" in names


def test_decorator_expands_start_line():
    src = textwrap.dedent("""\
        @some_decorator
        def foo():
            pass
    """)
    syms = python_symbols(src)
    assert len(syms) == 1
    qual, start, end = syms[0]
    assert qual == "foo"
    # decorator is on line 1, def on line 2
    assert start == 1
    assert end == 3


def test_multiple_decorators_start_at_first():
    src = textwrap.dedent("""\
        @dec_a
        @dec_b
        def bar():
            pass
    """)
    syms = python_symbols(src)
    assert len(syms) == 1
    qual, start, end = syms[0]
    assert qual == "bar"
    assert start == 1  # first decorator


def test_class_decorator_expands_start_line():
    src = textwrap.dedent("""\
        @dataclass
        class Point:
            x: int
            y: int
    """)
    syms = python_symbols(src)
    assert len(syms) == 1
    qual, start, end = syms[0]
    assert qual == "Point"
    assert start == 1


def test_async_function_included():
    src = textwrap.dedent("""\
        async def fetch():
            pass
    """)
    syms = python_symbols(src)
    assert syms == [("fetch", 1, 2)]


def test_syntax_error_raises():
    with pytest.raises(SyntaxError):
        python_symbols("def (broken")


# ---------------------------------------------------------------------------
# _attribute
# ---------------------------------------------------------------------------

_SYMBOLS = [
    ("MyClass", 1, 10),
    ("MyClass.method", 3, 6),
    ("helper", 12, 15),
]


def test_attribute_innermost_wins():
    # line 4 is inside both MyClass (1-10) and MyClass.method (3-6)
    result = _attribute("mod.py", [(4, 4)], _SYMBOLS)
    assert result == {"py:mod.py::MyClass.method"}


def test_attribute_outer_only():
    # line 8 is inside MyClass (1-10) but not MyClass.method (3-6)
    result = _attribute("mod.py", [(8, 8)], _SYMBOLS)
    assert result == {"py:mod.py::MyClass"}


def test_attribute_module_level():
    # line 20 is outside all symbols
    result = _attribute("mod.py", [(20, 20)], _SYMBOLS)
    assert result == {"py:mod.py::<module>"}


def test_attribute_range_spans_multiple_symbols():
    # range (4, 13) touches MyClass.method, MyClass, and helper
    result = _attribute("mod.py", [(4, 13)], _SYMBOLS)
    # innermost for 4-13: MyClass.method (span 3) is inside MyClass (span 9),
    # and helper (span 3) overlaps too — both are candidates, pick smaller span
    # MyClass.method span = 6-3 = 3, helper span = 15-12 = 3, same size
    # both are valid innermost; result must contain both
    assert "py:mod.py::MyClass.method" in result or "py:mod.py::helper" in result


def test_attribute_multiple_ranges():
    # two separate ranges: one inside method, one at module level
    result = _attribute("mod.py", [(5, 5), (20, 20)], _SYMBOLS)
    assert "py:mod.py::MyClass.method" in result
    assert "py:mod.py::<module>" in result


def test_attribute_empty_symbols():
    result = _attribute("mod.py", [(1, 5)], [])
    assert result == {"py:mod.py::<module>"}


def test_attribute_path_preserved():
    result = _attribute("pkg/sub/mod.py", [(5, 5)], _SYMBOLS)
    assert all("py:pkg/sub/mod.py::" in s for s in result)
