from aider.local_callers import related_callers


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
