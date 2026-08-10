"""The docstring gate: every function, class, and method in app/ carries a
docstring, so the coverage won can't erode.

Policy: module-level and class-level definitions need docstrings; dunder
methods and functions nested inside other functions (closures) are exempt.
Runs as a test, and pre-commit invokes this file directly.
"""

import ast
import os

APP_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"
)
SKIP_DIRS = {"__pycache__", "static", "templates"}


def missing_docstrings(root=APP_ROOT):
    """(path, line, name) for every definition the policy requires a
    docstring on that doesn't have one."""

    missing = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            path = os.path.join(dirpath, filename)
            tree = ast.parse(open(path).read())

            def walk(node, parents=()):
                for child in ast.iter_child_nodes(node):
                    if isinstance(
                        child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                    ):
                        nested = any(
                            isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef))
                            for p in parents
                        )
                        dunder = child.name.startswith("__") and child.name.endswith(
                            "__"
                        )
                        if (
                            not nested
                            and not dunder
                            and ast.get_docstring(child) is None
                        ):
                            missing.append(
                                (os.path.relpath(path), child.lineno, child.name)
                            )
                        walk(child, parents + (child,))

            walk(tree)
    return missing


def test_every_definition_has_a_docstring():
    missing = missing_docstrings()
    assert not missing, "undocumented definitions:\n" + "\n".join(
        f"  {path}:{line} {name}" for path, line, name in missing
    )


if __name__ == "__main__":
    import sys

    undocumented = missing_docstrings()
    for path, line, name in undocumented:
        print(f"{path}:{line}: missing docstring on {name}")
    sys.exit(1 if undocumented else 0)
