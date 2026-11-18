"""Build the comparison keys of titles and names, in 2 strengths.

title_key is the loose key. It folds the accents and the case, and it
removes the white space at the 2 ends. That is how the
utf8mb4_general_ci collation of MySQL compares 2 values. It keeps the
white space inside a value, because MySQL compares it. The frame game,
the Criterion schedule, the TMDB credit dedupe and the certification
sync use it.

match_key is the strict key of the Criterion TMDB match. It also
removes the apostrophes and the punctuation. Each run of other
characters becomes 1 space.

The diary has its own key (diary._normalize_title). Its TMDB pick
compares 2 keys for equality. A key with no punctuation would make
"Mother!" equal to "Mother".
"""

import re
import unicodedata

from unidecode import unidecode


def title_key(text):
    """Return the loose key of a title: no accents, no case, no white space at the ends."""

    return unidecode(text or "").casefold().strip()


def match_key(text):
    """Return the strict key of a title or a name.

    This folds accents. It removes case and punctuation. It removes
    apostrophes (straight or curly) fully. Thus, "Muriel’s Wedding"
    matches "Muriel's Wedding", and "P. J. Hogan" matches "P.J.
    Hogan". A letter with no ASCII form (for example, "Æ") is
    removed."""

    text = re.sub(r"[\'’`]", "", text or "")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
