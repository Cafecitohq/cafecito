"""The version is declared twice — pyproject.toml (packaging) and
cafecito/__init__.py (runtime, echoed by `cafecito version` and the MCP
serverInfo) — and pushing a v* tag publishes to PyPI via trusted publishing,
irrevocably. This pins the two declarations together so a release can never
ship with a split identity."""

import pathlib
import re

import cafecito


def test_pyproject_matches_dunder_version():
    root = pathlib.Path(cafecito.__file__).resolve().parents[1]
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        # Installed wheel, no source tree alongside: compare against metadata.
        import importlib.metadata
        assert importlib.metadata.version("cafecito") == cafecito.__version__
        return
    m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(), re.M)
    assert m, "pyproject.toml has no version line"
    assert m.group(1) == cafecito.__version__
