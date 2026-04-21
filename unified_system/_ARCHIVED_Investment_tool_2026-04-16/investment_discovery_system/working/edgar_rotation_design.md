# EDGAR Keyword Rotation Strategy — Design

**Created**: 2026-04-10 (Session 16)
**Status**: Design complete, implementation pending

---

## Problem

The EDGAR scanner (v2.2) has a 35-second wall-clock budget to avoid bash sandbox timeout (45s). With 4 keyword categories and 31 total keywords, plus filing-type searches, a full scan takes ~90-120s. Currently, only the first category ("activist") completes before the budget expires.

This means:
- **Distress signals** (going concern, covenant breach, material weakness) are NEVER scanned
- **M&A signals** (merger agreement, tender offer, definitive agreement) are NEVER scanned
- **Governance signals** (poison pill, auditor resignation, whistleblower) are NEVER scanned
- Only **activist signals** (8 keywords) and **filing-type signals** (SC 13D, NT 10-K) run

## Proposed Solution: Category Rotation

Implement a rotation file (`signals/edgar_rotation_state.json`) that tracks which category was last scanned. Each scan runs ONE category + the filing-type searches, completing well within the 35s budget.

### Rotation Order

Prioritized by signal value:
1. `activist` — Highest historical hit rate for actionable signals (SC 13D, "undervalued")
2. `mna` — Time-sensitive M&A events (definitive agreements, tender offers)
3. `distress` — Contrarian signals (going concern, covenant breach)
4. `governance` — Lower priority but captures corporate actions (poison pill, auditor resignation)

Full cycle: 4 scans = 4 categories. With hourly scans, all categories covered every 4 hours.

### State File Format

```json
{
  "last_category": "activist",
  "last_scan_ts": "2026-04-10T15:00:00Z",
  "rotation_index": 0,
  "scan_history": {
    "activist": "2026-04-10T15:00:00Z",
    "mna": "2026-04-10T14:00:00Z",
    "distress": "2026-04-10T13:00:00Z",
    "governance": "2026-04-10T12:00:00Z"
  }
}
```

### Implementation Changes

In `edgar_filing_monitor.py`:

1. Add `ROTATION_FILE` constant: `os.path.join(SIGNALS_DIR, "edgar_rotation_state.json")`

2. Add `--rotate` CLI flag (default: False). When True, reads rotation state and scans next category.

3. Modify `scan()` function:
   ```python
   if args.rotate:
       category = get_next_rotation_category()
       categories_to_scan = [category]
   elif args.category:
       categories_to_scan = [args.category]
   else:
       categories_to_scan = list(SIGNAL_KEYWORDS.keys())
   ```

4. After scan, update rotation state file.

5. Filing-type searches (`SIGNAL_FILING_TYPES`) always run regardless of rotation — they're fast (2 searches) and high-value.

### Benefits

- All 31 keywords covered across 4 scans (4 hours with hourly schedule)
- Each scan stays well within 35s budget (~15-20s per category)
- No missed signals — just delayed by up to 4 hours for non-priority categories
- Rotation state persists across sessions via file
- Backward compatible: `--rotate` is opt-in; default behavior unchanged

### Integration with `run_scanner.py`

Modify the `edgar` scanner entry in `run_scanner.py` to pass `--rotate` flag:
```python
if scanner == "edgar":
    cmd.extend(["--rotate"])
```

### Risk Assessment

- **Latency increase**: M&A/distress signals delayed up to 4 hours vs. real-time. ACCEPTABLE — our scanning window is already 24-48h, so 4h additional latency is noise.
- **State file corruption**: If rotation state corrupted, default to scanning "activist". Safe fallback.
- **Category-specific bugs**: If one category always errors, rotation advances past it. Add error handling to retry same category on next scan if it fails.

---

## Decision Required

This is a straightforward improvement. Record in DECISIONS.md as D-019 when implemented.
