"""Map an sNDA raw rank score to a percentile.

The sNDA model is rank-only (see ``snda_scorer``). We surface a percentile
rather than the uncalibrated probability. The reference distribution is the
pooled out-of-fold score set bundled at ``models/snda_oof_reference.csv``
(column ``p_oof``).

Caveat: the OOF scores come from the rolling-origin per-fold models, while
live scoring uses the full-fit coefficients — slightly different scales. The
OOF set is therefore an *approximate* empirical reference, adequate for a
triage rank. Over time, refresh the reference against the live full-fit-scored
efficacy-supplement universe (see plan follow-on). Callers may pass an explicit
``reference`` to override the bundled set.
"""

from __future__ import annotations

import bisect
import csv
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Sequence

REFERENCE_PATH = Path(__file__).resolve().parent / "models" / "snda_oof_reference.csv"


@lru_cache(maxsize=1)
def _load_reference(path: str = str(REFERENCE_PATH)) -> tuple:
    scores: List[float] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                scores.append(float(row["p_oof"]))
            except (KeyError, TypeError, ValueError):
                continue
    return tuple(sorted(scores))


def to_percentile(raw_score: float, reference: Optional[Sequence[float]] = None) -> float:
    """Return the percentile (0..100) of ``raw_score`` within ``reference``.

    Percentile = fraction of reference scores <= raw_score, times 100. Uses the
    bundled OOF reference when ``reference`` is None. Returns 0.0 for an empty
    reference (no rank information available).
    """
    ref = tuple(sorted(reference)) if reference is not None else _load_reference()
    n = len(ref)
    if n == 0:
        return 0.0
    rank = bisect.bisect_right(ref, float(raw_score))
    return 100.0 * rank / n
