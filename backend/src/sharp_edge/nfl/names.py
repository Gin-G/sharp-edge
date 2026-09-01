"""One name normaliser, shared by both sides of the join.

Worth its own module rather than living next to either caller, because the two
sources have to agree exactly and the failure mode when they don't is silent:
a player whose name normalises differently on the odds side and the projection
side simply never appears on the board, with no error anywhere.

The MLB helper (``_data._norm``) is not close enough to reuse. It leaves
generational suffixes alone, so "Michael Pittman Jr." and "Michael Pittman"
stay different strings — and football has a lot of those: Pittman Jr., Kyle
Pitts Sr., Travis Etienne Jr., James Cook III, Marvin Harrison Jr.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

# Trailing generational suffixes only. Anchored at the end so a surname that
# happens to be "Ivey" or "Vaughn" is untouched.
_SUFFIX = re.compile(r"\s+(?:jr|sr|ii|iii|iv|v)$")


def norm_name(name: Optional[str]) -> str:
    """Fold a player name to a join key.

    FanDuel and nflverse disagree on accents, on apostrophes ("Ja'Marr" vs
    "JaMarr"), on periods ("A.J. Brown"), on hyphens ("Smith-Njigba") and on
    suffixes ("Pittman Jr."). Fold all five and what is left joins cleanly.
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    # Apostrophes close up, everything else opens out. "Ja'Marr" is one word
    # and splitting it gives "ja marr", which never joins against "JaMarr";
    # "Amon-Ra" is two and closing it up gives "amonra", which never joins
    # against "Amon Ra".
    s = s.replace("'", "")
    s = re.sub(r"[^a-z ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = _SUFFIX.sub("", s).strip()
    # Punctuation removal splits initials apart, so "D.J. Moore" becomes
    # "d j moore" while the projection's "DJ Moore" stays "dj moore". Glue
    # runs of single letters back together so both land on "dj moore".
    return re.sub(r"\b([a-z])\s+(?=[a-z]\b)", r"\1", s)
