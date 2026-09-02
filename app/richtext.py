"""Store and render the inline-markup subset of Letterboxd in review text.

Fitzflix stores the review text as it is. The rows without a guid go
back to Letterboxd through the CSV export. Thus, the import must never
rewrite the text of the author. The markup becomes formatting only at
display time. review_html escapes all characters. Then it enables only
the allowed tags. The tags have no attributes. Thus, Fitzflix never
parses the content of a tag. This makes the allow-list safe against
injection.
"""

import re

from markupsafe import Markup, escape

# This is the inline subset without attributes that Letterboxd allows in
# reviews. <a> is absent by design. To handle href, Fitzflix would have
# to parse attributes. Thus, a link in a review becomes visible text
# that does nothing. It does not open an injection surface.

ALLOWED_REVIEW_TAGS = ("blockquote", "b", "i", "em", "strong")


def review_html(text):
    """Render the stored review text for display.

    The allowed tags become real markup. All other characters stay
    escaped. This function closes the tags that are not closed. Thus,
    stray markup cannot apply its style to the rest of the page. Each
    newline becomes <br>. Without this, HTML collapses the paragraph
    breaks.
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
                # A closing tag with no opening tag stays visible text
                pieces.append(match.group(0))
    pieces.append(escaped[pos:])
    pieces += [f"</{name}>" for name in reversed(open_stack)]
    return Markup("".join(pieces).replace("\n", "<br>"))


def strip_disallowed_tags(html_text):
    """Reduce the feed HTML to the subset that Fitzflix can store.

    The allowed tags stay, in their bare form without attributes. This
    is the form that an author types on Letterboxd. Thus, it is also the
    form that the CSV export holds. This function removes all other
    tags. It keeps their inner text."""

    def keep_or_drop(match):
        closing, name = match.group(1), match.group(2).lower()
        if name in ALLOWED_REVIEW_TAGS:
            return f"<{closing}{name}>"
        return ""

    return re.sub(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)\b[^>]*>", keep_or_drop, html_text)
