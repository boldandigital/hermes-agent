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
