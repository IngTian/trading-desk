"""Every refusal reads right to an agent calling through MCP: values as JSON, no Python syntax.
The scan covers every string and f-string in these modules, docstrings excepted."""
import ast
import re
from pathlib import Path

import desk

MODULES = ("api.py", "model.py", "rules.py", "strategy.py", "server.py")
PYTHON = re.compile(r"datetime\(|tzinfo|='|\{'|\b(None|True|False)\b")


def texts():
    for name in MODULES:
        tree = ast.parse((Path(desk.__file__).parent / name).read_text())
        docs = {id(n.body[0].value) for n in ast.walk(tree)
                if isinstance(n, ast.Module | ast.ClassDef | ast.FunctionDef)
                and n.body and isinstance(n.body[0], ast.Expr)
                and isinstance(n.body[0].value, ast.Constant)}
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr | ast.Constant) and id(node) not in docs:
                yield name, node


def test_no_message_shows_a_value_with_repr():
    assert [(n, v.lineno) for n, s in texts() if isinstance(s, ast.JoinedStr)
            for v in s.values if isinstance(v, ast.FormattedValue)
            and v.conversion == ord("r")] == []
    calls = [(name, node.lineno) for name in MODULES
             for node in ast.walk(ast.parse((Path(desk.__file__).parent / name).read_text()))
             if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "repr"]
    assert calls == []


def test_no_message_reads_as_python():
    assert [(n, s.lineno, s.value) for n, s in texts() if isinstance(s, ast.Constant)
            and isinstance(s.value, str) and PYTHON.search(s.value)] == []
