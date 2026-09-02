"""The Simplified Technical English gate for the prose in this repository.

The README, the page text, and every comment and docstring follow
ASD-STE100 (refer to the README section "Language notes"). This test
finds the mechanical violations that a reviewer catches most easily:
contractions, em dashes and semicolons that join clauses, some words
that STE does not permit, and sentences of more than 25 words. It does
not check the STE dictionary.

The checks apply to Python comments and docstrings (app/, tests/, and
the root scripts). They apply to Jinja, HTML, CSS, and JavaScript
comments under app/templates and app/static. They apply to the README
prose. Text in double
quotes or backticks is quoted material, so the test ignores it. The
exempt marker (the EXEMPT constant) skips the rest of its block, from
the line that holds it. Use it only for text that must stay as it is,
for example a licence notice. A semicolon inside code-like text (a
stylesheet rule, an assignment, a call) is not a clause join. A
double hyphen next to a command word is an argument separator, not a
dash. Runs as a test, and pre-commit invokes this file directly.
"""

import ast
import io
import os
import re
import tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY_DIRS = ("app", "tests")
ROOT_SCRIPTS = ("config.py", "fitzflix.py", "scheduler.py", "supervisor.py")
TEMPLATE_DIRS = ("app/templates", "app/static")
SKIP_DIRS = {"__pycache__", "node_modules"}
MAX_WORDS = 25

CONTRACTION = re.compile(
    r"\b\w+n['’]t\b|\b\w+['’](re|ve|ll)\b"
    r"|\b(it|that|there|here|what|who|let)['’]s\b",
    re.I,
)
BANNED = [
    (re.compile(r"\be\.g\.", re.I), '"e.g." (write "for example")'),
    (re.compile(r"\bi\.e\.", re.I), '"i.e." (write "that is")'),
    (re.compile(r"\bvia\b", re.I), '"via" (write "through")'),
    (re.compile(r"\bwhether\b", re.I), '"whether" (write "if")'),
    (re.compile(r"\bso that\b", re.I), '"so that" (write "Thus," or "so")'),
    (re.compile(r"\bensure\b", re.I), '"ensure" (write "make sure")'),
    (re.compile(r"\butili[sz]e\b", re.I), '"utilize" (write "use")'),
    (re.compile(r"\bprior to\b", re.I), '"prior to" (write "before")'),
    (re.compile(r"\bin order to\b", re.I), '"in order to" (write "to")'),
    (re.compile(r"\bas-is\b", re.I), '"as-is" (write "as it is")'),
    (re.compile(r"\btwice\b", re.I), '"twice" (write "two times")'),
    (re.compile(r"\ba few\b", re.I), '"a few" (write "some")'),
]
QUOTED = re.compile(r'"[^"]{0,400}"|`[^`]{0,200}`|“[^”]{0,400}”')
EM_DASH = re.compile(r"—|\s--\s")
CLAUSE_SEMICOLON = re.compile(r";\s+[a-z]")
# Text that holds one of these before a semicolon is code, not prose
CODE_HINT = re.compile(r"[=:(){}\[\]]|\b[A-Z]{3,}\b")
# A double hyphen in the same sentence as one of these is an argument
# separator (git log -- path), not a dash
COMMAND_WORDS = re.compile(
    r"\b(git|ffmpeg|ffprobe|mkvmerge|mkvpropedit|mkvextract|pip|python|"
    r"pytest|black|curl|rq|redis-cli|supervisorctl|rsync|aws|ssh)\b"
)
ENTITY = re.compile(r"&#?\w+;")
EXEMPT = "STE: exempt"
CODE_MARKER = re.compile(r"^#\s*(noqa|fmt:|type:|pragma)")
URL = re.compile(r"https?://\S+")


def _unquoted(text):
    """Return the text with quoted spans, URLs, and paths blanked."""
    text = QUOTED.sub(" ", text)
    text = ENTITY.sub(" ", text)
    text = URL.sub(" ", text)
    return re.sub(r"(?<!\S)[\w.~-]*(?:/[\w.~-]+)+", " ", text)


def _sentence_around(text, position):
    """Return the sentence of the text that holds the position."""
    start = max(text.rfind(".", 0, position), text.rfind("\n", 0, position)) + 1
    end = text.find(".", position)
    return text[start:] if end == -1 else text[start : end + 1]


def _checked_part(text):
    """Return the text before the line that holds the exempt marker."""
    if EXEMPT not in text:
        return text
    kept = []
    for line in text.splitlines():
        if EXEMPT in line:
            break
        kept.append(line)
    return "\n".join(kept)


def _violations(text, path, line):
    """Return (path, line, rule, excerpt) for each violation in a prose span."""
    found = []
    plain = _unquoted(_checked_part(text))
    if not re.search(r"[A-Za-z]{3}", plain):
        return found
    for match in CONTRACTION.finditer(plain):
        found.append((path, line, "contraction", match.group(0)))
    for match in EM_DASH.finditer(plain):
        sentence = _sentence_around(plain, match.start())
        if match.group(0).strip() == "--" and COMMAND_WORDS.search(sentence):
            continue
        found.append((path, line, "em dash", match.group(0).strip()))
        break
    for match in CLAUSE_SEMICOLON.finditer(plain):
        sentence_start = (
            max(plain.rfind(".", 0, match.start()), plain.rfind("\n", 0, match.start()))
            + 1
        )
        if CODE_HINT.search(plain[sentence_start : match.start()]):
            continue
        found.append((path, line, "semicolon that joins clauses", match.group(0)))
        break
    for pattern, rule in BANNED:
        match = pattern.search(plain)
        if match:
            found.append((path, line, rule, match.group(0).strip()))
    plain = re.sub(r"(?m)^\s*(?:[-*]|\d+\.)\s+", ". ", plain)
    for sentence in re.split(r"(?<=[.!?:])\s+", " ".join(plain.split())):
        words = sentence.split()
        if len(words) > MAX_WORDS:
            found.append(
                (path, line, f"sentence of {len(words)} words", " ".join(words[:8]))
            )
    return found


def _python_prose(path):
    """Yield (line, text) for each comment block and docstring in a Python file."""
    source = open(path, encoding="utf-8").read()
    block, start = [], None
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            body = tok.string.lstrip("#").strip()
            if CODE_MARKER.match(tok.string) or not body:
                continue
            # A comment after code on its line is its own span. Only
            # consecutive full-line comments make one block.
            trailing = bool(tok.line[: tok.start[1]].strip())
            if trailing:
                if block:
                    yield start, "\n".join(block)
                    block, start = [], None
                yield tok.start[0], body
                continue
            if block and tok.start[0] == start + len(block):
                block.append(body)
            else:
                if block:
                    yield start, "\n".join(block)
                block, start = [body], tok.start[0]
    if block:
        yield start, "\n".join(block)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            doc = ast.get_docstring(node)
            if doc:
                first = node.body[0]
                yield first.lineno, doc


def _template_prose(path):
    """Yield (line, text) for each comment in a template, script, or stylesheet."""
    source = open(path, encoding="utf-8").read()
    pattern = re.compile(
        r"\{#(.*?)#\}|<!--(.*?)-->|/\*(.*?)\*/|(?<![:/\\])//([^\n]*)", re.S | re.M
    )
    for match in pattern.finditer(source):
        text = next(g for g in match.groups() if g is not None)
        yield source.count("\n", 0, match.start()) + 1, text


def _readme_prose(path):
    """Yield (line, text) for each prose paragraph of the README."""
    in_code = False
    for number, line in enumerate(open(path, encoding="utf-8"), 1):
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code or line.startswith(("#", "|", "<img", "    ")):
            continue
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line).strip()
        if text:
            yield number, text


def _files(dirs, suffixes, root=ROOT):
    """Return the repository files under the given directories with a suffix."""
    paths = []
    for base in dirs:
        for dirpath, dirnames, filenames in os.walk(os.path.join(root, base)):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in sorted(filenames):
                if name.endswith(suffixes):
                    paths.append(os.path.join(dirpath, name))
    return paths


def ste_violations(root=ROOT):
    """Return every STE violation in the repository as (path, line, rule, excerpt)."""
    found = []
    python_files = _files(PY_DIRS, (".py",), root) + [
        os.path.join(root, name) for name in ROOT_SCRIPTS
    ]
    for path in python_files:
        if not os.path.exists(path):
            continue
        for line, text in _python_prose(path):
            found.extend(_violations(text, os.path.relpath(path, root), line))
    for path in _files(TEMPLATE_DIRS, (".html", ".js", ".css"), root):
        for line, text in _template_prose(path):
            found.extend(_violations(text, os.path.relpath(path, root), line))
    readme = os.path.join(root, "README.md")
    for line, text in _readme_prose(readme):
        found.extend(_violations(text, "README.md", line))
    return found


def test_prose_follows_simplified_technical_english():
    found = ste_violations()
    assert not found, "STE violations:\n" + "\n".join(
        f"  {path}:{line}: {rule}: {excerpt}" for path, line, rule, excerpt in found
    )


if __name__ == "__main__":
    import sys

    violations = ste_violations()
    for path, line, rule, excerpt in violations:
        print(f"{path}:{line}: {rule}: {excerpt}")
    print(f"{len(violations)} STE violation(s)" if violations else "STE: clean")
    sys.exit(1 if violations else 0)


def test_code_like_semicolons_are_not_clause_joins():
    assert _violations("/* color: red; background: none */", "x", 1) == []
    assert _violations("Run let x = 1; y = 2 in the console.", "x", 1) == []
    rules = [rule for _, _, rule, _ in _violations("It is fine; it works.", "x", 1)]
    assert rules == ["semicolon that joins clauses"]


def test_double_hyphen_next_to_a_command_is_a_separator():
    assert _violations("Run git log -- app to see the history.", "x", 1) == []
    rules = [rule for _, _, rule, _ in _violations("The dial -- the data.", "x", 1)]
    assert rules == ["em dash"]
    rules = [rule for _, _, rule, _ in _violations("The dial \u2014 the data.", "x", 1)]
    assert rules == ["em dash"]


def test_exempt_marker_skips_only_the_rest_of_the_block():
    text = "This isn't checked either.\nNotice (" + EXEMPT + ")\nIt doesn't matter."
    found = _violations(text, "x", 1)
    assert [excerpt for _, _, _, excerpt in found] == ["isn't"]
    assert EXEMPT not in __doc__


def test_trailing_comments_are_their_own_spans(tmp_path):
    words = " ".join(["word"] * 15)
    source = f"x = 1  # {words}\ny = 2  # {words}\n# one\n# two\n"
    path = tmp_path / "m.py"
    path.write_text(source)
    spans = list(_python_prose(str(path)))
    assert [line for line, _ in spans] == [1, 2, 3]
    assert spans[2][1] == "one\ntwo"
    assert _violations(spans[0][1], "x", 1) == []


def test_trailing_script_comments_are_scanned(tmp_path):
    path = tmp_path / "t.html"
    path.write_text(
        "<script>\nvar u = 'https://x.test/a';  // it isn't checked yet\n</script>\n"
    )
    spans = list(_template_prose(str(path)))
    assert [text.strip() for _, text in spans] == ["it isn't checked yet"]


def test_root_parameter_selects_the_tree(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "app" / "m.py").write_text("# it isn't right\n")
    (tmp_path / "README.md").write_text("Fine.\n")
    found = ste_violations(root=str(tmp_path))
    assert [(path, rule) for path, _, rule, _ in found] == [
        (os.path.join("app", "m.py"), "contraction")
    ]
