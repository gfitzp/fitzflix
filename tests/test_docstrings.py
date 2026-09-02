"""Make sure that every function, class, and method in app/ has a docstring.

This gate keeps the docstring coverage complete. The policy: module-level
and class-level definitions must have docstrings. Dunder methods are
exempt. Functions nested inside other functions (closures) are exempt.
This file runs as a test. The pre-commit hook also runs this file
directly.
"""

import ast
import os

APP_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"
)
SKIP_DIRS = {"__pycache__", "static", "templates"}


def missing_docstrings(root=APP_ROOT):
    """Return (path, line, name) for each definition that has no docstring.

    Only the definitions that the policy covers are included."""

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
