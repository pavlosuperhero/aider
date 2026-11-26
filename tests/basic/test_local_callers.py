from dataclasses import FrozenInstanceError

import pytest

from aider.local_callers import (
    DEFAULT_CALLER_HINT,
    CallerEvidence,
    CallerEvidenceReport,
    CallerEvidenceResult,
    CallerExcerpt,
    CallerScope,
    CompositeCallerFinder,
    PythonAstCallerFinder,
    discover_related_callers,
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
    # Exact match takes strict precedence over sibling definitions, while a vague prompt
    # on a single-definition module seeds that exported definition via bounded fallback.
    assert related_callers(tmp_path, "fix parser", ["api.py"], candidates) == (
        "consumer.py:1\nfrom api import parse as p\nvalue = p(1) + 1"
    )
    (tmp_path / "api.py").write_text(
        "def parse(value): return value\ndef parser(value): return value\n"
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


def test_evidence_report_names_definition_without_a_matching_caller(tmp_path):
    from aider.local_callers import CallerEvidenceReport

    (tmp_path / "api.py").write_text("def process(): pass")
    (tmp_path / "other.py").write_text("print('hello')")
    report = CallerEvidenceReport()
    excerpts = find_related_callers(
        tmp_path, "fix process", ["api.py"], ["other.py"], report=report
    )
    assert not excerpts
    assert "NO_EVIDENCE" in report.describe(excerpts)
    assert "api.py:process" in report.describe(excerpts)


def test_evidence_report_marks_ambiguous_method_receiver(tmp_path):
    from aider.local_callers import CallerEvidenceReport

    (tmp_path / "api.py").write_text("class A:\n    def convert(self): pass")
    (tmp_path / "other.py").write_text("from api import A\ndef process(obj): return obj.convert()")
    report = CallerEvidenceReport()
    excerpts = find_related_callers(
        tmp_path, "fix convert", ["api.py"], ["other.py"], report=report
    )
    assert "ambiguous method receiver" in report.describe(excerpts)
    assert excerpts[0].incomplete


def test_non_js_tree_sitter_caller_is_not_flagged_with_js_import_ambiguity(tmp_path):
    from aider.local_callers import CallerEvidenceReport

    (tmp_path / "api.go").write_text("package p\nfunc FormatPrice(c int) string { return \"\" }\n")
    (tmp_path / "consumer.go").write_text(
        "package p\nfunc Use() string { return FormatPrice(10) }\n"
    )
    report = CallerEvidenceReport()
    excerpts = find_related_callers(
        tmp_path, "fix FormatPrice", ["api.go"], ["consumer.go"], report=report
    )
    assert len(excerpts) == 1
    assert not excerpts[0].incomplete
    assert "SELECTED" in report.describe(excerpts)

    # Duplicate definition across two editable Go files IS still marked ambiguous.
    (tmp_path / "api2.go").write_text("package p\nfunc FormatPrice(c int) string { return \"\" }\n")
    dup_excerpts = find_related_callers(
        tmp_path, "fix FormatPrice", ["api.go", "api2.go"], ["consumer.go"]
    )
    assert len(dup_excerpts) == 1
    assert dup_excerpts[0].incomplete
    assert "ambiguous symbol binding" in dup_excerpts[0].limitations


def test_discover_related_callers_returns_immutable_typed_result_and_all_fields(tmp_path):
    (tmp_path / "api.py").write_text("def parse(value):\n    return value\n")
    (tmp_path / "consumer.py").write_text(
        "from api import parse\n"
        "\n"
        "# line 3\n"
        "# line 4\n"
        "# line 5\n"
        "result = parse('ok')\n"
    )
    (tmp_path / "unused.py").write_text("print('no match')\n")

    no_ev_report = CallerEvidenceReport()
    no_ev = discover_related_callers(
        tmp_path, "fix parse", ["api.py"], ["unused.py"], report=no_ev_report
    )
    assert isinstance(no_ev, CallerEvidenceResult)
    assert no_ev.status == "NO_EVIDENCE"
    assert no_ev.evidence == ()
    assert no_ev.excerpts == ()
    assert no_ev.definitions == (("api.py", "parse"),)
    assert isinstance(no_ev.scope, CallerScope)
    assert no_ev.scope.pool == "production"
    assert no_ev.ambiguity == ()
    assert no_ev.truncation == ()
    assert no_ev_report.result == no_ev
    assert "NO_EVIDENCE" in no_ev_report.describe()

    report = CallerEvidenceReport()
    complete = discover_related_callers(
        tmp_path, "fix parse", ["api.py"], ["consumer.py", "unused.py"], report=report
    )
    assert complete.status == "COMPLETE_WITHIN_SCOPE"
    assert complete.has_evidence is True
    assert complete.incomplete is False
    assert complete.ambiguity == ()
    assert complete.truncation == ()
    assert complete.limitations == ()
    assert complete.sources == ("python_ast",)
    assert len(complete.evidence) == 1

    item = complete.evidence[0]
    assert isinstance(item, CallerEvidence)
    assert item.path == "consumer.py"
    assert item.line == 6
    assert item.start_line == 4
    assert item.symbol == "parse"
    assert item.kind == "direct_call"
    assert item.source == "python_ast"
    assert item.scope == "production"
    assert item.ambiguity == ()
    assert item.truncation == ()
    assert report.result == complete
    assert "COMPLETE_WITHIN_SCOPE" in report.describe()
    assert "SELECTED" in report.describe(complete.excerpts)
    assert "consumer.py:4" in format_caller_context(complete, mode="passive")

    # Public result and nested DTOs are strictly immutable.
    with pytest.raises(FrozenInstanceError):
        complete.status = "NO_EVIDENCE"
    with pytest.raises(FrozenInstanceError):
        complete.scope.pool = "tests"
    with pytest.raises(FrozenInstanceError):
        item.line = 1
    with pytest.raises(FrozenInstanceError):
        complete.excerpts[0].symbol = "other"

    with pytest.raises(ValueError, match="Unknown caller evidence status"):
        CallerEvidenceResult(status="INVALID")  # type: ignore[arg-type]


def test_duplicate_python_definitions_across_and_within_files_report_ambiguity(tmp_path):
    (tmp_path / "a.py").write_text("def parse(v):\n    return v\n")
    (tmp_path / "b.py").write_text("def parse(v):\n    return v\n")
    (tmp_path / "consumer.py").write_text("from a import parse\nval = parse(1)\n")

    cross_file = discover_related_callers(
        tmp_path, "fix parse", ["a.py", "b.py"], ["consumer.py"]
    )
    assert cross_file.status == "INCOMPLETE"
    assert cross_file.definitions == (("a.py", "parse"), ("b.py", "parse"))
    assert "duplicate symbol definition" in cross_file.ambiguity
    assert "duplicate symbol definition" in cross_file.evidence[0].ambiguity
    assert cross_file.excerpts[0].incomplete is True

    (tmp_path / "dup_methods.py").write_text(
        "class First:\n"
        "    def copy(self): return 1\n"
        "class Second:\n"
        "    def copy(self): return 2\n"
    )
    (tmp_path / "method_use.py").write_text(
        "from dup_methods import First\nout = First().copy()\n"
    )
    within_file = discover_related_callers(
        tmp_path, "fix copy", ["dup_methods.py"], ["method_use.py"]
    )
    assert within_file.status == "INCOMPLETE"
    assert "ambiguous method receiver" in within_file.ambiguity
    assert "duplicate symbol definition" in within_file.ambiguity
    assert within_file.evidence[0].kind == "method_call"


def test_duplicate_non_python_definitions_and_duplicate_candidates(tmp_path):
    (tmp_path / "api.go").write_text('package p\nfunc FormatPrice(c int) string { return "" }\n')
    (tmp_path / "api2.go").write_text('package p\nfunc FormatPrice(c int) string { return "" }\n')
    (tmp_path / "consumer.go").write_text(
        "package p\nfunc Use() string { return FormatPrice(10) }\n"
    )

    dup_go = discover_related_callers(
        tmp_path, "fix FormatPrice", ["api.go", "api2.go"], ["consumer.go", "./consumer.go"]
    )
    assert dup_go.status == "INCOMPLETE"
    assert dup_go.scope.candidates_total == 1
    assert len(dup_go.evidence) == 1
    assert "ambiguous symbol binding" in dup_go.ambiguity
    assert "duplicate symbol definition" in dup_go.ambiguity

    (tmp_path / "api.py").write_text("def parse(x):\n    return x\n")
    (tmp_path / "consumer.py").write_text("from api import parse\nout = parse(1)\n")
    deduped = discover_related_callers(
        tmp_path,
        "fix parse",
        ["api.py"],
        ["consumer.py", "./consumer.py", "consumer.py"],
    )
    assert deduped.status == "COMPLETE_WITHIN_SCOPE"
    assert deduped.scope.candidates_total == 1
    assert len(deduped.evidence) == 1


def test_multiple_calls_in_same_file_report_truncation_when_outside_window(tmp_path):
    (tmp_path / "api.py").write_text("def parse(x):\n    return x\n")
    padding = "\n".join(f"# spacer {i}" for i in range(15))
    (tmp_path / "consumer.py").write_text(
        f"from api import parse\nfirst = parse(1)\n{padding}\nsecond = parse(2)\n"
    )

    result = discover_related_callers(tmp_path, "fix parse", ["api.py"], ["consumer.py"])
    assert result.status == "INCOMPLETE"
    assert result.evidence[0].line == 2
    assert "additional calls in file omitted" in result.truncation
    assert "additional calls in file omitted" in result.evidence[0].truncation
    assert result.excerpts[0].incomplete is True


def test_capped_production_callers_report_scope_and_truncation(tmp_path):
    (tmp_path / "api.py").write_text("def parse(x):\n    return x\n")
    prod_files = [f"prod_{i}.py" for i in range(5)]
    for name in prod_files:
        (tmp_path / name).write_text("from api import parse\nout = parse(1)\n")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    test_files = ["tests/test_a.py", "tests/test_b.py"]
    for name in test_files:
        (tmp_path / name).write_text("from api import parse\nassert parse(1) == 1\n")

    result = discover_related_callers(
        tmp_path,
        "fix parse",
        ["api.py"],
        [*prod_files, *test_files],
        max_files=3,
    )
    assert result.status == "INCOMPLETE"
    assert result.scope.pool == "production"
    assert result.scope.candidates_total == 7
    assert result.scope.production_candidates == 5
    assert result.scope.test_candidates == 2
    assert result.scope.omitted_test_candidates == 2
    assert result.scope.selected_files == 3
    assert result.scope.matched_files == 4
    assert len(result.evidence) == 3
    assert all(item.scope == "production" for item in result.evidence)
    assert "additional files omitted" in result.truncation
    assert any(e.incomplete for e in result.excerpts)


def test_capped_test_callers_fallback_reports_scope_and_truncation(tmp_path):
    (tmp_path / "api.py").write_text("def parse(x):\n    return x\n")
    (tmp_path / "unrelated_prod.py").write_text("value = 123\n")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    test_files = [f"tests/test_case_{i}.py" for i in range(5)]
    for name in test_files:
        (tmp_path / name).write_text("from api import parse\nassert parse(1) == 1\n")

    result = discover_related_callers(
        tmp_path,
        "fix parse",
        ["api.py"],
        ["unrelated_prod.py", *test_files],
        max_files=3,
    )
    assert result.status == "INCOMPLETE"
    assert result.scope.pool == "tests"
    assert result.scope.candidates_total == 6
    assert result.scope.candidates_searched == 6
    assert result.scope.production_candidates == 1
    assert result.scope.test_candidates == 5
    assert result.scope.omitted_test_candidates == 0
    assert result.scope.selected_files == 3
    assert result.scope.matched_files == 4
    assert len(result.evidence) == 3
    assert all(item.scope == "tests" for item in result.evidence)
    assert "additional files omitted" in result.truncation
    assert any(e.incomplete for e in result.excerpts)


def test_uncapped_production_and_test_fallback_report_complete_within_scope(tmp_path):
    (tmp_path / "api.py").write_text("def parse(x):\n    return x\n")
    (tmp_path / "consumer.py").write_text("from api import parse\nout = parse(1)\n")
    (tmp_path / "smoke.py").write_text("from api import parse\nassert parse(1) == 1\n")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_api.py").write_text("from api import parse\nassert parse(1) == 1\n")

    prod_res = discover_related_callers(
        tmp_path,
        "fix parse",
        ["api.py"],
        ["consumer.py", "smoke.py", "tests/test_api.py"],
    )
    assert prod_res.status == "COMPLETE_WITHIN_SCOPE"
    assert prod_res.scope.pool == "production"
    assert prod_res.scope.omitted_test_candidates == 2
    assert prod_res.ambiguity == ()
    assert prod_res.truncation == ()
    assert [e.path for e in prod_res.evidence] == ["consumer.py"]

    test_res = discover_related_callers(
        tmp_path,
        "fix parse",
        ["api.py"],
        ["smoke.py", "tests/test_api.py"],
    )
    assert test_res.status == "COMPLETE_WITHIN_SCOPE"
    assert test_res.scope.pool == "tests"
    assert test_res.scope.omitted_test_candidates == 0
    assert test_res.ambiguity == ()
    assert test_res.truncation == ()
    assert [e.path for e in test_res.evidence] == ["smoke.py", "tests/test_api.py"]


def test_injectable_custom_finder_receives_report_and_propagates_limitations(tmp_path):
    seen_reports = []

    class FakeCymbalAdapter:
        source = "cymbal"

        def supports(self, path: str) -> bool:
            return path.endswith(".py")

        def find_callers(
            self,
            root,
            message,
            editable,
            candidates,
            *,
            max_files=3,
            max_chars=700,
            context_lines=3,
            report=None,
        ):
            seen_reports.append(report)
            if report is not None:
                report.record_definition("models.py", "copy")
                report.record_ambiguity("stale index")
                report.record_ambiguity("ambiguous alias")
                report.record_truncation("cymbal refs capped")
            return (
                CallerExcerpt(
                    path="models.py",
                    start_line=456,
                    line=458,
                    symbol="copy",
                    code="p.copy()",
                    kind="reference",
                    source="cymbal",
                ),
                CallerExcerpt(
                    path="models.py",
                    start_line=456,
                    line=459,
                    symbol="copy",
                    code="p.copy()",
                    kind="reference",
                    source="cymbal",
                ),
            )

    report = CallerEvidenceReport()
    adapter = FakeCymbalAdapter()
    result = discover_related_callers(
        tmp_path,
        "fix copy",
        ["structures.py"],
        ["models.py"],
        report=report,
        finder=adapter,
    )
    assert seen_reports == [report]
    assert result.status == "INCOMPLETE"
    assert result.sources == ("cymbal",)
    assert result.definitions == (("models.py", "copy"),)
    assert [(item.path, item.line, item.kind, item.source) for item in result.evidence] == [
        ("models.py", 458, "reference", "cymbal"),
        ("models.py", 459, "reference", "cymbal"),
    ]
    assert result.scope.matched_files == 1
    assert result.scope.selected_files == 1
    assert "stale index" in result.ambiguity
    assert "ambiguous alias" in result.ambiguity
    assert "cymbal refs capped" in result.truncation
    assert all(e.incomplete for e in result.excerpts)
    assert "stale index" in report.describe()

    # find_related_callers also supports injectable finder.
    excerpts = find_related_callers(
        tmp_path, "fix copy", ["structures.py"], ["models.py"], finder=adapter
    )
    assert len(excerpts) == 2
    assert excerpts[0].line == 458
    assert excerpts[1].line == 459


def test_composite_merges_duplicate_matches_and_supports_legacy_and_empty_adapters(tmp_path):
    (tmp_path / "api.py").write_text("def parse(x):\n    return x\n")
    (tmp_path / "consumer.py").write_text("from api import parse\nout = parse(1)\n")

    class LegacyNoReportFinder:
        source = "legacy_adapter"

        def supports(self, path: str) -> bool:
            return path.endswith(".py")

        def find_callers(
            self,
            root,
            message,
            editable,
            candidates,
            *,
            max_files=3,
            max_chars=700,
            context_lines=3,
        ):
            return (
                CallerExcerpt(
                    path="consumer.py",
                    start_line=1,
                    line=2,
                    symbol="parse",
                    code="from api import parse\nout = parse(1)",
                    limitations=("stale index", "excerpt truncated"),
                ),
            )

    composite = CompositeCallerFinder((LegacyNoReportFinder(), PythonAstCallerFinder()))
    merged = discover_related_callers(
        tmp_path,
        "fix parse",
        ["api.py"],
        ["consumer.py"],
        finder=composite,
    )
    assert merged.status == "INCOMPLETE"
    assert len(merged.evidence) == 1
    assert merged.evidence[0].line == 2
    assert "stale index" in merged.ambiguity
    assert "excerpt truncated" in merged.truncation
    assert set(merged.sources) == {"legacy_adapter", "python_ast"}
    assert merged.excerpts[0].incomplete is True

    class EmptyWithWarningAdapter:
        source = "cymbal_probe"

        def supports(self, path: str) -> bool:
            return path.endswith(".py")

        def find_callers(
            self,
            root,
            message,
            editable,
            candidates,
            *,
            max_files=3,
            max_chars=700,
            context_lines=3,
            report=None,
        ):
            if report is not None:
                report.record_ambiguity("stale index")
            return ()

    combined = CompositeCallerFinder((EmptyWithWarningAdapter(), PythonAstCallerFinder()))
    propagated = discover_related_callers(
        tmp_path,
        "fix parse",
        ["api.py"],
        ["consumer.py"],
        finder=combined,
    )
    assert propagated.status == "INCOMPLETE"
    assert "stale index" in propagated.ambiguity
    assert propagated.excerpts[-1].incomplete is True
    assert "stale index" in propagated.excerpts[-1].ambiguity


def test_c_cpp_header_prototypes_rejected_while_c_and_inline_hpp_calls_preserved(tmp_path):
    from aider.local_callers import TreeSitterCallerFinder

    (tmp_path / "yajl_parser.c").write_text(
        "long long yajl_parse_integer(const unsigned char *number, unsigned int length) {\n"
        "    return (long long)length;\n"
        "}\n"
        "YAJL_API int yajl_parse(yajl_handle hand, const unsigned char *jsonText, size_t len) {\n"
        "    return 0;\n"
        "}\n"
        "int redisReaderGetReply(redisReader *r, void **reply) {\n"
        "    return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "yajl_parse.h").write_text(
        "extern long long yajl_parse_integer(\n"
        "    const unsigned char *number,\n"
        "    unsigned int length);\n"
        "extern long long\n"
        "yajl_parse_integer(const unsigned char *number, unsigned int length);\n"
        "YAJL_API int yajl_parse(yajl_handle hand, const unsigned char *jsonText, size_t len);\n"
        "int redisReaderGetReply(redisReader *r, void **reply);\n"
        "typedef int (*parse_cb)(int x);\n",
        encoding="utf-8",
    )
    (tmp_path / "consumer.c").write_text(
        '#include "yajl_parse.h"\n'
        "long long run_parse(const unsigned char *buf, unsigned int n) {\n"
        "    long long val = yajl_parse_integer(buf, n);\n"
        "    return val;\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "inline_wrap.hpp").write_text(
        "extern long long yajl_parse_integer(const unsigned char *number, unsigned int length);\n"
        "struct Helper {\n"
        "    int redisReaderGetReply(redisReader *r, void **reply);\n"
        "    inline long long wrap(const unsigned char *s, unsigned int n) {\n"
        "        return yajl_parse_integer(s, n);\n"
        "    }\n"
        "};\n"
        "inline long long one_liner(const unsigned char *s, unsigned int n) "
        "{ return yajl_parse_integer(s, n); }\n",
        encoding="utf-8",
    )

    # Header-only candidates must NOT match C/C++ prototypes as callers.
    header_only = discover_related_callers(
        tmp_path,
        "fix yajl_parse_integer yajl_parse redisReaderGetReply",
        ["yajl_parser.c"],
        ["yajl_parse.h"],
    )
    assert header_only.status == "NO_EVIDENCE"
    assert header_only.excerpts == ()

    # Real calls in .c and inline .hpp function bodies ARE preserved (and header skipped).
    real_calls = discover_related_callers(
        tmp_path,
        "fix yajl_parse_integer",
        ["yajl_parser.c"],
        ["yajl_parse.h", "consumer.c", "inline_wrap.hpp"],
    )
    assert [e.path for e in real_calls.excerpts] == ["consumer.c", "inline_wrap.hpp"]
    assert real_calls.evidence[0].line == 3
    assert "yajl_parse_integer(buf, n)" in real_calls.excerpts[0].code
    assert real_calls.evidence[1].line == 5
    assert "return yajl_parse_integer(s, n);" in real_calls.excerpts[1].code

    # Direct regex fallback check: prototypes rejected, inline & .c calls matched.
    call_names = {
        "yajl_parse_integer": "yajl_parse_integer",
        "yajl_parse": "yajl_parse",
        "redisReaderGetReply": "redisReaderGetReply",
    }
    header_src = (tmp_path / "yajl_parse.h").read_text(encoding="utf-8")
    assert TreeSitterCallerFinder._find_regex_call_lines(header_src, call_names, {}) == []

    hpp_src = (tmp_path / "inline_wrap.hpp").read_text(encoding="utf-8")
    hpp_regex_hits = TreeSitterCallerFinder._find_regex_call_lines(hpp_src, call_names, {})
    assert hpp_regex_hits == [(4, "yajl_parse_integer"), (7, "yajl_parse_integer")]

    c_src = (tmp_path / "consumer.c").read_text(encoding="utf-8")
    c_regex_hits = TreeSitterCallerFinder._find_regex_call_lines(c_src, call_names, {})
    assert c_regex_hits == [(2, "yajl_parse_integer")]


def test_large_file_skip_marks_report_incomplete_even_with_test_fallback(tmp_path):
    (tmp_path / "api.py").write_text(
        "def normalize_code(raw):\n    return raw.strip().upper()\n",
        encoding="utf-8",
    )
    padding = "\n".join(f"# padding line {i:04d} " + ("x" * 60) for i in range(2200))
    (tmp_path / "consumer.py").write_text(
        "from api import normalize_code\n"
        f"{padding}\n"
        "def route_code(raw):\n"
        "    code = normalize_code(raw)\n"
        "    return 'fallback-queue' if code == 'MISSING_CODE' else f'queue:{code}'\n",
        encoding="utf-8",
    )
    assert (tmp_path / "consumer.py").stat().st_size > 131072
    (tmp_path / "test_smoke.py").write_text(
        "from api import normalize_code\nassert normalize_code(' ab-1 ') == 'AB-1'\n",
        encoding="utf-8",
    )

    # Production consumer.py skipped for size, falls back to test_smoke.py -> MUST be INCOMPLETE.
    report = CallerEvidenceReport()
    result = discover_related_callers(
        tmp_path,
        "update normalize_code",
        ["api.py"],
        ["consumer.py", "test_smoke.py"],
        report=report,
    )
    assert result.status == "INCOMPLETE"
    assert report.skipped_large == 1
    assert result.scope.skipped_large == 1
    assert result.scope.pool == "tests"
    assert [e.path for e in result.excerpts] == ["test_smoke.py"]
    assert result.excerpts[0].incomplete is True
    assert any("128 KiB" in note for note in result.truncation)
    desc = report.describe()
    assert "INCOMPLETE" in desc
    assert "COMPLETE_WITHIN_SCOPE" not in desc
    assert "skipped_large=1" in desc

    # Only oversized candidate with no matching fallback -> still INCOMPLETE (not NO_EVIDENCE).
    only_large_report = CallerEvidenceReport()
    only_large = discover_related_callers(
        tmp_path,
        "update normalize_code",
        ["api.py"],
        ["consumer.py"],
        report=only_large_report,
    )
    assert only_large.status == "INCOMPLETE"
    assert only_large.excerpts == ()
    assert only_large_report.skipped_large == 1
    assert any("128 KiB" in note for note in only_large.truncation)

    # Non-Python oversized candidate alongside a valid caller -> also INCOMPLETE.
    (tmp_path / "api.mjs").write_text("export function splitName(s) { return s.split(' '); }\n")
    (tmp_path / "oversized.mjs").write_text(
        "import { splitName } from './api.mjs';\nsplitName('a b');\n//" + ("x" * 140000)
    )
    (tmp_path / "valid.mjs").write_text(
        "import { splitName } from './api.mjs';\nconst out = splitName('a b');\n"
    )
    js_res = discover_related_callers(
        tmp_path,
        "fix splitName",
        ["api.mjs"],
        ["oversized.mjs", "valid.mjs"],
    )
    assert js_res.status == "INCOMPLETE"
    assert js_res.scope.skipped_large == 1
    assert [e.path for e in js_res.excerpts] == ["valid.mjs"]
    assert js_res.excerpts[0].incomplete is True


def test_builtin_c_cpp_non_header_skips_unbacked_declarations_while_headers_keep_them(tmp_path):
    from aider.local_callers import TreeSitterCallerFinder

    finder = TreeSitterCallerFinder()
    c_src = (
        "extern int parse_value(int input);\n"
        "int helper(int x);\n"
        "int *ptr_func(int x) { return 0; }\n"
        "int real_func(int x) {\n"
        "    extern int block_extern(int y);\n"
        "    return block_extern(x);\n"
        "}\n"
    )
    cpp_src = (
        "extern int parse_value(int input);\n"
        "int helper(int x);\n"
        "class Engine {\n"
        "public:\n"
        "    int unbacked_method(int x);\n"
        "    int inline_method(int x) {\n"
        "        extern int block_extern(int y);\n"
        "        return block_extern(x);\n"
        "    }\n"
        "};\n"
        "int *ptr_func(int x) { return nullptr; }\n"
        "int Engine::out_of_line(int x) { return x; }\n"
    )
    h_src = (
        "extern int parse_value(int input);\n"
        "int helper(int x);\n"
    )
    hpp_src = (
        "extern int parse_value(int input);\n"
        "class Engine {\n"
        "public:\n"
        "    int decl_method(int x);\n"
        "    int inline_method(int x) {\n"
        "        extern int block_extern(int y);\n"
        "        return block_extern(x);\n"
        "    }\n"
        "};\n"
    )

    c_defs = finder._extract_definitions("mod.c", c_src)
    assert "ptr_func" in c_defs
    assert "real_func" in c_defs
    assert "parse_value" not in c_defs
    assert "helper" not in c_defs
    assert "block_extern" not in c_defs

    cpp_defs = finder._extract_definitions("mod.cpp", cpp_src)
    assert {"inline_method", "ptr_func", "out_of_line"}.issubset(cpp_defs)
    assert "parse_value" not in cpp_defs
    assert "helper" not in cpp_defs
    assert "unbacked_method" not in cpp_defs
    assert "block_extern" not in cpp_defs

    h_defs = finder._extract_definitions("api.h", h_src)
    assert {"parse_value", "helper"}.issubset(h_defs)

    hpp_defs = finder._extract_definitions("api.hpp", hpp_src)
    assert {"parse_value", "decl_method", "inline_method"}.issubset(hpp_defs)
    assert "block_extern" not in hpp_defs

    # End-to-end: editable .c with only unbacked extern declaration does NOT produce false matches.
    (tmp_path / "mod.c").write_text("extern int parse_value(int input);\n", encoding="utf-8")
    (tmp_path / "consumer.c").write_text(
        "int run(int x) { return parse_value(x); }\n", encoding="utf-8"
    )
    res = discover_related_callers(tmp_path, "fix parse_value", ["mod.c"], ["consumer.c"])
    assert res.status == "NO_EVIDENCE"
    assert res.definitions == ()
    assert res.excerpts == ()


def test_oversized_editable_file_marks_builtin_result_incomplete(tmp_path):
    padding = "\n".join(f"# pad {i:04d} " + ("x" * 64) for i in range(2200))
    (tmp_path / "big_api.py").write_text(
        f"{padding}\ndef normalize_code(raw):\n    return raw.strip()\n",
        encoding="utf-8",
    )
    (tmp_path / "consumer.py").write_text(
        "from big_api import normalize_code\nval = normalize_code('a')\n",
        encoding="utf-8",
    )
    py_report = CallerEvidenceReport()
    py_res = discover_related_callers(
        tmp_path,
        "fix normalize_code",
        ["big_api.py"],
        ["consumer.py"],
        report=py_report,
    )
    assert py_res.status == "INCOMPLETE"
    assert py_res.excerpts == ()
    assert py_report.skipped_large == 1
    assert py_res.scope.skipped_large == 1
    assert any("128 KiB" in note for note in py_res.truncation)

    cpp_padding = "\n".join(f"// pad {i:04d} " + ("y" * 64) for i in range(2200))
    (tmp_path / "big_api.cpp").write_text(
        f"{cpp_padding}\nint parse_value(int x) {{ return x + 1; }}\n",
        encoding="utf-8",
    )
    (tmp_path / "consumer.cpp").write_text(
        "int handle(int v) { return parse_value(v); }\n",
        encoding="utf-8",
    )
    cpp_report = CallerEvidenceReport()
    cpp_res = discover_related_callers(
        tmp_path,
        "fix parse_value",
        ["big_api.cpp"],
        ["consumer.cpp"],
        report=cpp_report,
    )
    assert cpp_res.status == "INCOMPLETE"
    assert cpp_res.excerpts == ()
    assert cpp_report.skipped_large == 1
    assert cpp_res.scope.skipped_large == 1
    assert any("128 KiB" in note for note in cpp_res.truncation)


@pytest.mark.parametrize(
    "ambiguity_note",
    [
        "ambiguous symbol binding",
        "duplicate symbol definition",
        "ambiguous method receiver",
        "stale index",
    ],
)
def test_builtin_zero_excerpts_with_ambiguity_is_incomplete(ambiguity_note):
    report = CallerEvidenceReport()
    report.record_ambiguity(ambiguity_note)

    res = report.to_result(())
    assert res.status == "INCOMPLETE"
    assert res.excerpts == ()
    assert ambiguity_note in res.ambiguity
    assert ambiguity_note in res.limitations

    clean = CallerEvidenceReport().to_result(())
    assert clean.status == "NO_EVIDENCE"


def test_public_shared_exports_and_backward_compatible_aliases():
    import aider.local_callers as lc

    assert lc.CONTROL_KEYWORDS is lc._CONTROL_KEYWORDS
    assert lc.C_CPP_HEADER_SUFFIXES is lc._C_CPP_HEADER_SUFFIXES
    assert lc.DEF_REGEXES is lc._DEF_REGEXES
    assert lc.LARGE_FILE_LIMITATION is lc._LARGE_FILE_LIMITATION
    assert lc.WORD_RE is lc._WORD_RE
    assert (
        lc.has_function_definition_declarator_ancestor
        is lc._has_function_definition_declarator_ancestor
    )
    assert lc.is_c_cpp_header is lc._is_c_cpp_header
    assert lc.is_test_path is lc._is_test_path
    assert lc.resolve_lang is lc._resolve_lang
    assert callable(lc.select_target_definitions)


def test_select_target_definitions_tiers_stopwords_and_bounds():
    from aider.local_callers import select_target_definitions

    # Tier 1: exact symbol match takes strict precedence over sub-token or fallback matches.
    exact_syms, tier = select_target_definitions(
        ["parse", "parse_integer", "helper"], {"fix", "parse", "integer"}
    )
    assert exact_syms == {"parse"}
    assert tier == "exact"

    # Tier 2: camelCase + snake_case sub-token overlap ignores generic stopwords and ranks deterministically.
    sub_syms, tier = select_target_definitions(
        ["yajl_parse_integer", "yajl_parse", "getValue", "handleError"],
        {"Fix", "integer", "parsing", "for", "non", "digit", "input"},
    )
    assert sub_syms == {"yajl_parse_integer"}
    assert tier == "subtoken"

    camel_syms, tier = select_target_definitions(
        ["redisReaderGetReply", "readLine", "processBulkItem"],
        {"Fix", "reader", "reply", "handling"},
    )
    assert camel_syms == {"redisReaderGetReply"}
    assert tier == "subtoken"

    # Tier 3: bounded exported fallback skips _private and test_* when exported definitions exist.
    fb_syms, tier = select_target_definitions(
        ["_internal", "test_alpha", "zeta", "alpha", "beta", "gamma"],
        {"Handle", "CRLF", "recovery"},
        max_fallback=2,
    )
    assert fb_syms == {"alpha", "beta"}
    assert tier == "fallback"

    # Tier 3 falls back to non-test private definitions only when all non-test definitions are private.
    priv_syms, tier = select_target_definitions(
        ["_priv_b", "_priv_a", "test_priv"],
        {"Handle", "CRLF", "recovery"},
        max_fallback=1,
    )
    assert priv_syms == {"_priv_a"}
    assert tier == "fallback"

    # Empty definitions or empty task words return no targets.
    assert select_target_definitions([], {"fix", "parse"}) == (set(), "none")
    assert select_target_definitions(["parse"], set()) == (set(), "none")


def test_same_file_callers_across_python_c_cpp_js_ts_rust_with_precision_guards(tmp_path):
    # 1. Python: sibling & top-level same-file callers discovered; self-recursion excluded;
    # cross-file production caller ranked ahead of same-file caller.
    (tmp_path / "py_self_only.py").write_text(
        "def factorial(n):\n"
        "    if n <= 1:\n"
        "        return 1\n"
        "    return n * factorial(n - 1)\n",
        encoding="utf-8",
    )
    self_only_py = discover_related_callers(
        tmp_path, "fix factorial", ["py_self_only.py"], ["py_self_only.py"]
    )
    assert self_only_py.status == "NO_EVIDENCE"
    assert self_only_py.excerpts == ()

    (tmp_path / "api.py").write_text(
        "def parse(value):\n"
        "    if isinstance(value, list):\n"
        "        return [parse(v) for v in value]\n"
        "    return int(value)\n\n"
        "def outer(raw):\n"
        "    return parse(raw) + 10\n",
        encoding="utf-8",
    )
    same_py = discover_related_callers(tmp_path, "fix parse", ["api.py"], [])
    assert same_py.status == "COMPLETE_WITHIN_SCOPE"
    assert [e.path for e in same_py.excerpts] == ["api.py"]
    assert same_py.evidence[0].line == 7
    assert "return parse(raw) + 10" in same_py.excerpts[0].code
    assert "additional calls in file omitted" not in same_py.truncation

    (tmp_path / "z_consumer.py").write_text(
        "from api import parse\nval = parse('5')\n",
        encoding="utf-8",
    )
    ranked_py = discover_related_callers(
        tmp_path, "fix parse", ["api.py"], ["api.py", "z_consumer.py"]
    )
    assert [e.path for e in ranked_py.excerpts] == ["z_consumer.py", "api.py"]

    # 2. C: exact yajl_parser.c scenario — prototype, definition, and self-recursion excluded,
    # while sibling caller yajl_do_parse IS discovered.
    (tmp_path / "yajl_parser.c").write_text(
        "long long yajl_parse_integer(const unsigned char *number, unsigned int length);\n"
        "\n"
        "long long yajl_parse_integer(const unsigned char *number, unsigned int length) {\n"
        "    if (length == 0) return yajl_parse_integer(number, 1);\n"
        "    return (long long)length;\n"
        "}\n"
        "\n"
        "int yajl_do_parse(const unsigned char *buf, unsigned int len) {\n"
        "    long long i = yajl_parse_integer(buf, len);\n"
        "    if ((i == LLONG_MIN || i == LLONG_MAX) && errno == ERANGE) {\n"
        "        return 1;\n"
        "    }\n"
        "    return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    c_res = discover_related_callers(
        tmp_path, "fix yajl_parse_integer", ["yajl_parser.c"], ["yajl_parser.c"]
    )
    assert c_res.status == "COMPLETE_WITHIN_SCOPE"
    assert [e.path for e in c_res.excerpts] == ["yajl_parser.c"]
    assert c_res.evidence[0].line == 9
    assert "yajl_parse_integer(buf, len)" in c_res.excerpts[0].code
    assert "LLONG_MIN" in c_res.excerpts[0].code

    # 3. C++ same-file caller (prototype & self-recursion excluded).
    (tmp_path / "calc.cpp").write_text(
        "int compute_sum(int n);\n"
        "int compute_sum(int n) {\n"
        "    if (n <= 0) return 0;\n"
        "    return n + compute_sum(n - 1);\n"
        "}\n"
        "int run_total(int k) {\n"
        "    return compute_sum(k) * 2;\n"
        "}\n",
        encoding="utf-8",
    )
    cpp_res = discover_related_callers(
        tmp_path, "fix compute_sum", ["calc.cpp"], ["calc.cpp"]
    )
    assert cpp_res.status == "COMPLETE_WITHIN_SCOPE"
    assert [e.path for e in cpp_res.excerpts] == ["calc.cpp"]
    assert cpp_res.evidence[0].line == 7
    assert "compute_sum(k) * 2" in cpp_res.excerpts[0].code

    # 4. JS/TS same-file caller without false ambiguous symbol binding.
    (tmp_path / "parser.ts").write_text(
        "export function parsePrice(raw: string): number {\n"
        "  if (raw.startsWith(' ')) return parsePrice(raw.trim());\n"
        "  return Number(raw);\n"
        "}\n"
        "export function total(raw: string): number {\n"
        "  return parsePrice(raw) * 100;\n"
        "}\n",
        encoding="utf-8",
    )
    ts_res = discover_related_callers(
        tmp_path, "fix parsePrice", ["parser.ts"], ["parser.ts"]
    )
    assert ts_res.status == "COMPLETE_WITHIN_SCOPE"
    assert [e.path for e in ts_res.excerpts] == ["parser.ts"]
    assert ts_res.evidence[0].line == 6
    assert "parsePrice(raw) * 100" in ts_res.excerpts[0].code
    assert "ambiguous symbol binding" not in ts_res.ambiguity

    # 5. Rust same-file caller (definition & self-recursion excluded).
    (tmp_path / "parse.rs").write_text(
        "pub fn parse_num(s: &str) -> i64 {\n"
        "    if s.starts_with(' ') { return parse_num(s.trim()); }\n"
        "    0\n"
        "}\n"
        "pub fn run(s: &str) -> i64 {\n"
        "    parse_num(s) + 1\n"
        "}\n",
        encoding="utf-8",
    )
    rs_res = discover_related_callers(tmp_path, "fix parse_num", ["parse.rs"], ["parse.rs"])
    assert rs_res.status == "COMPLETE_WITHIN_SCOPE"
    assert [e.path for e in rs_res.excerpts] == ["parse.rs"]
    assert rs_res.evidence[0].line == 6
    assert "parse_num(s) + 1" in rs_res.excerpts[0].code


def test_vague_prompt_seeding_finds_callers_and_extern_only_c_stays_empty(tmp_path):
    # Sub-token match: "Fix integer parsing for non-digit input" seeds yajl_parse_integer.
    (tmp_path / "yajl_parser.c").write_text(
        "long long yajl_parse_integer(const unsigned char *number, unsigned int length) {\n"
        "    return (long long)length;\n"
        "}\n"
        "int yajl_do_parse(const unsigned char *buf, unsigned int len) {\n"
        "    long long i = yajl_parse_integer(buf, len);\n"
        "    return (int)i;\n"
        "}\n",
        encoding="utf-8",
    )
    sub_res = discover_related_callers(
        tmp_path,
        "Fix integer parsing for non-digit input",
        ["yajl_parser.c"],
        ["yajl_parser.c"],
    )
    assert ("yajl_parser.c", "yajl_parse_integer") in sub_res.definitions
    assert [e.path for e in sub_res.excerpts] == ["yajl_parser.c"]
    assert sub_res.evidence[0].symbol == "yajl_parse_integer"

    # Fallback tier: vague prompt with zero sub-token overlap seeds exported Python function.
    (tmp_path / "protocol.py").write_text(
        "def _private_helper(buf):\n"
        "    return buf\n\n"
        "def decode_frame(buf):\n"
        "    return _private_helper(buf)\n",
        encoding="utf-8",
    )
    (tmp_path / "client.py").write_text(
        "from protocol import decode_frame\n"
        "def read_next(buf):\n"
        "    return decode_frame(buf)\n",
        encoding="utf-8",
    )
    fb_res = discover_related_callers(
        tmp_path,
        "Handle CRLF recovery in parser",
        ["protocol.py"],
        ["client.py"],
    )
    assert fb_res.definitions == (("protocol.py", "decode_frame"),)
    assert [e.path for e in fb_res.excerpts] == ["client.py"]
    assert fb_res.evidence[0].symbol == "decode_frame"

    # Negative guard: extern-only .c file still yields definitions == () and NO_EVIDENCE.
    (tmp_path / "extern_only.c").write_text(
        "extern int parse_value(int input);\n", encoding="utf-8"
    )
    (tmp_path / "use_extern.c").write_text(
        "int run(int x) { return parse_value(x); }\n", encoding="utf-8"
    )
    ext_res = discover_related_callers(
        tmp_path,
        "Fix value parsing",
        ["extern_only.c"],
        ["use_extern.c"],
    )
    assert ext_res.status == "NO_EVIDENCE"
    assert ext_res.definitions == ()
    assert ext_res.excerpts == ()


def test_bash_shebang_resolution_definitions_and_command_callers(tmp_path):
    from aider.local_callers import TreeSitterCallerFinder, resolve_lang

    assert resolve_lang("script.sh") == "bash"
    assert resolve_lang("script.bash") == "bash"
    assert resolve_lang("suite.bats") == "bash"
    assert resolve_lang("libexec/rbenv-init", "#!/usr/bin/env bash\necho hi\n") == "bash"
    assert resolve_lang("libexec/rbenv-sh", "#!/bin/sh\necho hi\n") == "bash"
    assert resolve_lang("README", "Plain text file\n") is None

    libexec = tmp_path / "libexec"
    libexec.mkdir()
    (libexec / "rbenv-version-file-read").write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        "read_version_file() {\n"
        "  local file=\"$1\"\n"
        "  cat \"$file\"\n"
        "}\n"
        "function trim_version {\n"
        "  echo \"$1\"\n"
        "}\n",
        encoding="utf-8",
    )
    (libexec / "rbenv-init").write_text(
        "#!/usr/bin/env bash\n"
        "# read_version_file should not match comments\n"
        "read_version_file=1\n"
        'echo "read_version_file"\n'
        "ver=$(read_version_file .ruby-version)\n"
        "trim_version \"$ver\"\n",
        encoding="utf-8",
    )
    (tmp_path / "helper.sh").write_text(
        "#!/bin/sh\n"
        "if read_version_file .ruby-version; then\n"
        "  echo ok\n"
        "fi\n",
        encoding="utf-8",
    )
    (tmp_path / "noise.sh").write_text(
        "#!/bin/sh\n"
        "# read_version_file in comment\n"
        "read_version_file=assigned\n"
        "echo 'read_version_file'\n"
        'echo "read_version_file"\n',
        encoding="utf-8",
    )

    res = discover_related_callers(
        tmp_path,
        "fix read_version_file",
        ["libexec/rbenv-version-file-read"],
        ["libexec/rbenv-init", "helper.sh", "noise.sh"],
    )
    assert res.status == "COMPLETE_WITHIN_SCOPE"
    assert ("libexec/rbenv-version-file-read", "read_version_file") in res.definitions
    assert [e.path for e in res.excerpts] == ["helper.sh", "libexec/rbenv-init"]
    assert res.evidence[0].line == 2
    assert res.evidence[1].line == 5
    assert "ver=$(read_version_file .ruby-version)" in res.excerpts[1].code

    # Direct regex fallback also ignores comments, assignments, definitions, and quoted literals.
    noise_src = (tmp_path / "noise.sh").read_text(encoding="utf-8")
    init_src = (libexec / "rbenv-init").read_text(encoding="utf-8")
    def_src = (libexec / "rbenv-version-file-read").read_text(encoding="utf-8")
    call_names = {"read_version_file": "read_version_file", "trim_version": "trim_version"}
    assert TreeSitterCallerFinder._find_bash_regex_call_lines(noise_src, call_names) == []
    assert TreeSitterCallerFinder._find_bash_regex_call_lines(def_src, call_names) == []
    assert TreeSitterCallerFinder._find_bash_regex_call_lines(init_src, call_names) == [
        (4, "read_version_file"),
        (5, "trim_version"),
    ]


def test_rust_homonymous_trait_method_marks_ambiguous_receiver_incomplete(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "display.rs").write_text(
        "use std::fmt::{Display, Formatter, Result};\n"
        "pub struct Version(pub String);\n"
        "impl Display for Version {\n"
        "    fn fmt(&self, f: &mut Formatter<'_>) -> Result {\n"
        "        write!(f, \"{}\", self.0)\n"
        "    }\n"
        "}\n"
        "impl From<&str> for Version {\n"
        "    fn from(s: &str) -> Self {\n"
        "        Version(s.to_string())\n"
        "    }\n"
        "}\n"
        "pub fn parse_version(s: &str) -> Version {\n"
        "    Version(s.to_string())\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "error.rs").write_text(
        "use std::fmt::{Debug, Display, Formatter, Result};\n"
        "pub struct Error(pub String);\n"
        "impl Debug for Error {\n"
        "    fn fmt(&self, f: &mut Formatter<'_>) -> Result {\n"
        "        Display::fmt(self, f)\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "consumer.rs").write_text(
        "pub fn build(s: &str) -> Version {\n"
        "    Version::from(s)\n"
        "}\n"
        "pub fn load(s: &str) -> Version {\n"
        "    parse_version(s)\n"
        "}\n",
        encoding="utf-8",
    )

    # Homonymous trait call Display::fmt(self, f) inside impl Debug for Error -> INCOMPLETE.
    fmt_res = discover_related_callers(
        tmp_path,
        "fix fmt formatting",
        ["src/display.rs"],
        ["src/error.rs"],
    )
    assert fmt_res.status == "INCOMPLETE"
    assert [e.path for e in fmt_res.excerpts] == ["src/error.rs"]
    assert "ambiguous trait method receiver" in fmt_res.ambiguity
    assert "ambiguous trait method receiver" in fmt_res.evidence[0].ambiguity
    assert fmt_res.excerpts[0].incomplete is True

    # Provably matching target type Version::from(s) -> COMPLETE_WITHIN_SCOPE.
    from_res = discover_related_callers(
        tmp_path,
        "fix from conversion",
        ["src/display.rs"],
        ["src/consumer.rs"],
    )
    assert from_res.status == "COMPLETE_WITHIN_SCOPE"
    assert "ambiguous trait method receiver" not in from_res.ambiguity

    # Non-homonymous function parse_version(s) -> COMPLETE_WITHIN_SCOPE.
    fn_res = discover_related_callers(
        tmp_path,
        "fix parse_version",
        ["src/display.rs"],
        ["src/consumer.rs"],
    )
    assert fn_res.status == "COMPLETE_WITHIN_SCOPE"
    assert "ambiguous trait method receiver" not in fn_res.ambiguity


def test_coder_find_local_callers_searches_editable_files_for_same_file_callers(
    tmp_path, monkeypatch
):
    from unittest.mock import Mock
    import git

    from aider.coders import Coder
    from aider.io import InputOutput
    from aider.models import Model

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("webbrowser.open", Mock())
    repo = git.Repo.init(tmp_path)
    with repo.config_writer() as config:
        config.set_value("user", "name", "Test")
        config.set_value("user", "email", "test@example.invalid")

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "yajl_parser.c").write_text(
        "long long yajl_parse_integer(const unsigned char *number, unsigned int length) {\n"
        "    return (long long)length;\n"
        "}\n"
        "int yajl_do_parse(const unsigned char *buf, unsigned int len) {\n"
        "    long long i = yajl_parse_integer(buf, len);\n"
        "    return (int)i;\n"
        "}\n",
        encoding="utf-8",
    )
    repo.index.add(["src/yajl_parser.c"])
    repo.index.commit("initial")

    coder = Coder.create(
        main_model=Model("gpt-3.5-turbo"),
        edit_format="diff",
        fnames=["src/yajl_parser.c"],
        io=InputOutput(yes=True, pretty=False, fancy_input=False),
        auto_lint=False,
        stream=False,
        map_tokens=0,
        detect_urls=False,
        local_callers=True,
    )
    excerpts, description, _, _ = coder._find_local_callers(
        "Fix integer parsing for non-digit input"
    )
    assert [e.path for e in excerpts] == ["src/yajl_parser.c"]
    assert excerpts[0].symbol == "yajl_parse_integer"
    assert excerpts[0].line == 5
    assert "yajl_parser.c:yajl_parse_integer" in description
