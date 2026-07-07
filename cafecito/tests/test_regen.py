import textwrap

import pytest

from cafecito import regen
from cafecito.regen import Region, _test_defs, diff3_segments


# ---------------------------------------------------------------------------
# diff3_segments
# ---------------------------------------------------------------------------

def test_identical_inputs_returns_none():
    content = "line1\nline2\nline3\n"
    assert diff3_segments(content, content, content) is None


def test_clean_merge_returns_none():
    # ours adds a line at top, theirs adds a line at bottom — no overlap
    base = "middle\n"
    ours = "top\nmiddle\n"
    theirs = "middle\nbottom\n"
    assert diff3_segments(base, ours, theirs) is None


def test_collision_yields_three_segments():
    base = "line1\nCOMMON\nline3\n"
    ours = "line1\nOURS\nline3\n"
    theirs = "line1\nTHEIRS\nline3\n"
    segs = diff3_segments(base, ours, theirs)
    assert segs is not None
    # text, Region, text
    assert len(segs) == 3
    assert isinstance(segs[0], str)
    assert isinstance(segs[1], Region)
    assert isinstance(segs[2], str)


def test_collision_region_sides_match_inputs():
    base = "line1\nCOMMON\nline3\n"
    ours = "line1\nOURS\nline3\n"
    theirs = "line1\nTHEIRS\nline3\n"
    segs = diff3_segments(base, ours, theirs)
    region = segs[1]
    assert "OURS" in region.ours
    assert "COMMON" in region.base
    assert "THEIRS" in region.theirs


def test_collision_surrounding_text_preserved():
    base = "before\nCOMMON\nafter\n"
    ours = "before\nOURS\nafter\n"
    theirs = "before\nTHEIRS\nafter\n"
    segs = diff3_segments(base, ours, theirs)
    assert "before" in segs[0]
    assert "after" in segs[2]


def test_multiple_conflicts_yield_multiple_regions():
    base = "A\nCOMMON1\nB\nCOMMON2\nC\n"
    ours = "A\nOURS1\nB\nOURS2\nC\n"
    theirs = "A\nTHEIRS1\nB\nTHEIRS2\nC\n"
    segs = diff3_segments(base, ours, theirs)
    assert segs is not None
    regions = [s for s in segs if isinstance(s, Region)]
    assert len(regions) == 2


def test_whole_file_conflict_empty_surrounding_text():
    base = "COMMON\n"
    ours = "OURS\n"
    theirs = "THEIRS\n"
    segs = diff3_segments(base, ours, theirs)
    assert segs is not None
    assert isinstance(segs[0], str)
    assert isinstance(segs[1], Region)
    assert isinstance(segs[2], str)
    assert segs[0] == ""
    assert segs[2] == ""


# ---------------------------------------------------------------------------
# _test_defs
# ---------------------------------------------------------------------------

def test_test_defs_extracts_names():
    src = textwrap.dedent("""\
        def test_foo():
            pass

        def test_bar():
            pass
    """)
    assert _test_defs(src) == {"test_foo", "test_bar"}


def test_test_defs_ignores_non_test_functions():
    src = textwrap.dedent("""\
        def helper():
            pass

        def test_something():
            pass
    """)
    assert _test_defs(src) == {"test_something"}


def test_test_defs_empty_source():
    assert _test_defs("") == set()


def test_test_defs_no_test_functions():
    src = "def helper(): pass\nclass Foo: pass\n"
    assert _test_defs(src) == set()


def test_test_defs_indented_def_matches():
    # strip() removes indentation before the startswith check
    src = textwrap.dedent("""\
        class TestSuite:
            def test_member(self):
                pass
    """)
    assert "test_member" in _test_defs(src)
