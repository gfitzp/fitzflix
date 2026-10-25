"""Test the names of the Redis keys of the app.

Each module-level *_KEY or *_PREFIX text constant names a Redis key or a
key family. Each must start with "fitzflix:". 2 families must never be
the same pattern. Then 2 features cannot write 1 key.
"""

import ast
import pathlib
import re

APP = pathlib.Path(__file__).resolve().parent.parent / "app"

# The keys that another program owns. rq names its own keys.

FOREIGN = {"SCHEDULER_KEY_PREFIX"}


def _key_constants():
    """Yield (path, name, text) for each module-level key constant of app/."""

    for path in sorted(APP.rglob("*.py")):
        for node in ast.parse(path.read_text()).body:
            if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
                continue
            name = getattr(node.targets[0], "id", "")
            value = node.value
            if (name.endswith("_KEY") or name.endswith("_PREFIX")) and isinstance(
                value, ast.Constant
            ):
                if isinstance(value.value, str) and name not in FOREIGN:
                    yield path.relative_to(APP), name, value.value


def test_every_key_starts_with_the_app_prefix_and_each_family_is_unique(app):
    constants = list(_key_constants())
    assert len(constants) > 80
    for path, name, text in constants:
        assert text.startswith("fitzflix:"), (path, name, text)
    patterns = {}
    for path, name, text in constants:
        pattern = re.sub(r"\{[^}]*\}", "*", text)
        assert pattern not in patterns, (path, name, patterns.get(pattern))
        patterns[pattern] = (path, name)
