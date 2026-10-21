# Local patches — durable home for fork-fix changes

Hermes Agent (`~/.hermes/hermes-agent`) is a git fork of
`git@github.com:NousResearch/hermes-agent.git` and is **3017 commits
behind `origin/main`** as of 2026-10-21. Patches in this file live on
dedicated branches and survive `hermes update` because
`updates.parked_branch_strategy: update_in_place` makes the updater merge
upstream into the parked branch in place — it does not switch branches
or clobber the working tree.

## How to add a new local patch

1. **Verify upstream state first.** Default bias is to find an upstream
   fix, not write a new one (the `upstream-fork-patch-maintenance`
   skill, Step 3.5). Search the issue tracker and open PRs for the
   symptom before patching locally. If several open PRs already target
   the same root cause, surface that on the canonical PR / tracking
   issue rather than opening a duplicate.
2. **Branch from the live tree, not from `local-patches`.** The live
   tree is at the released commit, which is hundreds of branches ahead
   of the older `local-patches` branch. Checking `local-patches` out
   into the live tree would downgrade it by tens of thousands of
   commits. Always create a new branch from the live HEAD
   (`git checkout -b fix/<topic>`).
3. **Touch only what the patch needs.** `git add <file>` — never
   `git add .`. Verify `git status --short` and `git diff --cached
   --name-only` before every commit. Pre-existing dirty files (a
   frequent visitor to this repo) must never enter the index.
4. **Write a real commit message** that records:
   - what upstream does and why that is wrong here,
   - what the patch changes,
   - any durable-mechanism dependency the reader must reproduce
     (e.g. the parked-branch strategy, gate hooks, dispatcher
     channels).
5. **No `.orig` / `.bak` / re-patch shell scripts.** The patch lives
   on the branch and in the commit; tooling is not a back-channel.
6. **After `hermes update`** the parked branch is N commits behind
   `origin/main`. Rebase it onto fresh main before retesting:
   `git fetch origin main && git checkout fix/<topic> && git rebase
   origin/main`. If it conflicts, walk into it; `hermes update` left
   a backup at `~/.hermes/backups/pre-update-<stamp>.zip`.

## Active patches

### `fix/mcp-schema-cache-ttl-zero` — TTL=0 turns `mcp_servers.<name>.lazy` inert

**Symptom.** With `lazy: true` set on any MCP server, `_register_lazy_from_cache`
silently fell through to the eager connect on every startup. The lazy
flag was accepted and persisted and did nothing.

**Root cause (proven, not theorised).** The MCP Python SDK models
SEP-2549's `ttlMs` with default `0`. `_paginate_full_list`
(`tools/mcp_tool.py:294`) reads `ttl_ms` from the first page's
`tools/list` response and stores it in `_list_cache_meta`. The original
`write_cache_entry` (`tools/mcp_schema_cache.py`) then persisted it on
the `isinstance(ttl_ms, (int, float))` branch — `0` qualifies — and
`get_cached_entry`'s `expired` predicate evaluated
`(now − written_at) × 1000 >= float(0)`, true for every age.
The entry was a miss the instant it was written.

**Fix (commit `e43433fc8f`).** Centralise the "positive numeric TTL"
test in `_is_real_ttl(ttl_ms)` and apply it on BOTH paths:

- **Write:** only a positive TTL is persisted (no new entries are
  born expired; TTL-free entries regain the idempotent write-through
  skip).
- **Read:** an entry already on disk carrying `ttl_ms: 0` from a
  previous build is served, not treated as a miss until a live
  re-probe happens to rewrite it. The write-side fix alone would
  leave those machines broken for one extra probe cycle.

`bool` is excluded so `ttl_ms=True` is not silently stored as a 1 ms
lifetime.

**Measured blast radius on this machine** (cache file
`~/.hermes/cache/mcp_schema_cache.json`, 12 entries):

| ttl_ms on disk | count | lazy:true in config |
|---:|---:|---|
| `null` (no hint ever written) | 10 | 7 of 7 lazy servers |
| `0` (synthesized SDK default, was born-expired) | 2 (crawl4ai, rea) | 0 |

Neither of the two currently-broken servers is `lazy:true`, because
lazy servers never re-connect and therefore their entries predate the
TTL feature. The latent trap: any eager-registered server that later
gets `lazy:true` will write `ttl_ms=0` on its next reconnect and
permanently become eager again until this patch lands.

**Why `_paginate_full_list` cannot be left alone.** The card hypothesis
"`if not items` only fires on empty pages" is wrong. `not items` is
true on the **first** page iteration, which is virtually every server
in practice. The hint is read for both empty and non-empty first
pages, so the bug bites all entries written through the write-through
path — not only the empty-page edge case.

**Upstream status (as of 2026-10-21).** `origin/main` still carries
the identical buggy `isinstance(ttl_ms, (int, float))` writer and
`(now − written_at) >= float(ttl_ms)` reader. Six PRs target the same
fix and four have been closed as duplicates of `#92621`, which is
itself a **stale draft** (last touched 2026-09-14, no movement for
over a month). This is why the patch is local instead of upstreamed
yet — opening a seventh duplicate PR would be closed by the same
triage logic and add zero movement.

Tracked at:

- issue 101007 — canonical "schema cache `ttl_ms=0`" report
- PR 92621 — canonical draft (status: open · draft · stale)
- PR 101050, 108467, 127949, 134622, 135112 — open duplicates / variants
- PR 97410, 100816, 101390, 123637 — closed as duplicates of #92621

The local patch survives `hermes update` because it lives on this
branch and the updater merges `origin/main` into the parked branch in
place; the patch commit is preserved as a real commit on the
fast-forward / merge target.

### `fix/error-log-noise-20261021` — three upstream mismatches produced ~900 warnings/day

**Symptom.** A 24h census of `~/.hermes/logs/errors.log` found 1259 lines.
~900 of them were three unrelated warnings repeating, which buried the
signals that were real. None of the three is a functional bug; all three
are the local build disagreeing with upstream.

| # | Warning | Per 24h | Root cause |
|---|---|---|---|
| 1 | `tier must be one of official/community (got 'verified')` | 256 | The live catalog now ships a `verified` tier; this build hard-rejects it |
| 2 | `Token from GITHUB_TOKEN is not supported: Classic PAT…` | 329 | `resolve_copilot_token()` revalidates on every status/picker call |
| 3 | `kanban dispatcher stuck: ready queue non-empty … 0 workers spawned` | 43 | The health check never consulted the per-profile cap bucket |

**1 — catalog tier.** The live catalog holds 760 entries: 741 `community`,
15 `official`, 4 `verified` (`cua-spaces`, `nvidia-app`, `nvidia-broadcast`,
`tinyfish`). `entry_from_mapping` rejected the four and dropped them from
the picker. `tier` is display-only in `hermes_cli/plugin_catalog.py` — it
never gates installation or trust — so `CATALOG_TIERS` now accepts
`verified` and the four plugins return to the picker.

**2 — copilot warning.** `_copilot_acp_auth_evidence()` runs on every
status endpoint and model picker, and `resolve_copilot_token()` validates
`COPILOT_GITHUB_TOKEN` / `GH_TOKEN` / `GITHUB_TOKEN` in turn. A classic
PAT in `GITHUB_TOKEN` is not a valid Copilot token, so the same correct
warning fired 329 times. It is now emitted once per distinct
`(env var, reason)` pair per process.

**3 — dispatcher false positive.** `gateway/kanban_watchers.py` counted a
tick as bad whenever the ready queue was non-empty and nothing spawned,
then raised a "check profile health (venv, PATH, credentials)" alarm
without ever consulting the dispatcher's own
`skipped_per_profile_capped` bucket. With
`kanban.max_in_progress_per_profile=1` and one gate lane, every tick the
gate was busy looked like a stall — the alarm fired for 26 consecutive
ticks during a legitimate review. The condition is now the testable helper
`_tick_is_stuck()`: a queue held back only by `skipped_per_profile_capped`
/ `skipped_nonspawnable` / `auto_assigned_default` is *correctly idle* and
does not advance the bad-tick counter, while a respawn-guard hold, a
provider rate limit or memory pressure still does.
`describe_suppression()` now also names `per_profile_capped=N` so the
remaining alarm explains itself.

**Verification.** Each fix was re-broken individually and the matching test
confirmed to FAIL, then restored — a test that passes with and without the
fix proves nothing:

| Test | Fails when |
|---|---|
| catalog accepts `verified` tier | `CATALOG_TIERS` is reverted |
| copilot PAT warning emitted once | the dedupe guard is removed (5 warnings) |
| per-profile cap is not "stuck" | `_tick_is_stuck()` returns `True` unconditionally |
| suppression names `per_profile_capped` | the counter is removed |

All four pass with the patches applied. Existing suites: copilot + catalog
17/17, and the dispatch/catalog/copilot selection produces an IDENTICAL
failure set with and without the patch (1 failed / 17 passed both ways) —
no regression.

**Durable mechanism.** Same parked-branch strategy as the sibling patch:
`updates.parked_branch_strategy=update_in_place` makes `hermes update`
merge `origin/main` into this branch in place, so the patch commit
survives an update. Rebase onto fresh main before retesting:
`git fetch origin main && git checkout fix/error-log-noise-20261021 && git rebase origin/main`.
