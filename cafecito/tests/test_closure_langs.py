import subprocess
import sys

import pytest

from cafecito.closure import go_closure, input_closure, js_closure
from cafecito.engine import Engine
from cafecito.facts import FactsStore
from cafecito.gate import blob_map, run_gate
from cafecito.gate import test_family as family_of


def make_repo(tmp_path, files):
    for path, content in files.items():
        p = tmp_path / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    def sh(*args):
        subprocess.run(args, cwd=tmp_path, check=True, capture_output=True)
    sh("git", "init", "-q", "-b", "main")
    sh("git", "add", "-A")
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "i")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                          capture_output=True, text=True).stdout.strip()
    return str(tmp_path), head, set(blob_map(str(tmp_path), head))


PKG = '{"name": "x", "private": true}\n'


# ------------------------------------------------------------------ JS / TS

def test_js_transitive_chain_and_configs(tmp_path):
    repo, head, listing = make_repo(tmp_path, {
        "package.json": PKG,
        "vitest.config.ts": "export default {};\n",
        "src/util.ts": "export const u = 1;\n",
        "src/mod.ts": "import { u } from './util';\nexport const m = u;\n",
        "src/mod.test.ts": "import { m } from './mod';\ntest('m', () => {});\n",
    })
    c = js_closure(repo, head, "src/mod.test.ts", listing)
    assert c == {"src/mod.test.ts", "src/mod.ts", "src/util.ts",
                 "package.json", "vitest.config.ts"}


def test_js_ext_swap_index_and_asset_leaves(tmp_path):
    repo, head, listing = make_repo(tmp_path, {
        "package.json": PKG,
        "a.ts": "export {};\n",
        "lib/index.ts": "import d from './../data.json';\nimport './../s.css';\n"
                        "export {};\n",
        "data.json": "{}\n",
        "s.css": "body {}\n",
        "t.test.ts": "import {} from './a.js';\nimport {} from './lib';\n",
    })
    c = js_closure(repo, head, "t.test.ts", listing)
    assert c == {"t.test.ts", "a.ts", "lib/index.ts", "data.json", "s.css",
                 "package.json"}


def test_js_bare_specifiers_are_external(tmp_path):
    repo, head, listing = make_repo(tmp_path, {
        "package.json": PKG,
        "t.test.ts": "import { test } from 'vitest';\ntest('x', () => {});\n",
    })
    assert js_closure(repo, head, "t.test.ts", listing) == \
        {"t.test.ts", "package.json"}


def test_js_lockfile_and_nested_configs_ride_along(tmp_path):
    repo, head, listing = make_repo(tmp_path, {
        "package.json": PKG,
        "package-lock.json": "{}\n",
        "pkg/a/package.json": PKG,
        "pkg/a/x.test.ts": "test('x', () => {});\n",
    })
    c = js_closure(repo, head, "pkg/a/x.test.ts", listing)
    assert c == {"pkg/a/x.test.ts", "pkg/a/package.json", "package.json",
                 "package-lock.json"}


@pytest.mark.parametrize("files", [
    {"t.test.ts": "import {} from './missing';\n"},
    {"t.test.ts": "import {} from '#internal/x';\n"},
    {"t.test.ts": "test('x');\n", "package.json":
        '{"workspaces": ["pkg/*"]}\n'},
    {"t.test.ts": "test('x');\n", "package.json": PKG, "vitest.config.ts":
        "export default { resolve: { alias: { a: './src' } } };\n"},
    {"t.test.ts": "test('x');\n", "package.json": PKG, "jest.config.js":
        "module.exports = { moduleNameMapper: {} };\n"},
    {"t.test.ts": "test('x');\n", "tsconfig.json":
        '{"compilerOptions": {"paths": {"@/*": ["src/*"]}}}\n'},
    {"t.test.ts": "test('x');\n", "tsconfig.json":
        '{"compilerOptions": {"baseUrl": "./src"}}\n'},
])
def test_js_confusion_returns_none(tmp_path, files):
    repo, head, listing = make_repo(tmp_path, files)
    assert js_closure(repo, head, "t.test.ts", listing) is None


def test_js_tsconfig_extends_relative(tmp_path):
    repo, head, listing = make_repo(tmp_path, {
        "tsconfig.base.json": '{"compilerOptions": {"strict": true}}\n',
        "app/tsconfig.json": '{"extends": "../tsconfig.base"}\n',
        "app/t.test.ts": "test('x');\n",
    })
    c = js_closure(repo, head, "app/t.test.ts", listing)
    assert c == {"app/t.test.ts", "app/tsconfig.json", "tsconfig.base.json"}

    repo2, head2, listing2 = make_repo(tmp_path / "two", {
        "tsconfig.base.json": '{"compilerOptions": {"paths": {}}}\n',
        "app/tsconfig.json": '{"extends": "../tsconfig.base"}\n',
        "app/t.test.ts": "test('x');\n",
    })
    assert js_closure(repo2, head2, "app/t.test.ts", listing2) is None


# ---------------------------------------------------------------------- Go

GO_MOD = "module example.com/m\n\ngo 1.22\n"


def test_go_package_membership_and_cross_package(tmp_path):
    repo, head, listing = make_repo(tmp_path, {
        "go.mod": GO_MOD,
        "go.sum": "\n",
        "pkg/a.go": 'package pkg\n\nimport (\n\t"fmt"\n\t'
                    'u "example.com/m/util" // aliased\n)\n\n'
                    'var _ = fmt.Sprint(u.X)\n',
        "pkg/b.go": "package pkg\n",
        "pkg/a_test.go": 'package pkg\n\nimport "testing"\n',
        "util/u.go": 'package util\n\nvar X = 1\n',
        "unrelated/z.go": "package unrelated\n",
    })
    c = go_closure(repo, head, "pkg/a_test.go", listing)
    assert c == {"pkg/a.go", "pkg/b.go", "pkg/a_test.go", "util/u.go",
                 "go.mod", "go.sum"}


def test_go_external_imports_ignored(tmp_path):
    repo, head, listing = make_repo(tmp_path, {
        "go.mod": GO_MOD,
        "pkg/a_test.go": 'package pkg\n\nimport (\n\t"testing"\n\t'
                         '"github.com/stretchr/testify/assert"\n)\n',
    })
    assert go_closure(repo, head, "pkg/a_test.go", listing) == \
        {"pkg/a_test.go", "go.mod"}


@pytest.mark.parametrize("files", [
    {"go.mod": GO_MOD, "go.work": "go 1.22\n",
     "pkg/a_test.go": "package pkg\n"},
    {"go.mod": GO_MOD, "sub/go.mod": "module example.com/m/sub\n",
     "pkg/a_test.go": "package pkg\n"},
    {"go.mod": GO_MOD, "vendor/x/x.go": "package x\n",
     "pkg/a_test.go": "package pkg\n"},
    {"go.mod": "go 1.22\n", "pkg/a_test.go": "package pkg\n"},
    {"go.mod": GO_MOD,
     "pkg/a_test.go": 'package pkg\n\nimport _ "embed"\n\n'
                      '//go:embed data.txt\nvar d string\n'},
    {"go.mod": GO_MOD, "pkg/a_test.go": 'package pkg\n\nimport "C"\n'},
    {"go.mod": GO_MOD,
     "pkg/a_test.go": 'package pkg\n\nimport "example.com/m/ghost"\n'},
])
def test_go_confusion_returns_none(tmp_path, files):
    repo, head, listing = make_repo(tmp_path, files)
    assert go_closure(repo, head, "pkg/a_test.go", listing) is None


# ---------------------------------------------------------------- dispatch

def test_dispatcher_routes_by_extension(tmp_path):
    repo, head, listing = make_repo(tmp_path, {
        "package.json": PKG,
        "go.mod": GO_MOD,
        "mod.py": "x = 1\n",
        "tests/test_mod.py": "from mod import x\n",
        "a.test.ts": "test('x');\n",
        "pkg/a_test.go": "package pkg\n",
        "spec_test.rb": "puts 1\n",
    })
    py = input_closure(repo, head, "tests/test_mod.py", listing)
    assert py and "mod.py" in py and "package.json" not in py
    js = input_closure(repo, head, "a.test.ts", listing)
    assert js and "package.json" in js and "go.mod" not in js
    go = input_closure(repo, head, "pkg/a_test.go", listing)
    assert go and "go.mod" in go
    assert input_closure(repo, head, "spec_test.rb", listing) is None


# ------------------------------------------------------- full-mode families

def test_gate_families_derived_from_test_cmd(tmp_path):
    _, _, _ = make_repo(tmp_path, {"a.py": "x = 1\n"})
    eng = Engine(str(tmp_path))
    cases = [
        (["/venv/bin/python", "-m", "pytest", "-q"], {"py"}),
        (["python3.12", "-m", "pytest"], {"py"}),
        (["npx", "vitest", "run"], {"js"}),
        (["yarn", "jest"], {"js"}),
        (["go", "test"], {"go"}),
        (["./scripts/run-checks.sh"], {"py"}),      # unknown → old behavior
    ]
    for cmd, want in cases:
        eng.config["test_cmd"] = cmd
        eng.config["gate_families"] = []
        assert eng._gate_families() == want, cmd
    eng.config["gate_families"] = ["js", "go"]
    assert eng._gate_families() == {"js", "go"}     # explicit config wins


def test_full_mode_collects_js_tests(tmp_path):
    repo, _, _ = make_repo(tmp_path, {
        "package.json": PKG,
        "sum.ts": "export const s = 1;\n",
        "sum.test.ts": "test('s', () => {});\n",
        "tests/test_stray.py": "def test_x():\n    assert True\n",
    })
    eng = Engine(repo)
    eng.config.update({"gate_mode": "full", "gate_families": ["js"],
                       "test_cmd": [sys.executable, "-c", "pass"]})
    def sh(*args):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    sh("git", "checkout", "-q", "-b", "w", "main")
    (tmp_path / "sum.ts").write_text("export const s = 2;\n")
    sh("git", "add", "-A")
    sh("git", "-c", "user.name=a", "-c", "user.email=a@a", "commit", "-q", "-m", "e")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    sh("git", "checkout", "-q", "main")
    res = eng.submit(head, agent="a", title="edit")
    assert res["verdict"] == "landed"
    assert res["gate"]["tests"] == ["sum.test.ts"]  # the stray .py excluded


def test_family_by_extension():
    assert family_of("tests/test_x.py") == "py"
    assert family_of("pkg/a_test.go") == "go"
    assert family_of("web/a.test.tsx") == "js"
    assert family_of("web/a.spec.mjs") == "js"
    assert family_of("spec_test.rb") is None


# --------------------------------------------------- gate-level memoization

def test_ts_facts_inherit_and_invalidate(tmp_path):
    repo, head, listing = make_repo(tmp_path, {
        "package.json": PKG,
        "web/sum.ts": "export const sum = (a, b) => a + b;\n",
        "web/sum.test.ts": "import { sum } from './sum';\ntest('s', () => {});\n",
    })
    facts = FactsStore(tmp_path / ".facts")
    (tmp_path / ".facts").mkdir()
    cmd = [sys.executable, "-c", "pass"]     # stand-in runner: always green
    first = run_gate(repo, head, ["web/sum.test.ts"], cmd, facts=facts)
    assert first["green"] and first["memo"] == {"hits": 0, "runs": 1}
    replay = run_gate(repo, head, ["web/sum.test.ts"], cmd, facts=facts)
    assert replay["memo"] == {"hits": 1, "runs": 0}

    (tmp_path / "web" / "sum.ts").write_text(
        "export const sum = (a, b) => b + a;\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True,
                   capture_output=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "-m", "change dep"], cwd=tmp_path,
                   check=True, capture_output=True)
    changed = run_gate(repo, "HEAD", ["web/sum.test.ts"], cmd, facts=facts)
    assert changed["memo"] == {"hits": 0, "runs": 1}
