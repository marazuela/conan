-- Drop delaware_chancery_scanner registry row.
--
-- Context (2026-05-11): F-203 audit decision — delete rather than implement
-- the CourtConnect docket-search surface (Q-002, requires session/frameset
-- handling). The scanner has been status='deprecated' since 20260510000010
-- and `last_run` is NULL (it never ran), so there is no orphan signal /
-- assessment data to clean up. Source files removed in the same commit:
--   modal_workers/scanners/delaware_chancery_scanner.py
--   modal_workers/tests/test_delaware_chancery_scanner.py
-- And the original INSERT was stripped from 20260429000000.

DELETE FROM public.scanners
 WHERE name = 'delaware_chancery_scanner';
