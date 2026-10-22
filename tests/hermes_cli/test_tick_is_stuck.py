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
        """Gate's most precise repro: two separate results, one in each bucket.

        ORDER MATTERS for the generator bug. The hold must come FIRST: the
        first ``any()`` (legitimate-deferred) pulls the hold result, finds no
        legit bucket on it, and keeps pulling until it reaches the deferral,
        which satisfies it and stops. The generator is then exhausted, so the
        second ``any()`` (other-hold) reads nothing and the real stall is
        swallowed. With the deferral FIRST the first ``any()`` short-circuits
        before consuming the hold, so the second pass still sees it - that
        ordering passes with and without the fix and proves nothing.
        """
        real_hold = _res(rate_limited=["t2"])
        deferral = _res(skipped_per_profile_capped=[("t1", "gate", 1)])
        gen = (r for r in [real_hold, deferral])
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

    # A truthy value for EVERY legitimate-deferral bucket, so no matrix row
    # silently degrades into a hold-alone case (which passes with or without
    # the fix and therefore proves nothing).
    _LEGIT_VALUE = {
        "skipped_per_profile_capped": [("t1", "gate", 1)],
        "skipped_nonspawnable": ["t1"],
        "auto_assigned_default": ["t1"],
    }
    _HOLD_VALUE = {
        "respawn_guarded": [("t1", "active_pr")],
        "rate_limited": ["t1"],
        "memory_pressure": "critical",
    }

    @pytest.mark.parametrize("legit", sorted(_LEGIT_VALUE))
    @pytest.mark.parametrize("hold", sorted(_HOLD_VALUE))
    def test_every_legit_vs_every_hold_via_generator(self, legit, hold):
        """Every legit bucket x every hold bucket, hold FIRST, generator-fed.

        Gate's non-blocking finding on the first version of this matrix: rows
        whose legit bucket was not ``skipped_per_profile_capped`` fell through
        a branch that DROPPED the deferral bucket entirely, turning them into
        hold-alone cases that pass with or without the fix. Every row here
        carries a real deferral, so every row must fail without the fix.
        """
        kwargs = {legit: self._LEGIT_VALUE[legit], hold: self._HOLD_VALUE[hold]}
        # hold first -> the first any() consumes past the hold -> the second
        # any() would read an exhausted generator without the materialisation
        gen = (r for r in [_res(**kwargs)])
        assert tick_is_stuck(gen, True, False) is True, (
            f"legit={legit} + hold={hold} must be reported as stuck"
        )


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
