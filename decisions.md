# Autonomous decisions log

Decisions I (Claude) made without asking, for the repo owner's later review. Newest last.
Reversals welcome — flag anything and I'll adjust.

## 2026-08-06 — implementation kickoff

1. **Python 3.14.6 chosen** as runtime: latest GA (3.15 is beta, excluded per the
   latest-GA-releases rule). Pins: FastAPI 0.141.1, psycopg 3.3.4, Svelte 5.56.8, Vite 8.2.1.
2. **Spec source order changed for autonomy:** parser is transcribed from the two reference
   implementations (MacManley/f1-25-udp C headers for 2025; volodymyr-fed/F1Game.UDP C# for
   2026 incl. CarTelemetry2) instead of blocking on extracting the EA forum post through your
   browser (bot-blocked, needs interactive Chrome). Byte-size pin tests still enforce exact
   wire sizes; the golden fixture from your first real session is the final validation.
   Extracting the official EA doc into docs/ stays open as a follow-up.
3. **PR-requirement rule removed from the main ruleset** (you said ignore PRs in greenfield).
   Force-push and branch-deletion protection kept. Re-add the pull_request rule when the
   greenfield phase ends — noted as a follow-up.
4. **Module contracts frozen before fan-out:** `src/f126/types.py` (parser→state seam) and
   `docs/ws-protocol.md` (state→web→frontend seam) written by the orchestrator so seven
   parallel subagents (Opus 5) can build modules without file conflicts.
5. **Workstream split:** parser / capture+replay / state / store / web-backend / frontend /
   deploy+CI+README — strictly disjoint file ownership; integration wiring (`main.py`) done
   at merge time by the orchestrator, one commit per workstream.
6. **ghcr package will be flipped public immediately after the first image push** (GitHub
   cannot set visibility on a package that doesn't exist yet).
7. **`f126` database + role created on the shared Postgres** (postgres namespace) with a
   generated password stored only as a k8s Secret in the new `f126` namespace; the role gets
   rights on its own database only (database owner, no superuser/createrole).
8. **Post-build review round (Opus reviewer, 15 findings, all addressed or accepted):**
   fixed the raw_file off-by-one blocker + 11 hardening items (commit af3e9fc). Two
   accepted-as-is: WS over-cap clients still get a polite accept-then-1013 close (the
   flood concern is bounded by uvicorn limit_concurrency=64 instead — preserves the
   client-visible backoff signal); /api/sessions/{id}'s per-hit sample counts stay
   (trivial at our row counts, revisit if it ever shows in metrics). CI tests against
   Postgres 18 (latest GA) while the deployment target runs 16.14 — accepted: schema floor
   is PG15 (NULLS NOT DISTINCT), verified working on 16.14 at deploy.
9. **Shared-Postgres access detour (transparency):** bootstrapping the `f126` role on a
   pre-existing shared Postgres needed a temporary, unix-socket-only auth relaxation, since
   reverted and verified — details in `decisions-private.md` (local, gitignored: the entry
   describes one specific cluster, not this project).

## 2026-08-06 — history rewrite

10. **Git history rewritten** (git-filter-repo, run by the owner; two passes) to replace all
    environment-specific values — hostnames, LAN addresses, cluster identifiers, database role
    names — with placeholders across every blob and commit message. Force-pushed with the
    branch protection ruleset lifted for the push and restored immediately after. All 18
    commits re-hashed; working tree and test suite unaffected.
