"""Held-out multi-file tasks seeded in the source distribution of Requests 2.34.2."""

from dataclasses import replace
from importlib.metadata import distribution

import run


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
    json_check = """
import pickle
from requests import Response
from requests.exceptions import JSONDecodeError
for encoding in (None, "utf-8"):
    r = Response()
    r.encoding = encoding
    r._content = b'{"x":}'
    try:
        r.json()
    except JSONDecodeError as exc:
        restored = pickle.loads(pickle.dumps(exc))
        assert (restored.msg, restored.doc, restored.pos) == (exc.msg, '{"x":}', 5)
    else:
        raise AssertionError("Invalid JSON accepted")
"""
    definitions = [
        (
            "requests_copy",
            "Fix copy so modifying headers or cookies on a copied prepared request preserves the original.",
            "A copied request must have independent case-insensitive headers and a separate cookie jar.",
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
            json_check,
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
    for name, task, explicit, hidden, mutations in definitions:
        seeded = files.copy()
        seeded["smoke.py"] = (
            "from requests import Request\n"
            "assert Request('GET', 'http://example.invalid').prepare().method == 'GET'\n"
        )
        for path, before, after in mutations:
            if before not in seeded[path]:
                raise ValueError(f"Fixture source mismatch: {path}")
            seeded[path] = seeded[path].replace(before, after)
        first, second = (path for path, _, _ in mutations)
        case = run.py_case(name, seeded, task, hidden, explicit, editable_file=first)
        result.append(replace(case, additional_editable=(second,)))
    return result
