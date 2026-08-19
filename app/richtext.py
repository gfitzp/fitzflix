"""Review text carries Letterboxd's inline-markup subset, stored
verbatim (guid-less rows round-trip back to Letterboxd via CSV export,
so import must never rewrite authored text). Display is where markup
becomes formatting: review_html escapes everything, then re-enables
exactly the allowed tags — attribute-less, so nothing inside a tag is
ever parsed, which is what makes the allow-list safe against injection.
"""

import re

from markupsafe import Markup, escape

# The attribute-less inline subset Letterboxd itself allows in reviews.
# <a> is deliberately absent: href handling would mean parsing
# attributes, and a link in a review degrades to visible-but-inert
# text rather than opening an injection surface.

ALLOWED_REVIEW_TAGS = ("blockquote", "b", "i", "em", "strong")


def review_html(text):
    """Render stored review text for display: allowed tags become real
    markup, every other character stays escaped, unclosed tags are
    closed so stray markup can't bleed styling into the page, and
    newlines become <br> (paragraph breaks otherwise collapse in HTML).
    """

    escaped = str(escape(text or ""))
    pieces = []
    open_stack = []
    pos = 0
    for match in re.finditer(r"&lt;(/?)([a-zA-Z]+)\s*/?&gt;", escaped, re.IGNORECASE):
        closing, name = match.group(1), match.group(2).lower()
        if name == "br" and not closing:
            pieces += [escaped[pos : match.start()], "<br>"]
            pos = match.end()
        elif name in ALLOWED_REVIEW_TAGS:
            pieces.append(escaped[pos : match.start()])
            pos = match.end()
            if not closing:
                open_stack.append(name)
                pieces.append(f"<{name}>")
            elif name in open_stack:
                open_stack.remove(name)
                pieces.append(f"</{name}>")
            else:
                # A closer with no matching opener stays visible text
                pieces.append(match.group(0))
    pieces.append(escaped[pos:])
    pieces += [f"</{name}>" for name in reversed(open_stack)]
    return Markup("".join(pieces).replace("\n", "<br>"))


def strip_disallowed_tags(html_text):
    """Reduce feed HTML to the storable subset: allowed tags survive
    normalized to their bare attribute-less form (matching what an
    author types on Letterboxd, and so what its CSV export holds);
    every other tag is dropped, keeping its inner text."""

    def keep_or_drop(match):
        closing, name = match.group(1), match.group(2).lower()
        if name in ALLOWED_REVIEW_TAGS:
            return f"<{closing}{name}>"
        return ""

    return re.sub(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)\b[^>]*>", keep_or_drop, html_text)
