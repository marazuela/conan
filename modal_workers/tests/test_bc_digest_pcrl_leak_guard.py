"""CI guard — p_crl must NEVER reach a rendered / digest surface (band-only invariant).

The CRL probability ``p_crl`` is persisted-internal on ``bc_rubric_scores`` only
(the bc_candidates G1 tier gate reads it). It must never appear in:
  * the ``bc-digest`` edge function (its read path or HTML/text render), nor
  * the ``bc_digest_rows()`` SQL reader's SELECT / RETURNS TABLE list.

The digest reader structurally omits p_crl and the renderer never references it; this
test fails the build if a future edit reintroduces it. Intentional "we omit p_crl"
documentation comments are allowed — only a *code* reference fails.

Scope grows automatically as the bc_digest files land under conan/supabase/ (so it is
a no-op until P2 ports the edge fn + reader, then becomes an active guard).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]  # modal_workers/tests/ -> repo root
SUPA = REPO / "supabase"

# The digest read + render surface. Globs (not a fixed list) so new files are covered.
DIGEST_GLOBS = (
    "functions/bc-digest/*.ts",
    "migrations/*bc_digest*.sql",
    "migrations/*digest_rows*.sql",
)

# A pure comment/doc line may mention p_crl (e.g. "INVARIANT: p_crl is never read").
_COMMENT_LINE = re.compile(r"^\s*(--|//|\*|/\*|#)")
# An in-line mention in an explicitly-omitting context is also allowed.
_ALLOW_CONTEXT = re.compile(
    r"omit|never|invariant|internal|forbidden|deliberately|no\s+p_crl|without\s+p_crl",
    re.IGNORECASE,
)


def _digest_files() -> list[Path]:
    # Production surface only — *.test.ts deliberately reference p_crl to PROVE it is
    # never rendered (the protective negative test), so they are excluded here.
    files: list[Path] = []
    for g in DIGEST_GLOBS:
        files.extend(p for p in SUPA.glob(g) if not p.name.endswith(".test.ts"))
    return sorted(set(files))


def test_no_pcrl_in_digest_surface() -> None:
    offenders: list[str] = []
    for f in _digest_files():
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if "p_crl" not in line:
                continue
            if _COMMENT_LINE.match(line) or _ALLOW_CONTEXT.search(line):
                continue
            offenders.append(f"{f.relative_to(REPO)}:{i}: {line.strip()}")
    assert not offenders, (
        "p_crl leaked into a digest surface (band-only invariant violated):\n"
        + "\n".join(offenders)
    )
