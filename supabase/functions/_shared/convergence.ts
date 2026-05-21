// Convergence classification — authoritative helper for the live Conan v2 reactor.
// Must stay behaviorally aligned with modal_workers/shared/rubric_engine.py::
// convergence_reference. Do NOT treat the legacy file-bus convergence copies in
// `Scoring engine/` or `unified_system/` as authoritative here: they diverge on
// post-bonus rebanding and other legacy details.

export type Direction = "long" | "short" | "neutral" | null | undefined;

export interface GroupSignal {
  signal_id: string;
  scoring_profile: string;
  thesis_direction: Direction;
  score: number;
  source_content_hash: string;
}

export type GroupType = "contradiction" | "same_direction" | "orthogonal" | "single";

export interface GroupVerdict {
  bonus: 0 | 5 | 10;
  type: GroupType;
  winner_signal_id: string | null;
  unique_signals: GroupSignal[];
}

// Classify a convergence group.
//   - contradiction: group contains both 'long' AND 'short' → bonus=0
//   - same_direction: all same direction + 2+ unique signals → +5 (2) / +10 (3+)
//   - orthogonal: same_direction but signals span different profiles → same bonus scale
//   - single: only one unique signal → bonus=0
//
// Dedup uses source_content_hash to collapse cross-listing echoes (v1 parity).
// Signals with null/undefined/"" hashes are treated as unique (keyed by signal_id)
// — matches rubric_engine.convergence_reference so convergence_qa stays honest.
//
// Bonus also requires at least one directional signal in the group: if every
// entry is `neutral`/null, no +5/+10 is awarded. Non-directional "something is
// happening here" filings (strategic_review, trading_update, board_change, etc.)
// were otherwise pushing watchlist-borderline scores into Immediate alerts with
// no actual directional thesis.
export function classifyGroup(signals: GroupSignal[]): GroupVerdict {
  // Dedup on source_content_hash (keep highest-scoring representative per hash).
  const byHash = new Map<string, GroupSignal>();
  for (const s of signals) {
    const h = s.source_content_hash;
    if (h === null || h === undefined || h === "") {
      byHash.set(`__no_hash__${s.signal_id}`, s);
      continue;
    }
    const existing = byHash.get(h);
    if (!existing || s.score > existing.score) byHash.set(h, s);
  }
  const unique = Array.from(byHash.values());
  if (unique.length === 0) return { bonus: 0, type: "single", winner_signal_id: null, unique_signals: [] };

  const dirs = new Set(unique.map((s) => s.thesis_direction).filter(Boolean));
  if (dirs.has("long") && dirs.has("short")) {
    return {
      bonus: 0,
      type: "contradiction",
      winner_signal_id: pickWinner(unique).signal_id,
      unique_signals: unique,
    };
  }

  if (unique.length === 1) {
    return {
      bonus: 0,
      type: "single",
      winner_signal_id: unique[0].signal_id,
      unique_signals: unique,
    };
  }

  // Require at least one directional (long|short) signal in the group to award
  // a bonus. Groups composed entirely of neutral/null directions converge on
  // "something is happening" with no actionable thesis — no bonus.
  const directional = dirs.has("long") || dirs.has("short");
  if (!directional) {
    return {
      bonus: 0,
      type: "single",
      winner_signal_id: pickWinner(unique).signal_id,
      unique_signals: unique,
    };
  }

  const profiles = new Set(unique.map((s) => s.scoring_profile));
  const type: GroupType = profiles.size > 1 ? "orthogonal" : "same_direction";
  const bonus: 0 | 5 | 10 = unique.length >= 3 ? 10 : 5;
  return { bonus, type, winner_signal_id: pickWinner(unique).signal_id, unique_signals: unique };
}

// Highest score wins; signal_id ASC breaks ties so the winner is deterministic
// across this TS reactor and the Python reference at
// modal_workers/shared/rubric_engine.py _pick_winner. Without the tiebreak the
// two implementations could pick different winners on score ties, producing
// spurious convergence_qa convergence_disagreement flags.
export function pickWinner(signals: GroupSignal[]): GroupSignal {
  return signals.reduce((best, s) => {
    if (s.score !== best.score) return s.score > best.score ? s : best;
    return s.signal_id < best.signal_id ? s : best;
  }, signals[0]);
}

// classify_band — exact threshold logic from rubric_engine.py.
export type Band = "immediate" | "watchlist" | "archive" | "discard";
export function classifyBand(score: number): Band {
  if (score >= 35) return "immediate";
  if (score >= 25) return "watchlist";
  if (score >= 15) return "archive";
  return "discard";
}

// Convergence window selection — 30 days if any signal in the candidate group is
// litigation-profiled, else 14 days.
export function windowDays(profiles: string[]): 14 | 30 {
  return profiles.includes("litigation") ? 30 : 14;
}

// signal_fingerprint for alerts dedup: sha256(source_content_hash | scoring_profile).
// Matches spec.md §6.2 + the alerts.UNIQUE(entity_id, signal_fingerprint, day_utc) constraint.
export async function signalFingerprint(source_content_hash: string, scoring_profile: string): Promise<string> {
  const text = `${source_content_hash}|${scoring_profile}`;
  const buf = new TextEncoder().encode(text);
  const digest = await crypto.subtle.digest("SHA-256", buf);
  return Array.from(new Uint8Array(digest)).map((b) => b.toString(16).padStart(2, "0")).join("");
}
