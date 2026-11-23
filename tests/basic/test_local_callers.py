import pytest

from aider.local_callers import (
    DEFAULT_CALLER_HINT,
    CallerExcerpt,
    find_related_callers,
    format_caller_context,
    read_source,
    related_callers,
)


def test_scoped_import_and_budget(tmp_path):
    (tmp_path / "api.py").write_text("def parse(value): return value")
    (tmp_path / "consumer.py").write_text("from api import parse as p\nvalue = p(1) + 1")
    (tmp_path / "other.py").write_text("from legacy import parse\nparse(2)")
    (tmp_path / "broken.py").write_text("from api import parse\nparse(")
    (tmp_path / "linked.py").symlink_to(tmp_path / "consumer.py")
    candidates = ["other.py", "linked.py", "broken.py", "consumer.py"]
    assert related_callers(tmp_path, "fix parse", ["api.py"], candidates) == (
        "consumer.py:1\nfrom api import parse as p\nvalue = p(1) + 1"
    )
    assert not related_callers(tmp_path, "fix parser", ["api.py"], candidates)


def test_reference_size_and_file_count(tmp_path):
    (tmp_path / "api.py").write_text("async def decode(): pass")
    callers = [f"call{i}.py" for i in range(5)]
    for name in callers:
        (tmp_path / name).write_text("from api import decode\nvalue = decode()\n#" + "a" * 2048)
    excerpt = related_callers(tmp_path, "fix decode", ["api.py"], reversed(callers))
    assert [name in excerpt for name in callers] == [True, True, True, False, False]
    assert len(excerpt) < 2200


def test_python_relative_import_module_call_and_class_method(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "api.py").write_text(
        "def compute(x):\n"
        "    return x * 2\n\n"
        "class Formatter:\n"
        "    def render(self, item):\n"
        "        return str(item)\n"
    )
    (pkg / "rel_caller.py").write_text("from .api import compute\nout = compute(5)\n")
    (tmp_path / "mod_caller.py").write_text("import pkg.api as a\nval = a.compute(7)\n")
    (tmp_path / "direct_mod_caller.py").write_text("import pkg.api\nval = pkg.api.compute(9)\n")
    (tmp_path / "method_caller.py").write_text(
        "from pkg.api import Formatter\nfmt = Formatter()\nlabel = fmt.render(42)\n"
    )
    (tmp_path / "unrelated_method.py").write_text(
        "from other import Formatter\nfmt = Formatter()\nlabel = fmt.render(42)\n"
    )

    rel_excerpts = find_related_callers(
        tmp_path,
        "update compute",
        ["pkg/api.py"],
        ["pkg/rel_caller.py", "mod_caller.py", "direct_mod_caller.py"],
    )
    assert [e.path for e in rel_excerpts] == [
        "direct_mod_caller.py",
        "mod_caller.py",
        "pkg/rel_caller.py",
    ]
    assert all(e.symbol == "compute" for e in rel_excerpts)
    assert "out = compute(5)" in rel_excerpts[2].code
    assert "val = a.compute(7)" in rel_excerpts[1].code
    assert "val = pkg.api.compute(9)" in rel_excerpts[0].code

    method_excerpts = find_related_callers(
        tmp_path,
        "fix render formatting",
        ["pkg/api.py"],
        ["method_caller.py", "unrelated_method.py"],
    )
    assert len(method_excerpts) == 1
    assert method_excerpts[0].path == "method_caller.py"
    assert method_excerpts[0].symbol == "render"
    assert "label = fmt.render(42)" in method_excerpts[0].code


def test_js_esm_caller_detection_aliases_and_shadowing(tmp_path):
    (tmp_path / "api.mjs").write_text(
        'export function splitName(full) {\n  return full.split(" ");\n}\n'
    )
    (tmp_path / "consumer.mjs").write_text(
        "import { splitName } from './api.mjs';\n"
        "export function format(full) {\n"
        "  const [last, first] = splitName(full);\n"
        "  return `${last}, ${first}`;\n"
        "}\n"
    )
    (tmp_path / "aliased.mjs").write_text(
        "import { splitName as sn } from './api.mjs';\n"
        "const [last, first] = sn('Ada Lovelace');\n"
    )
    (tmp_path / "other_consumer.mjs").write_text(
        "import { splitName } from './other.mjs';\n"
        "const parts = splitName('Grace Hopper');\n"
    )
    (tmp_path / "broken.mjs").write_text("import { splitName } from './other.mjs';\n((((\n")

    excerpts = find_related_callers(
        tmp_path,
        "fix splitName return order",
        ["api.mjs"],
        ["other_consumer.mjs", "broken.mjs", "consumer.mjs", "aliased.mjs", "api.mjs"],
    )
    assert [e.path for e in excerpts] == ["aliased.mjs", "consumer.mjs"]
    assert all(e.symbol == "splitName" for e in excerpts)
    assert "sn('Ada Lovelace')" in excerpts[0].code
    assert "const [last, first] = splitName(full);" in excerpts[1].code


def test_typescript_and_go_tree_sitter_callers(tmp_path):
    (tmp_path / "parser.ts").write_text(
        "export const parsePrice = (raw: string): number => Number(raw);\n"
    )
    (tmp_path / "checkout.ts").write_text(
        "import { parsePrice } from './parser';\n"
        "export function total(input: string): number {\n"
        "  return parsePrice(input) * 100;\n"
        "}\n"
    )
    (tmp_path / "other.ts").write_text(
        "import { parsePrice } from './legacy';\nconst x = parsePrice('10');\n"
    )

    ts_excerpts = find_related_callers(
        tmp_path,
        "fix parsePrice cents conversion",
        ["parser.ts"],
        ["checkout.ts", "other.ts"],
    )
    assert len(ts_excerpts) == 1
    assert ts_excerpts[0].path == "checkout.ts"
    assert ts_excerpts[0].symbol == "parsePrice"
    assert "return parsePrice(input) * 100;" in ts_excerpts[0].code

    (tmp_path / "range.go").write_text(
        "package api\n\nfunc ParseRange(s string) (int, int) {\n\treturn 0, 1\n}\n"
    )
    (tmp_path / "main.go").write_text(
        "package main\n\n"
        "func run(s string) int {\n"
        "\tlo, hi := api.ParseRange(s)\n"
        "\treturn hi - lo\n"
        "}\n"
    )

    go_excerpts = find_related_callers(
        tmp_path,
        "fix ParseRange bounds",
        ["range.go"],
        ["main.go"],
    )
    assert len(go_excerpts) == 1
    assert go_excerpts[0].path == "main.go"
    assert go_excerpts[0].symbol == "ParseRange"
    assert "lo, hi := api.ParseRange(s)" in go_excerpts[0].code


def test_format_caller_context_modes_and_validation():
    excerpt = CallerExcerpt(
        path="consumer.mjs",
        start_line=1,
        symbol="splitName",
        code="const [last, first] = splitName(full);",
    )

    assert format_caller_context([], mode="passive") == ""
    assert format_caller_context([], mode="contract") == ""

    passive_default = format_caller_context([excerpt], mode="passive")
    assert passive_default.startswith(
        f"Read-only caller snapshot for this task. {DEFAULT_CALLER_HINT}\n"
    )
    assert "consumer.mjs:1\nconst [last, first] = splitName(full);" in passive_default

    passive_empty_hint = format_caller_context([excerpt], hint="", mode="passive")
    assert passive_empty_hint.startswith("Read-only caller snapshot for this task.\nconsumer.mjs:1")
    assert DEFAULT_CALLER_HINT not in passive_empty_hint

    passive_custom_hint = format_caller_context([excerpt], hint="Preserve order.", mode="passive")
    assert passive_custom_hint.startswith(
        "Read-only caller snapshot for this task. Preserve order.\n"
    )

    contract = format_caller_context([excerpt], mode="contract")
    assert "CALLER_CONTRACT:" in contract
    assert "TRACE:" in contract
    assert "consumer.mjs:1\nconst [last, first] = splitName(full);" in contract

    contract_hint = format_caller_context([excerpt], hint="Focus on null.", mode="contract")
    assert "Focus on null." in contract_hint
    assert "CALLER_CONTRACT:" in contract_hint
    assert "TRACE:" in contract_hint

    with pytest.raises(ValueError, match="Unknown caller context mode"):
        format_caller_context([excerpt], mode="unknown")

    with pytest.raises(ValueError, match="Unknown caller context mode"):
        format_caller_context([], mode="unknown")


def test_security_and_size_guards_for_non_python_files(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    outside = tmp_path / "outside.mjs"
    outside.write_text("import { splitName } from './api.mjs';\nsplitName('a b');\n")

    (project / "api.mjs").write_text("export function splitName(s) { return s.split(' '); }\n")
    (project / "valid.mjs").write_text(
        "import { splitName } from './api.mjs';\nconst out = splitName('a b');\n"
    )
    (project / "linked.mjs").symlink_to(project / "valid.mjs")
    (project / "oversized.mjs").write_text(
        "import { splitName } from './api.mjs';\nsplitName('a b');\n//" + "x" * 140000
    )
    (project / "binary.mjs").write_bytes(b"\xff\xfeimport { splitName } from './api.mjs';")

    assert read_source(project, "../outside.mjs") == ""
    assert read_source(project, "linked.mjs") == ""
    assert read_source(project, "oversized.mjs") == ""
    assert read_source(project, "binary.mjs") == ""

    excerpts = find_related_callers(
        project,
        "fix splitName",
        ["api.mjs"],
        ["../outside.mjs", "linked.mjs", "oversized.mjs", "binary.mjs", "valid.mjs"],
    )
    assert [e.path for e in excerpts] == ["valid.mjs"]


def test_production_callers_preferred_over_smoke_and_tests(tmp_path):
    (tmp_path / "api.py").write_text("def split_name(full):\n    return full.split(' ', 1)\n")
    (tmp_path / "consumer.py").write_text(
        "from api import split_name\nlast, first = split_name('Ada Lovelace')\n"
    )
    (tmp_path / "smoke.py").write_text("from api import split_name\nassert split_name('A B')\n")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_api.py").write_text("from api import split_name\nassert split_name('X Y')\n")

    excerpts = find_related_callers(
        tmp_path,
        "fix split_name",
        ["api.py"],
        ["smoke.py", "tests/test_api.py", "consumer.py"],
    )
    assert [e.path for e in excerpts] == ["consumer.py"]

    # When no production caller exists, test/smoke callers still work as fallback.
    fallback = find_related_callers(
        tmp_path,
        "fix split_name",
        ["api.py"],
        ["smoke.py", "tests/test_api.py"],
    )
    assert [e.path for e in fallback] == ["smoke.py", "tests/test_api.py"]
