"""Memory-derived dispatch caps — derived from AVAILABLE memory, not total RAM.

Two properties are policed here:

1. ``derive_default_max_in_progress`` must size the cap from memory that is
   actually available to workers.  Total RAM overstates that budget: the OS,
   the desktop app, the browser and any co-tenant VM are not available to a
   worker.  The old total-RAM formula returned 8 on an 8 GiB host, which is
   the shape of the 2026-10-22 OOM cascade.

2. An ESTIMATED memory reading that is implausibly small must NOT be
   classified as ``critical``.  ``critical`` freezes dispatch for EVERY
   profile, so a sampler bug reporting near-zero available would take the
   whole fleet down with no way to recover but fixing the sampler.  The floor
   applies only to samples carrying ``mem_available_estimated`` (the Darwin
   sampler, which computes a reclaimable-page figure because macOS publishes
   no MemAvailable).

3. An AUTHORITATIVE reading is never floored.  On Linux ``MemAvailable`` comes
   straight from ``/proc/meminfo``; a genuine reading under 2% of total is a
   real emergency and must stay ``critical``.  Flooring it would keep spawning
   workers into an OOM — the opposite of what the guard exists for.
"""

from __future__ import annotations

from hermes_cli.kanban_db_dispatch import (
    _memory_pressure_level,
    derive_default_max_in_progress,
)

EIGHT_GIB_TOTAL_KIB = 8 * 1024 * 1024
EIGHT_GIB_AVAILABLE_KIB = 1500 * 1024  # ~1.5 GiB reclaimable on the mini


# ---------------------------------------------------------------------------
# derive_default_max_in_progress
# ---------------------------------------------------------------------------


def test_small_host_does_not_derive_a_wide_fan_out() -> None:
    """An 8 GiB host with 1.5 GiB available must not derive an 8-worker default."""
    cap = derive_default_max_in_progress(
        {"mem_total_kib": EIGHT_GIB_TOTAL_KIB, "mem_available_kib": EIGHT_GIB_AVAILABLE_KIB}
    )
    assert cap == 2, f"expected the floor, got {cap}"


def test_derived_cap_ignores_total_ram_when_available_is_known() -> None:
    """Total RAM must not be able to inflate the cap when available is known."""
    with_total = derive_default_max_in_progress(
        {"mem_total_kib": 64 * 1024 * 1024, "mem_available_kib": EIGHT_GIB_AVAILABLE_KIB}
    )
    without_total = derive_default_max_in_progress(
        {"mem_available_kib": EIGHT_GIB_AVAILABLE_KIB}
    )
    assert with_total == without_total == 2


def test_derived_cap_falls_back_to_total_ram_when_available_is_missing() -> None:
    """A sample carrying only total RAM keeps the pre-existing behaviour.

    This is the platform-without-a-sampler path. Dropping the cap entirely
    here would silently remove the incident coverage that
    ``test_kanban_memory_guard.py`` asserts for small VMs.
    """
    assert derive_default_max_in_progress({"mem_total_kib": 1 * 1024 * 1024}) == 2
    assert derive_default_max_in_progress({"mem_total_kib": EIGHT_GIB_TOTAL_KIB}) == 8


def test_derived_cap_floor_still_allows_progress() -> None:
    assert derive_default_max_in_progress({"mem_available_kib": 100 * 1024}) == 2


def test_derived_cap_ceiling_still_binds() -> None:
    assert derive_default_max_in_progress({"mem_available_kib": 64 * 1024 * 1024}) == 8


def test_derived_cap_unknown_memory_is_none() -> None:
    """No usable figure at all => no cap. A platform without a sampler is unaffected."""
    assert derive_default_max_in_progress({}) is None
    assert derive_default_max_in_progress({"mem_available_kib": 0}) is None
    assert derive_default_max_in_progress({"mem_available_kib": -5}) is None
    assert derive_default_max_in_progress({"mem_available_kib": True}) is None
    assert derive_default_max_in_progress({"mem_available_kib": "1500"}) is None
    assert derive_default_max_in_progress({"mem_total_kib": 0}) is None
    assert derive_default_max_in_progress({"mem_total_kib": -5}) is None
    assert derive_default_max_in_progress({"mem_total_kib": True}) is None
    assert derive_default_max_in_progress({"mem_total_kib": "1048576"}) is None


# ---------------------------------------------------------------------------
# _memory_pressure_level
# ---------------------------------------------------------------------------


def test_implausible_estimated_reading_is_unknown_not_critical() -> None:
    """An ESTIMATED sampler bug reporting near-zero available must not brick dispatch."""
    sample = {
        "mem_total_kib": EIGHT_GIB_TOTAL_KIB,
        "mem_available_kib": 65 * 1024,
        "mem_available_estimated": True,
    }
    assert _memory_pressure_level(sample) == "unknown"


def test_naive_free_only_sample_would_have_frozen_dispatch() -> None:
    """The exact reading a naive (free-pages-only) Darwin sampler produces.

    Without the implausibility floor this classifies as ``critical``, which
    returns ``False`` from the dispatch guard and spawns nothing for any
    profile.  This test pins that the floor catches the estimated figure.
    """
    naive = {
        "mem_total_kib": EIGHT_GIB_TOTAL_KIB,
        "mem_available_kib": 65 * 1024,
        "mem_available_estimated": True,
    }
    assert _memory_pressure_level(naive) != "critical"


def test_empty_sample_is_unknown() -> None:
    assert _memory_pressure_level({}) == "unknown"


def test_plausible_pressure_is_still_classified() -> None:
    """A real, plausible reading must still be classified normally.

    50% of total available is comfortable; 10% is 'elevated'.  Both are
    above the 2% implausibility floor, so they must not degrade to
    ``unknown`` — otherwise the guard would be blind in normal operation.
    """
    comfortable = {"mem_total_kib": EIGHT_GIB_TOTAL_KIB, "mem_available_kib": EIGHT_GIB_TOTAL_KIB // 2}
    assert _memory_pressure_level(comfortable) == "ok"

    tight = {"mem_total_kib": EIGHT_GIB_TOTAL_KIB, "mem_available_kib": EIGHT_GIB_TOTAL_KIB // 10}
    assert _memory_pressure_level(tight) == "elevated"


def test_estimated_reading_below_the_floor_is_unknown_not_critical() -> None:
    """Documented trade-off for ESTIMATED figures: below 2% of total the reading
    is treated as untrustworthy rather than an emergency, because a false
    ``critical`` freezes every profile's dispatch."""
    sample = {
        "mem_total_kib": EIGHT_GIB_TOTAL_KIB,
        "mem_available_kib": 40 * 1024,
        "mem_available_estimated": True,
    }
    assert _memory_pressure_level(sample) == "unknown"


# ---------------------------------------------------------------------------
# Authoritative (Linux /proc/meminfo) readings are never floored
# ---------------------------------------------------------------------------


def test_authoritative_low_reading_stays_critical() -> None:
    """Linux ``MemAvailable`` at 1% of total is a REAL emergency.

    A generic implausibility floor must not override it: flooring an
    authoritative reading would keep spawning workers into an OOM.
    """
    for fraction in (0.005, 0.01, 0.015, 0.019):
        sample = {
            "mem_total_kib": EIGHT_GIB_TOTAL_KIB,
            "mem_available_kib": int(EIGHT_GIB_TOTAL_KIB * fraction),
        }
        assert _memory_pressure_level(sample) == "critical", (
            f"{fraction:.1%} of total available must classify critical, not unknown"
        )


def test_authoritative_absolute_threshold_stays_critical() -> None:
    """32 MiB available is under the 64 MiB absolute floor regardless of fraction."""
    sample = {"mem_total_kib": EIGHT_GIB_TOTAL_KIB, "mem_available_kib": 32 * 1024}
    assert _memory_pressure_level(sample) == "critical"


def test_authoritative_and_estimated_disagree_below_the_floor() -> None:
    """The same near-zero figure: trusted source => critical, estimate => unknown."""
    avail = 40 * 1024
    trusted = {"mem_total_kib": EIGHT_GIB_TOTAL_KIB, "mem_available_kib": avail}
    estimate = {**trusted, "mem_available_estimated": True}
    assert _memory_pressure_level(trusted) == "critical"
    assert _memory_pressure_level(estimate) == "unknown"


def test_estimated_reading_above_the_floor_is_still_classified() -> None:
    """The floor must not blind the guard for an ordinary estimated reading."""
    healthy = {
        "mem_total_kib": EIGHT_GIB_TOTAL_KIB,
        "mem_available_kib": int(EIGHT_GIB_TOTAL_KIB * 0.18),
        "mem_available_estimated": True,
    }
    assert _memory_pressure_level(healthy) == "ok"
    tight = {
        "mem_total_kib": EIGHT_GIB_TOTAL_KIB,
        "mem_available_kib": int(EIGHT_GIB_TOTAL_KIB * 0.03),
        "mem_available_estimated": True,
    }
    assert _memory_pressure_level(tight) == "critical"
