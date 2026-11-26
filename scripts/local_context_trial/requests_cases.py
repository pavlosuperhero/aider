"""Held-out multi-file tasks seeded in the source distribution of Requests 2.34.2."""

from dataclasses import replace
from importlib.metadata import distribution

import run


def _json_check(document: str, position: int) -> str:
    return f"""
import pickle
from requests import Response
from requests.exceptions import JSONDecodeError

document = {document!r}
position = {position}
for encoding in (None, "utf-8"):
    r = Response()
    r.encoding = encoding
    r._content = document.encode("utf-8")
    try:
        r.json()
    except JSONDecodeError as exc:
        assert exc.msg and (exc.doc, exc.pos) == (document, position)
        restored = pickle.loads(pickle.dumps(exc))
        assert type(restored) is type(exc)
        assert (restored.msg, restored.doc, restored.pos) == (exc.msg, document, position)
    else:
        raise AssertionError("Invalid JSON accepted")
"""


def cases():
    package = distribution("requests")
    if package.version != "2.34.2":
        raise ValueError("The Requests suite requires requests==2.34.2")
    files = {
        str(path): package.locate_file(path).read_text()
        for path in package.files
        if path.parts[0] == "requests" and path.suffix == ".py"
    }
    copy_check = """
from requests import Request
p = Request("GET", "https://example.invalid", headers={"X-Name": "old"}, cookies={"a": "1"}).prepare()
q = p.copy()
q.headers["x-name"] = "new"
q._cookies.set("a", "2")
assert p.headers["x-name"] == "old"
assert q.headers["X-Name"] == "new"
assert p._cookies.get("a") == "1"
"""
    copy_smoke = (
        "from requests import Request\n"
        "assert Request('GET', 'http://example.invalid').prepare().method == 'GET'\n"
    )
    definitions = [
        (
            "requests_copy",
            "Fix copy so modifying headers or cookies on a copied prepared request preserves the original.",
            "A copied request must have independent case-insensitive headers and a separate cookie jar.",
            copy_smoke,
            True,
            copy_check,
            [
                (
                    "requests/structures.py",
                    "return CaseInsensitiveDict(self._store.values())",
                    "return self",
                ),
                ("requests/cookies.py", "return new_cj", "return self"),
            ],
        ),
        (
            "requests_json",
            "Fix json and __reduce__ so malformed responses preserve Requests' public exception contract.",
            "The public JSONDecodeError must preserve its message, document, and position through pickling.",
            _json_check('{"value":}', 9),
            False,
            _json_check('{"x":}', 5),
            [
                (
                    "requests/models.py",
                    "raise RequestsJSONDecodeError(e.msg, e.doc, e.pos)",
                    "raise e",
                ),
                (
                    "requests/exceptions.py",
                    "return CompatJSONDecodeError.__reduce__(self)",
                    "return super().__reduce__()",
                ),
            ],
        ),
    ]
    result = []
    for name, task, explicit, smoke, smoke_passes_on_base, hidden, mutations in definitions:
        seeded = files.copy()
        seeded["smoke.py"] = smoke
        for path, before, after in mutations:
            if before not in seeded[path]:
                raise ValueError(f"Fixture source mismatch: {path}")
            seeded[path] = seeded[path].replace(before, after)
        first, second = (path for path, _, _ in mutations)
        case = run.py_case(name, seeded, task, hidden, explicit, editable_file=first)
        result.append(
            replace(case, additional_editable=(second,), smoke_passes_on_base=smoke_passes_on_base)
        )
    return result
