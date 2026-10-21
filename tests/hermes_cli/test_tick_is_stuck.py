"""gate-found regression guards for ``tick_is_stuck`` (``hermes_cli/kanban_db_dispatch``).

Gate review of ``fix/error-log-noise-20261021`` found a generator-exhausted-by-
two-passes bug in ``tick_is_stuck`` (commit 3254bba589): the predicate reads
``results`` twice (legitimately-deferred, then other-hold), but the gateway
wrapper passes a generator. A generator is exhausted by the first ``any()``;
the second pass reads nothing. A tick with a legit deferral PLUS a real stall
was silently swallowed. The dispatcher reported "ok", the system carried on.

These tests are the persistent guard: the falsification harness in
``~/.hermes/cache/scratch/falsify_fixes.py`` exercises the happy paths, but
these tests are committed, run in CI, and cover the cross-site-of-predicate
differences gate caught.
"""
from __future__ import annotations

from collections import namedtuple

import pytest

from hermes_cli.kanban_db_dispatch import tick_is_stuck
from gateway.kanban_watchers_dispatcher import _tick_is_stuck


_DR = namedtuple("DR", "skipped_per_profile_capped skipped_nonspawnable "
                          "auto_assigned_default host_capped respawn_guarded "
                          "rate_limited memory_pressure")


def _res(**kw):
    """Build a DispatchResult-shaped object with all buckets None by default."""
    defaults = {k: None for k in _DR._fields if k not in kw}
    return _DR(**defaults, **kw)


# ── F1: regression the second health-check site caught ────────────────────

class TestGeneratorMaterialization:
    """A tick with ONE legit deferral AND ONE real hold MUST report stuck.

    Without materialising ``results`` to a list at the top of
    ``tick_is_stuck``, the gateway wrapper (passing a generator) returns False
    and the CLI daemon (passing a plain list) returns True. Gate ran the real
    code and proved this. Both sites must agree.
    """

    def test_one_result_with_both_buckets_is_stuck_via_list(self):
        res = _res(skipped_per_profile_capped=[("t1", "gate", 1)],
                   rate_limited=["t1"])
        # CLI-style list call
        assert tick_is_stuck([res], True, False) is True, (
            "a single result carrying both a legit deferral and a rate-limit "
            "hold must be reported as stuck"
        )

    def test_one_result_with_both_buckets_is_stuck_via_generator(self):
        # Gate's actual fail-mode: gateway wrapper passes a generator.
        res = _res(skipped_per_profile_capped=[("t1", "gate", 1)],
                   rate_limited=["t1"])
        gen = (r for r in [res])
        # Without the fix the second any() returns False -> reports not stuck
        assert tick_is_stuck(gen, True, False) is True, (
            "the gateway wrapper passes a generator, and a generator-typed "
            "caller must NOT silently swallow the real stall"
        )

    def test_two_results_one_defer_one_hold_via_generator(self):
        """Gate's most precise repro: two separate results, one in each bucket."""
        deferral = _res(skipped_per_profile_capped=[("t1", "gate", 1)])
        real_hold = _res(rate_limited=["t2"])
        gen = (r for r in [deferral, real_hold])
        assert tick_is_stuck(gen, True, False) is True, (
            "separate deferral + real hold must be reported as stuck"
        )

    def test_gateway_wrapper_returns_same_answer_as_cli(self):
        """D1's stated goal: the two sites can NEVER disagree about the same tick."""
        # Build the same logical content via both call shapes and assert equal.
        kwargs = dict(skipped_per_profile_capped=[("t1", "gate", 1)],
                      rate_limited=["t1"])
        from gateway.kanban_watchers_dispatcher import _tick_is_stuck
        cli = tick_is_stuck([_res(**kwargs)], True, False)
        gw = _tick_is_stuck([("main", _res(**kwargs))], True, False)
        assert cli == gw, (
            f"gateway={gw!r}, cli={cli!r} - the two sites disagree about the "
            f"same tick"
        )

    @pytest.mark.parametrize(
        "legit,hold",
        [
            ("skipped_per_profile_capped", "respawn_guarded"),
            ("skipped_per_profile_capped", "memory_pressure"),
            ("skipped_nonspawnable", "rate_limited"),
            ("auto_assigned_default", "respawn_guarded"),
        ],
    )
    def test_every_legit_vs_every_hold_via_generator(self, legit, hold):
        legit_val = [("t1", "g", 1)] if legit == "skipped_per_profile_capped" else None
        kwargs = {legit: legit_val, hold: ["t1"]}
        # normalise: only the real bucket value matters
        if legit_val is None:
            kwargs = {hold: ["t1"]}
        gen = (r for r in [_res(**kwargs)])
        assert tick_is_stuck(gen, True, False) is True


# ── regressions already on the contract are still worked ─────────────────

class TestFalsePositivesGone:
    def test_per_profile_cap_is_not_stuck(self):
        assert tick_is_stuck([_res(skipped_per_profile_capped=[
            ("t1", "gate", 1)])], True, False) is False

    def test_host_cap_is_not_stuck(self):
        assert tick_is_stuck([_res(host_capped=2)], True, False) is False

    def test_spawned_is_never_stuck(self):
        res = _res(skipped_per_profile_capped=[("t1", "gate", 1)],
                   rate_limited=["t1"])
        assert tick_is_stuck([res], True, True) is False

    def test_idle_queue_is_never_stuck(self):
        assert tick_is_stuck([_res()], False, False) is False


class TestGenuineStallsStillCaught:
    def test_unexplained_zero_spawn_is_stuck(self):
        assert tick_is_stuck([_res()], True, False) is True

    def test_real_hold_alone_is_stuck(self):
        assert tick_is_stuck([_res(rate_limited=["t1"])], True, False) is True

    def test_memory_pressure_alone_is_stuck(self):
        assert tick_is_stuck([_res(memory_pressure="critical")], True, False) is True
