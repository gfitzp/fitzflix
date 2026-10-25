"""Test that each render_template keyword of the views is read by its template (#359).

An unread keyword is dead work. movie() computed might_interest, with 2
Redis and profile reads, for a template that never read it. The test
reads each template together with the templates that it extends,
includes or imports. Jinja gives an included template the context of
its parent. Thus, a keyword that an included template reads counts.
"""

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "app" / "templates"

REFERENCE = re.compile(r"""{%-?\s*(?:include|extends|import|from)\s+["']([^"']+)["']""")

# A keyword that a template reads by a way that the search cannot see,
# for example through an include whose name is a variable. Each entry is
# (template, keyword) with the reason in a comment.

ALLOWLIST = set()


def _closure(name, seen=None):
    """Return the template and each template that it references, recursively."""

    seen = set() if seen is None else seen
    if name in seen:
        return seen
    seen.add(name)
    path = TEMPLATES / name
    if path.exists():
        for match in REFERENCE.finditer(path.read_text()):
            _closure(match.group(1), seen)
    return seen


def test_every_render_template_keyword_is_read_by_its_template():
    unread = []
    checked = 0
    for path in sorted((ROOT / "app" / "main").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "render_template"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                continue
            template = node.args[0].value
            text = "".join(
                (TEMPLATES / name).read_text()
                for name in _closure(template)
                if (TEMPLATES / name).exists()
            )
            for keyword in node.keywords:
                if keyword.arg is None or (template, keyword.arg) in ALLOWLIST:
                    continue
                checked += 1
                if not re.search(rf"\b{re.escape(keyword.arg)}\b", text):
                    unread.append(
                        f"{path.relative_to(ROOT)}:{node.lineno} "
                        f"{template} {keyword.arg}"
                    )
    assert checked > 100
    assert not unread, unread
