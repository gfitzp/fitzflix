"""Review markup handling: the display filter renders Letterboxd's
inline subset and nothing else, and the feed's tag pass stores that
same subset — so CSV-imported and feed-synced reviews agree."""


def test_review_html_renders_allowed_tags_and_newlines(app):
    from app.richtext import review_html

    result = str(
        review_html("I watched <i>The Tall T</i> a month ago.\n\nStill <b>great</b>.")
    )
    assert result == (
        "I watched <i>The Tall T</i> a month ago.<br><br>Still <b>great</b>."
    )


def test_review_html_escapes_everything_outside_the_subset(app):
    from app.richtext import review_html

    result = str(review_html('<script>alert("x")</script> a <3 movie & more'))
    assert "<script" not in result
    assert "&lt;script&gt;" in result
    assert "a &lt;3 movie &amp; more" in result

    # Attributes disqualify a tag — nothing inside a tag is ever parsed
    assert "<i>" not in str(review_html('<i onmouseover="alert(1)">sneaky</i>'))


def test_review_html_balances_stray_markup(app):
    from app.richtext import review_html

    # An unclosed opener can't bleed styling past the review
    assert str(review_html("so <b>good")) == "so <b>good</b>"

    # A closer with no opener stays visible text
    assert str(review_html("weird</b> text")) == "weird&lt;/b&gt; text"


def test_strip_disallowed_tags_normalizes_to_the_subset(app):
    from app.richtext import strip_disallowed_tags

    assert (
        strip_disallowed_tags('<I>italic</I> <span class="x">plain</span> <b>bold</b>')
        == "<i>italic</i> plain <b>bold</b>"
    )
