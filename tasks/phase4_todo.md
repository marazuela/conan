# Phase 4 — UI Dashboard Build Checklist

Derived from the approved plan at `~/.claude/plans/plan-it-partitioned-thompson.md`. Build commits 1-14 landed; commit 15 (Vercel deploy + smoke) is Pedro-side action.

## Prerequisites (Phase 1 dependencies, not Phase 4 scope)

- [ ] Spec Appendix A DDL filed into `supabase/migrations/*.sql` and pushed to project `xvwvwbnxdsjpnealarkh`. Current live schema is **missing 3 tables**: `operator_flags`, `candidate_aging_failures`, `phase3_base_rates`. Dashboard handles `operator_flags` gracefully via a "schema pending" placeholder.
- [x] `ALTER PUBLICATION supabase_realtime ADD TABLE signals, alerts, candidates;` shipped in `supabase/migrations/20260423000000_realtime_publication.sql` (applied 2026-04-20 23:38 UTC as remote migration `realtime_publication`). Verified: `SELECT tablename FROM pg_publication_tables WHERE pubname='supabase_realtime'` returns `alerts, candidates, signals`.
- [ ] Pedro's email (and any collaborators) added to `auth.users` via Supabase Studio before smoke.

## Build

- [x] **Commit 1 — Bootstrap.** `pnpm create next-app` + Tailwind v4 + shadcn init (zinc palette); `.nvmrc`. **AC ✓** `pnpm build` returns 200; `/` prerender works.
- [x] **Commit 2 — Typegen wiring.** `scripts/gen-types.sh` + `types/database.ts` (1136 lines from live conan project); `package.json` scripts `typegen` + `typecheck`. **AC ✓** enums `signal_band` + `candidate_state` present.
- [x] **Commit 3 — Supabase clients + env.** `lib/supabase/{server,client,proxy}.ts`; `.env.local.example` with `NEXT_PUBLIC_SUPABASE_URL` + `NEXT_PUBLIC_SUPABASE_ANON_KEY`. **AC ✓** throwaway RSC rendered `scanners count: 0` via live Supabase read.
- [x] **Commit 4 — Proxy + auth shell.** `proxy.ts` (Next 16 rename), `/login`, `/auth/callback`, `/logout`. **AC ✓** unauthed `/` → 307 `/login`; `/login` 200; `/auth/callback` without code → 307 `/login?error=missing_code`.
- [x] **Commit 5 — App shell.** zinc-950 bg, Geist Mono, green-400 accents, top nav, user-email badge. `(app)` route group; 6 nav-route stubs. **AC ✓** all routes render inside shell.
- [x] **Commit 6 — Home (`/`).** 4 cards: Signals 24h, Immediate 7d, Scanner rollup, Open flags by severity. Graceful fallback for missing `operator_flags` table (`PGRST205` → "schema pending" card). **AC ✓**
- [x] **Commit 7 — `/scanners`.** Card grid with name, geography, cadence, last-run badge, status classification (green/yellow/red/idle + stale > 2× cadence). **AC ✓** empty state when registry unseeded.
- [x] **Commit 8 — `/signals` list.** RSC + filter bar (band, profile, scanner, ticker prefix) + keyset pagination `(scan_date DESC, signal_id DESC)` 50/page, URL state shareable. **AC ✓**
- [x] **Commit 9 — `/signals` realtime.** `realtime-banner.tsx` client component subscribes `postgres_changes INSERT` on `signals`; "+N new" banner → `router.refresh()`. **AC ✓** build + compile clean; live verification in commit 15.
- [x] **Commit 10 — `/signals/[id]`.** Detail: dimensions, auto-caps, metadata, convergence siblings (limit 10), filing (joined by `source_content_hash`), raw_payload JSON. **AC ✓** email-template URL shape `/signals/{id}` resolves.
- [x] **Commit 11 — `/convergence`.** 30d window, group by `convergence_key`, multi-member filter, classification (same_direction / contradiction / orthogonal / neutral) inferred from `thesis_direction` set, winner highlighted. **AC ✓**
- [x] **Commit 12 — `/candidates` Kanban + `/candidates/[id]` detail.** 4-column Kanban read-only; dossier markdown via `react-markdown` + `remark-gfm`; events timeline; rationale header card; `thesis_drafting_failures` banner on Kanban when `resolved_at IS NULL`. **AC ✓**
- [x] **Commit 13 — `/flags` + resolve.** Open + Resolved-7d tabs; inline resolve form (server action sets `resolved_by=auth.uid()`); `revalidatePath('/flags')` after update; "schema pending" placeholder when table missing. **AC ✓** (runtime verification gated on Phase 1 migration.)
- [x] **Commit 14 — `/reports`.** Recursive Storage list (`reports/<yyyy>/<mm>/...`), 12 most recent, signed URL via server action (3600s TTL). **AC ✓** graceful "bucket not readable" state.
- [x] **Commit 15 — Vercel deploy.** Deployed via `vercel --prod` to `https://conan-dashboard.vercel.app` (project `marazuelas-projects/conan-dashboard`, id `prj_ncAFB6RbqAE2yjYaVMNaVAuJSCTx`). Env vars set across production/preview/development. HEAD smoke: `/` → 307 `/login`; `/login` 200; `/auth/callback` → `/login?error=missing_code`. Build: 12s, deploy: 30s, served from `cdg1` Paris edge (co-located with Supabase).

### Remaining for Phase 4 exit criterion (Pedro-side Supabase Studio)

- [x] Supabase Studio → Authentication → URL Configuration. Verified 2026-05-21 via management API: Site URL `https://conan-dashboard.vercel.app`; uri_allow_list includes `https://conan-dashboard.vercel.app/**`, `https://conan-dashboard-*.vercel.app/**`, `https://*.vercel.app/auth/callback`, `http://localhost:3000/**`, `http://localhost:3456/**`.
- [x] Supabase Studio → Authentication → Users: `pedro@solutz.com` and `javiergorordo13@hotmail.com` present, email-confirmed.
- [x] Magic-link smoke: `pedro@solutz.com` last_sign_in 2026-05-06; `javiergorordo13@hotmail.com` last_sign_in 2026-04-20.
- [x] fanout edge function env: `DASHBOARD_URL=https://conan-dashboard.vercel.app` set 2026-05-21 (previously unset → emails were defaulting to `https://conan.example.com` placeholder).
- [ ] (Optional) DNS: `conan.solutz.com` CNAME → `cname.vercel-dns.com`; add domain in Vercel → Project → Domains. Note: Pedro's Vercel account currently owns `solutz.app`, not `solutz.com` — confirm the target host before doing the CNAME.

## Smoke summary (local, unauthed — pre-deploy)

All 11 routes HTTP-verified on `localhost:3456`:

```
/                   307 → /login   (proxy auth ✓)
/signals            307 → /login
/convergence        307 → /login
/candidates         307 → /login
/scanners           307 → /login
/flags              307 → /login
/reports            307 → /login
/login              200             (public)
/auth/callback      307 → /login?error=missing_code
```

`pnpm build` clean; all routes typed + dynamic; `ƒ Proxy (Middleware)` detected.

## Hand-off

- [x] Dashboard code at `dashboard/` (~25 files, ~1k LoC).
- [x] README.md at `dashboard/README.md` doubles as Pedro's deploy checklist.
- [ ] Pedro executes the 5 deploy steps in `dashboard/README.md`, runs the smoke test.
- [ ] Phase 5 (review queue, annotations, rationales editor) builds on Phase 4 scaffolding.
