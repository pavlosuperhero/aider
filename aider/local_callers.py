"""Bounded local lookup for explicit Python function names and direct imports."""

import ast
import re
from pathlib import Path


def read_python(root, name):
    path = root / name
    try:
        if (
            path.suffix != ".py"
            or path.resolve() != path
            or not path.is_relative_to(root)
            or not path.is_file()
        ):
            return ""
        with path.open("rb") as stream:
            data = stream.read(131073)
        return data.decode() if len(data) <= 131072 else ""
    except (OSError, UnicodeError):
        return ""


def related_callers(root, message, editable, candidates):
    root = root.resolve()
    words = set(re.findall(r"\b[A-Za-z_]\w*\b", message))
    targets = set()
    for name in editable:
        try:
            definitions = ast.parse(read_python(root, name)).body
        except (SyntaxError, ValueError):
            continue
        module = ".".join(Path(name).with_suffix("").parts)
        for node in definitions:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in words:
                targets.add((module, node.name))
    if not targets:
        return ""

    excerpts = []
    for name in sorted(candidates):
        source = read_python(root, name)
        if not any(symbol in source for _, symbol in targets):
            continue
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            continue
        aliases = {
            alias.asname or alias.name
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and not node.level
            for alias in node.names
            if (node.module, alias.name) in targets
        }
        lines = source.splitlines()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in aliases
            ):
                start = max(0, node.lineno - 3)
                excerpt = "\n".join(lines[start : node.lineno + 3])
                excerpts.append(f"{name}:{start + 1}\n{excerpt[:700]}")
                break
        if len(excerpts) == 3:
            break
    return "\n\n".join(excerpts)
