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

**4 — host-wide cap is "correctly idle", not "stuck".** The same
broad-stall check also ignored `DispatchResult.host_capped`, the bucket
for the host-wide `kanban.max_in_progress` cap. A host that's already
running `max_in_progress` tasks and where the next ready task is therefore
deferred was logged as a dispatcher-stall alarm. Commit `28df077c69`
adds the `host_capped` bucket on the dispatch result and includes it in
the legitimate-deferral set.

**5 — D1/D2 follow-ups.** Gate review of this branch found two real
defects in the original three fixes:

- The CLI daemon at `hermes_cli/kanban_ops.py:_on_tick()` carried its own
  inline copy of the bad-tick logic, so the embedded gateway dispatcher
  and the CLI daemon could disagree about the same tick. The predicate
  now lives ONCE in `hermes_cli.kanban_db_dispatch.tick_is_stuck()`; the
  gateway wrapper at `gateway/kanban_watchers_dispatcher._tick_is_stuck`
  delegates to it; `kanban_ops.py` calls it directly.

- `describe_suppression` ASSIGNED `host_capped` where every sibling key
  ACCUMULATES, and conflated running-workers with deferred-tasks. It is
  now `counts["host_capped_running"] = max(...)` — named apart so the
  unit is unambiguous, `max()`-combined because the same host appears on
  every board (summing would double-count).

**6 — generator-exhausted-by-two-passes (F1).** The D1 refactor moved
the predicate to a function that calls `any(results)` twice — once for
the legitimate-deferral set, once for the real-hold set — but the
gateway wrapper passes a **generator**, not a list. The first `any`
drinks the generator dry; the second reads an empty cup. A tick with a
legit deferral PLUS a real stall was silently swallowed: the dispatcher
reported "ok" while a real hold was in effect. Gate ran the real code and
proved this — the CLI and gateway sides disagreed about the same tick in
the mixed-deferral-plus-hold case. Commit `3254bba589` materialises
`results` to a list at the top of `tick_is_stuck`. The committed pytest
in `tests/hermes_cli/test_tick_is_stuck.py` has the regression case as
its first test class.

**Verification.** Each fix was re-broken individually and the matching test
confirmed to FAIL, then restored — a test that passes with and without the
fix proves nothing:

| Test (in `~/.hermes/cache/scratch/falsify_fixes.py`) | Fails when |
|---|---|
| catalog accepts `verified` tier | `CATALOG_TIERS` is reverted |
| copilot PAT warning emitted once | the dedupe guard is removed (5 warnings) |
| per-profile cap is not "stuck" | `_tick_is_stuck()` returns `True` unconditionally |
| suppression names `per_profile_capped` | the counter is removed |
| host-wide cap is not "stuck" | `counts["host_capped_running"]` is reverted to `host_capped=` |
| both health checks share one predicate | `kanban_ops.py` goes back to its own inline copy |
| toolaria obsolete warning suppressed | the registry probe is reenabled |

All seven pass with the patches applied. The committed pytest
`tests/hermes_cli/test_tick_is_stuck.py` adds 20 cases. Gate's re-verdict
card `t_3ea1c917` returned **SHIP** with one non-blocking finding: the
first version of the matrix self-normalised — any legit bucket other than
`skipped_per_profile_capped` fell through a branch that DROPPED the
deferral bucket, turning those rows into hold-alone cases that pass with
or without the fix, and the two-result case ordered the deferral first so
the first `any()` short-circuited before consuming the hold. The matrix
now carries a real deferral on every row, orders the hold FIRST (the
order in which the generator is actually exhausted), and covers all
3 legit buckets x 3 hold buckets. Stripping the materialisation now fails
**12 of the 20** tests, up from 4 — every matrix row bites.

Existing suites: the host plugin's `sdk-only` and `regression`
collections report **17/17** with the patches applied (verified before
the cleanup touched their plugin — they exercise the same surface).
The dispatch/catalog/copilot pytest subset produces an **identical**
failure set with and without the patch (1 failed / 17 passed both ways)
— no regression in the affected subset.

**Run the tests with the venv's own interpreter** (`.venv/bin/python`,
Python 3.14.7) — not a system `python3.11` against the 3.11
site-packages, which `uv sync` has correctly pruned (`ruamel-yaml` is a
`python_full_version >= '3.14'` dependency in `uv.lock`, so the 3.11 tree
does not carry it and `hermes_yaml.py` cannot import there).

**Note on the `tier` claim (gate's F4).** `tier` is display-only for
`entry_from_mapping` — the site that was failing — and for the picker.
One downstream consumer, `shared_metrics_catalog.catalog_platform_names`,
filters by `e.tier in CATALOG_TIERS`; with `verified` now accepted, that
filter admits the four previously-dropped entries whose `category` is
`platform` (the consumer requires BOTH `category == "platform"` AND
`tier in CATALOG_TIERS`). The four entries gate cared about are tool /
service categories, not `platform`, so the platform list is unaffected.

**Durable mechanism — and the two ways it silently failed (2026-10-22).**
Same parked-branch strategy as the sibling patch:
`updates.parked_branch_strategy=update_in_place` makes `hermes update`
merge `origin/main` into the checked-out branch in place, so the patch
commits survive an update.

Two failure modes killed that guarantee in practice, both now fixed:

1. **`origin` was the OFFICIAL repo.** `hermes_cli/update_channel.py:
   _tracks_official_origin()` decides the update channel from
   `remote.origin.url`. With `origin` = `NousResearch/hermes-agent` the
   updater followed `stable`, checked out upstream's published tree and
   left the checkout **detached** — the fork's patches went dormant and
   the live gateway ran unpatched upstream code. The remotes are now in
   the standard layout: `origin` = `boldandigital/hermes-agent` (the
   private fork), `upstream` = `NousResearch/hermes-agent`. The updater
   now resolves `default_channel = main` and never detaches.

2. **The patches were not on the fork's `main`.** `hermes_cli/
   update_cmd_git.py` computes the parked branch as
   `git rev-list --count origin/{target}..HEAD` — the *currently checked
   out* branch — and `auto_switch_parked_branch` checks out `origin/main`
   afterwards. If the fork's `main` lacks the patches, the next update
   checks out unpatched code again. The patches now live on the fork's
   `main`.

**Where the patch ended up.** The error-log-noise branch was folded into
`fix/gateway-self-maintenance-lock` (merge `9c7330e0e7`), then upstream
`main` (`4d04303818`) was merged on top (merge `9593de5569`). The result
was pushed to the private fork's `main` and to the branch itself, so
`origin/main` = the fully patched tree. Upstream had meanwhile fixed the
catalog tier on its own (`CATALOG_TIERS = ("official", "verified",
"community")`), so the merge dropped that one patch as redundant and
resolved with **zero conflicts**.

**Verification against the merged tree** (upstream had in fact touched
**none** of the five patched files — the merge was conflict-free by
*disjointness*, not by textual resolution — so every fix was still
re-falsified against the merged tree rather than assumed intact):

| Check | Result |
|---|---|
| falsification harness | 7/7 pass |
| committed pytest `test_tick_is_stuck.py` | 20/20 pass |
| relevant subset (`test_copilot_auth.py` + the above) | 35/35 pass |
| materialisation stripped | 12 of 20 tests FAIL (the bug bites) |
| copilot dedupe removed | its harness test FAILS |
| live error log after the gateway restart | all 10 tracked classes at 0 |

**Known false positive, deliberately not "fixed".** The context scanner
flags `invisible_unicode_U+200D` for `SOUL.md` because the pirate-flag
emoji `🏴‍☠️` contains a ZERO WIDTH JOINER. It is one ZWJ inside a valid
emoji sequence in a file Lars chose. Exempting ZWJ from
`tools/threat_patterns.py:INVISIBLE_CHARS` would weaken a security
control to silence one log line, and removing the flag would change the
agent's identity. Neither is a good trade; it is left visible on purpose.

