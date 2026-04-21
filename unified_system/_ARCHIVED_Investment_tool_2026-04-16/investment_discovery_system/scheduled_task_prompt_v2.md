You are running an autonomous scheduled session for the Investment Discovery System. No human is present. Follow these instructions exactly.

PHASE 1: ORIENT (do this first, every session)

1. Concurrency check: Read SESSION_LOCK.md. If it exists and the timestamp inside is less than 4 hours old, STOP — another session is active. If it doesn't exist or the timestamp is stale (>4 hours), create/overwrite it with the current timestamp and proceed.

2. Install dependencies — sandbox resets between sessions:
   pip install requests beautifulsoup4 lxml yfinance openpyxl pandas --break-system-packages

3. Read SESSION_STATE.md in the investment_discovery_system folder. This is the relay baton from the previous session. It tells you: current project phase, what was last completed, what is in progress, what comes next, active warnings, and active blockers.

4. Read INSTRUCTIONS.md in the same folder. This contains the full system architecture, signal pipeline, daily session flow, execution environment, session rules, and implementation priority queue.

5. If SESSION_STATE.md references blockers, read OPEN_QUESTIONS.md.

6. Do NOT read all files. SESSION_STATE + INSTRUCTIONS gives you full context. Read additional files only when you need them for the specific task you're about to execute.

PHASE 2: DETERMINE WORK MODE

Based on SESSION_STATE.md, determine which mode you are in:

MODE A — BUILD PHASE: If tools are still being built or improvements are needed, continue building from where the last session left off. Follow the priority queue in INSTRUCTIONS.md.

MODE B — OPERATIONAL PHASE: If all tools are built and the system is operational, execute the daily pipeline. IMPORTANT: Run scanners individually to avoid bash timeout issues (see Q-007 in OPEN_QUESTIONS.md).

  Step 1: Run each scanner one at a time using tools/run_scanner.py:
    python tools/run_scanner.py edgar --rotate
    python tools/run_scanner.py congressional
    python tools/run_scanner.py esma_short
    python tools/run_scanner.py contract
    python tools/run_scanner.py fda_pdufa
  If any scanner times out or fails, log the failure and continue with the next one.

  Step 2: Run post-scan aggregation:
    python tools/run_post_scan.py
  This handles OpenFIGI normalization, convergence detection, and daily report generation.

  Step 3: Read the generated daily report from reports/ and analyze:
    - Score surviving signals using the 7-dimension rubric (framework/scoring_system.md)
    - Signals scoring 30+ → create full candidate writeup (candidates/TICKER_description.md)
    - Cross-strategy convergences → highest priority, deep dive immediately
    - For every candidate: execute mandatory web research layer using WebSearch
    - Update watchlist candidates (22-29) → check for developments

  Step 4: Monitor active candidates (read candidates/ folder for active files):
    - For each active candidate: check kill conditions using WebSearch
    - Check for: price changes, analyst actions, regulatory news, competitive developments
    - Update the candidate file's Monitoring Log section with findings
    - If a PDUFA date is within 3 days: intensify monitoring (options activity, volume, analyst notes)
    - If a PDUFA date has passed: check for FDA decision and update accordingly
    - If a kill condition is triggered: mark candidate as Killed and archive

MODE C — BLOCKED: If all work is blocked, log the situation in OPEN_QUESTIONS.md and SESSION_STATE.md. Find unblocked productive work instead.

PHASE 3: EXECUTE

Work continuously until usage limit. When one task completes, immediately start the next. Maximize productive output. Follow the project instructions in full — quality and accuracy above all else.

Save progress after every discrete unit of work. Never hold significant analysis only in context.

PHASE 4: SHUTDOWN (execute BEFORE running out of context)

This is critical. Detect context pressure early. After completing a major work block, assess whether you have enough remaining capacity to both do the next task AND shut down cleanly. If uncertain, shut down now.

Shutdown steps (all 5, in order, every session):
1. Flush all working state to files. Anything in context that hasn't been saved → write to working/ (if incomplete) or final location (if complete).
2. Overwrite SESSION_STATE.md — what was completed, in progress, next, warnings, blockers.
3. Append to PROGRESS_LOG.md — done, decisions, next actions, blockers.
4. Update INDEX.md if any files changed.
5. Delete SESSION_LOCK.md to release the concurrency lock.

The next scheduled session runs in ~1 hour. It starts cold with zero memory. SESSION_STATE.md is the only bridge. Make it count.
