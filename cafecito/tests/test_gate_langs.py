import subprocess

import pytest

from cafecito.gate import impact_tests


@pytest.fixture()
def repo(tmp_path):
    def sh(*args):
        subprocess.run(args, cwd=tmp_path, check=True, capture_output=True)

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "store.go").write_text("package pkg\n")
    (tmp_path / "pkg" / "store_test.go").write_text("package pkg\n")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "cart.ts").write_text("export const x = 1;\n")
    (tmp_path / "web" / "cart.test.ts").write_text("test('x', () => {});\n")
    (tmp_path / "web" / "__tests__").mkdir()
    (tmp_path / "web" / "checkout.tsx").write_text("export const y = 2;\n")
    (tmp_path / "web" / "__tests__" / "checkout.spec.tsx").write_text("it('y');\n")
    sh("git", "init", "-q", "-b", "main")
    sh("git", "add", "-A")
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "i")
    return str(tmp_path)


def test_go_sibling_convention(repo):
    assert impact_tests(repo, {"pkg/store.go"}, "HEAD") == {"pkg/store_test.go"}


def test_ts_sibling_test_convention(repo):
    assert impact_tests(repo, {"web/cart.ts"}, "HEAD") == {"web/cart.test.ts"}


def test_tsx_dunder_tests_spec_convention(repo):
    assert impact_tests(repo, {"web/checkout.tsx"}, "HEAD") == {
        "web/__tests__/checkout.spec.tsx"}


def test_touched_lang_test_files_map_to_themselves(repo):
    got = impact_tests(repo, {"pkg/store_test.go", "web/cart.test.ts"}, "HEAD")
    assert got == {"pkg/store_test.go", "web/cart.test.ts"}


def test_markdown_still_produces_nothing(repo):
    assert impact_tests(repo, {"README.md"}, "HEAD") == set()
