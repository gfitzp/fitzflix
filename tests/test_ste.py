"""The Simplified Technical English gate for the prose in this repository.

The README, the page text, and every comment and docstring follow
ASD-STE100 (refer to the README section "Language notes"). This test
finds the mechanical violations that a reviewer catches most easily:
contractions, em dashes and semicolons that join clauses, some words
that STE does not permit, and sentences of more than 25 words. It does
not check the STE dictionary.

The checks apply to Python comments and docstrings (app/, tests/,
scripts/, and the root scripts). They apply to the text of each flash
call and each help argument (#393). They apply to the text of each
click.echo call, and to the literals that a function returns when its
result reaches a flash call (#528). They apply to the string arguments
of each notify_admin_text and task_send_email call (#602). They apply to the Jinja, HTML, CSS,
and JavaScript comments of the templates and of the static files. They
apply to the page text of the templates, to the text that their scripts
write into the page, and to the email templates. A preformatted block
is not page text. They apply to the prose of each Markdown file. Text
in double quotes or backticks is quoted material, so the test ignores
it. The
exempt marker (the EXEMPT constant) skips the rest of its block, from
the line that holds it. Use it only for text that must stay as it is,
for example a licence notice. A semicolon inside code-like text (a
stylesheet rule, an assignment, a call) is not a clause join. A
double hyphen next to a command word is an argument separator, not a
dash. Runs as a test, and pre-commit invokes this file directly.
"""

import ast
import html
import io
import os
import re
import tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY_DIRS = ("app", "tests", "scripts")
ROOT_SCRIPTS = (
    "config.py",
    "fitzflix.py",
    "scheduler.py",
    "supervisor.py",
    "gunicorn.conf.py",
    ".claude/mint_session_cookie.py",
)
TEMPLATE_DIRS = ("app/templates", "app/static")
PAGE_TEXT_DIR = "app/templates"
SKIP_DIRS = {"__pycache__", "node_modules"}
# The Markdown walk skips these directories. They hold no repository
# prose: the virtual environment, the caches, and the local notes.
MARKDOWN_SKIP_DIRS = SKIP_DIRS | {
    ".git",
    ".claude",
    ".pytest_cache",
    "venv",
    ".venv",
    "migrations",
}
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


def _python_prose(path, flashed=frozenset()):
    """Yield (line, text) for each comment, docstring and message of a Python file.

    flashed holds the names of the functions whose result reaches a
    flash call in some module. Their returned literals are messages."""
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
    yield from _message_prose(tree)
    yield from _returned_messages(tree, flashed)


def _literal_text(node):
    """Return the text of a string literal, an f-string, or a sum of them, or None.

    A replacement field of an f-string becomes the word X."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value if isinstance(part, ast.Constant) else "X"
            for part in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal_text(node.left), _literal_text(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _call_name(node):
    """Return the name of the function of a call node, or None."""
    return getattr(node.func, "id", None) or getattr(node.func, "attr", None)


def _is_echo(node):
    """Return True for a call of click.echo or click.secho (#528)."""
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in ("echo", "secho")
        and getattr(func.value, "id", None) == "click"
    )


# The calls that send an inline mail. Each string argument is mail prose
# (#602): the subject, the plain body and the HTML body.

MAIL_CALLS = ("notify_admin_text", "task_send_email")


def _mail_text(text):
    """Return the prose of an inline mail body: no tags, and no entities."""
    return html.unescape(re.sub(r"<[^>]+>", " ", text))


def _mail_literal(node):
    """Return the literal text of a mail argument, or None if it has none.

    A body is often a literal plus a joined list. Each operand of a sum
    that is not a literal becomes the word X."""
    text = _literal_text(node)
    if text is not None:
        return text
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _mail_literal(node.left), _mail_literal(node.right)
        if left is None and right is None:
            return None
        return (left if left is not None else " X ") + (
            right if right is not None else " X "
        )
    return None


def _message_prose(tree):
    """Yield (line, text) for each flash, echo, help and inline mail text."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        values = [node.args[0]] if name == "flash" and node.args else []
        if _is_echo(node) and node.args:
            values.append(node.args[0])
        values += [kw.value for kw in node.keywords if kw.arg == "help"]
        for value in values:
            text = _literal_text(value)
            if text:
                yield value.lineno, text
        if name in MAIL_CALLS:
            for value in list(node.args) + [kw.value for kw in node.keywords]:
                text = _mail_literal(value)
                if text:
                    yield value.lineno, _mail_text(text)


# A call with one of these names is not a message helper. Its result can
# reach a flash call, but its returns are not messages.
NOT_MESSAGE_HELPERS = {"Markup", "format", "str", "join", "get", "escape", "strip"}


def _names_bound_by(target):
    """Yield each plain name that an assignment target binds."""
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from _names_bound_by(element)


def flashed_functions(tree):
    """Return the names of the functions whose result reaches a flash call (#528).

    A helper can return the text, and its caller flashes it: flash(f(x)),
    or error = f(x) and then flash(error), or a, error = f(x)."""
    names = set()
    scopes = [tree] + [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for scope in scopes:
        bound = {}
        for node in ast.walk(scope):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                for target in node.targets:
                    for name in _names_bound_by(target):
                        bound.setdefault(name, set()).add(_call_name(node.value))
        for node in ast.walk(scope):
            if not (isinstance(node, ast.Call) and _call_name(node) == "flash"):
                continue
            if not node.args:
                continue
            argument = node.args[0]
            if isinstance(argument, ast.Call):
                names.add(_call_name(argument))
            elif isinstance(argument, ast.Name) and argument.id in bound:
                names |= bound[argument.id]
    return {name for name in names if name} - NOT_MESSAGE_HELPERS


def _returned_messages(tree, flashed):
    """Yield (line, text) for each literal that a flashed function returns.

    A returned tuple gives each of its literals."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in flashed:
            continue
        for returned in ast.walk(node):
            if not isinstance(returned, ast.Return) or returned.value is None:
                continue
            value = returned.value
            elements = value.elts if isinstance(value, ast.Tuple) else [value]
            for element in elements:
                text = _literal_text(element)
                if text:
                    yield element.lineno, text


# A block tag of HTML ends a run of page text. The other tags do not.
BLOCK_TAG = re.compile(
    r"</?(?:p|div|li|ul|ol|h[1-6]|br|hr|tr|td|th|table|thead|tbody|section|"
    r"header|footer|nav|main|form|button|label|option|select|dl|dt|dd|"
    r"blockquote|figure|figcaption|title|textarea|summary|details|span)\b[^>]*>",
    re.I,
)
# The entities that stand for punctuation that the rules check.
PUNCTUATION_ENTITIES = {
    "&mdash;": "—",
    "&#8212;": "—",
    "&rsquo;": "’",
    "&#39;": "'",
    "&apos;": "'",
    "&ldquo;": "“",
    "&rdquo;": "”",
    "&nbsp;": " ",
}


def _page_text(path):
    """Yield (line, text) for each run of page text in a template (#393).

    The comments, the scripts, the styles, the preformatted blocks, and
    the tags go first. A Jinja expression becomes the word Xxx. A block
    tag, a span, or a Jinja statement ends a run. Thus, a row of badges
    is not 1 long sentence. In a plain text template, a blank line or a
    Jinja statement ends a run."""
    source = open(path, encoding="utf-8").read()

    def blank(match):
        return re.sub(r"[^\n]", " ", match.group(0))

    text = re.sub(
        r"\{#.*?#\}|<!--.*?-->|<script\b.*?</script>|<style\b.*?</style>"
        r"|<pre\b.*?</pre>",
        blank,
        source,
        flags=re.S | re.I,
    )
    text = re.sub(r"\{\{.*?\}\}", lambda m: "Xxx" + blank(m)[3:], text, flags=re.S)
    text = re.sub(r"\{%.*?%\}", lambda m: "\x00" + blank(m)[1:], text, flags=re.S)
    if path.endswith(".txt"):
        text = re.sub(r"\n([ \t]*)\n", "\n\\1\x00\n", text)
    else:
        text = BLOCK_TAG.sub(lambda m: "\x00" + blank(m)[1:], text)
        text = re.sub(r"<[^>]*>", blank, text, flags=re.S)
        for entity, character in PUNCTUATION_ENTITIES.items():
            text = text.replace(entity, character)
    position = 0
    for run in text.split("\x00"):
        if run.strip():
            offset = len(run) - len(run.lstrip())
            line = text.count("\n", 0, position + offset) + 1
            yield line, run
        position += len(run) + 1


# A script line that puts a literal into the page as text.
SCRIPT_TEXT = re.compile(
    r"\.(?:textContent|innerText)\s*=\s*(?P<quote>[\"'])(?P<text>.*?)(?<!\\)(?P=quote)"
)


def _script_text(path):
    """Yield (line, text) for each literal that a script writes as page text."""
    source = open(path, encoding="utf-8").read()
    for match in SCRIPT_TEXT.finditer(source):
        yield source.count("\n", 0, match.start()) + 1, match.group("text")


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
    """Yield (line, text) for each prose paragraph of a Markdown file."""
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
    python_files = [path for path in python_files if os.path.exists(path)]
    flashed = set()
    for path in python_files:
        flashed |= flashed_functions(ast.parse(open(path, encoding="utf-8").read()))
    for path in python_files:
        for line, text in _python_prose(path, flashed):
            found.extend(_violations(text, os.path.relpath(path, root), line))
    for path in _files(TEMPLATE_DIRS, (".html", ".js", ".css"), root):
        for line, text in _template_prose(path):
            found.extend(_violations(text, os.path.relpath(path, root), line))
    for path in _files((PAGE_TEXT_DIR,), (".html", ".txt"), root):
        for line, text in list(_page_text(path)) + list(_script_text(path)):
            found.extend(_violations(text, os.path.relpath(path, root), line))
    for path in _markdown_files(root):
        for line, text in _readme_prose(path):
            found.extend(_violations(text, os.path.relpath(path, root), line))
    return found


def _markdown_files(root=ROOT):
    """Return each Markdown file of the repository, the root README first."""
    paths = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in MARKDOWN_SKIP_DIRS)
        paths.extend(
            os.path.join(dirpath, name)
            for name in sorted(filenames)
            if name.endswith(".md")
        )
    readme = os.path.join(root, "README.md")
    return [readme] + [path for path in paths if path != readme]


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


def test_the_gate_reads_page_text_messages_emails_and_markdown(tmp_path):
    """The page text, flash and help text, emails and Markdown are prose (#393)."""
    templates = tmp_path / "app" / "templates"
    (templates / "email").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "README.md").write_text("Fine.\n")
    (tmp_path / "tests" / "README.md").write_text("It isn't checked yet.\n")
    (tmp_path / "app" / "m.py").write_text(
        'flash(f"{name} won\'t import.")\n'
        'click.option("--x", help="A dash — here.")\n'
        'log("It isn\'t a message.")\n'
    )
    (templates / "page.html").write_text(
        "<p>This film won&rsquo;t show.</p>\n"
        "<pre>It isn't prose.</pre>\n"
        '<script>el.textContent = "Couldn\'t reach it.";</script>\n'
        '<span title="It\'s a title">{{ "Didn\'t like it" }}</span>\n'
        "<span>Database {{ a }} ms</span><span>Redis {{ b }} ms</span>\n"
    )
    (templates / "email" / "note.txt").write_text("Hello,\n\nIt's been moved.\n")
    found = sorted(
        (path, line, rule) for path, line, rule, _ in ste_violations(str(tmp_path))
    )
    page = os.path.join("app", "templates", "page.html")
    assert found == sorted(
        [
            (os.path.join("app", "m.py"), 1, "contraction"),
            (os.path.join("app", "m.py"), 2, "em dash"),
            (page, 1, "contraction"),
            (page, 3, "contraction"),
            (os.path.join("app", "templates", "email", "note.txt"), 3, "contraction"),
            (os.path.join("tests", "README.md"), 1, "contraction"),
        ]
    )


def test_the_gate_reads_echo_text_and_flashed_helper_returns(tmp_path):
    """A helper that returns the flashed text is message prose (#528).

    The DVR admin helpers returned an em dash that the gate did not see."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "README.md").write_text("Fine.\n")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "helpers.py").write_text(
        "def resolve(text):\n"
        "    if not text:\n"
        '        return None, "Enter a title."\n'
        "    return None, f'{text} is ambiguous \u2014 try again.'\n"
        "\n"
        "\n"
        "def unrelated():\n"
        '    return "It isn\'t a message."\n'
    )
    (tmp_path / "app" / "routes.py").write_text(
        "from app.helpers import resolve\n"
        "\n"
        "\n"
        "def view(text):\n"
        "    movie, error = resolve(text)\n"
        "    flash(error)\n"
        '    click.echo("It doesn\'t echo well.")\n'
    )
    found = sorted(
        (path, line, rule) for path, line, rule, _ in ste_violations(str(tmp_path))
    )
    assert found == [
        (os.path.join("app", "helpers.py"), 4, "em dash"),
        (os.path.join("app", "routes.py"), 7, "contraction"),
    ]


def test_the_gate_reads_the_inline_mail_bodies(tmp_path):
    """The bodies of notify_admin_text and task_send_email are mail prose (#602).

    The missing-archive-keys mail said "don't" and used em dashes. The
    gate scanned the email templates only."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "README.md").write_text("Fine.\n")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "mails.py").write_text(
        "def report(keys):\n"
        "    notify_admin_text(\n"
        '        "Keys missing!",\n'
        '        "These keys don\'t exist:\\n\\n" + "\\n".join(keys),\n'
        '        "<p>These keys exist &mdash; check them.</p><ul>"\n'
        '        + "".join(keys)\n'
        '        + "</ul>",\n'
        "    )\n"
        "    task_send_email(\n"
        '        "Report",\n'
        '        sender="x@example.test",\n'
        "        recipients=[],\n"
        '        text_body="It isn\'t sent.",\n'
        "        html_body=None,\n"
        "    )\n"
        '    log("It isn\'t a mail.")\n'
    )
    found = sorted(
        (path, line, rule) for path, line, rule, _ in ste_violations(str(tmp_path))
    )
    mails = os.path.join("app", "mails.py")
    assert found == [
        (mails, 4, "contraction"),
        (mails, 5, "em dash"),
        (mails, 13, "contraction"),
    ]
