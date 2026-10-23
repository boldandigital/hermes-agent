"""Darwin memory sampling (vm_stat) — pure-function tests, no live vm_stat needed.

The macOS sampler must report the RECLAIMABLE set as available, because
macOS has no ``MemAvailable``: ``inactive`` pages are file-backed and are
reclaimed under pressure.  Counting only free+speculative+purgeable reports
~65-97 MiB on an 8 GiB host (0.8-1.2%), which ``classify_pressure`` reads as
``critical`` — the condition that would freeze dispatch for every profile.

The arithmetic is pure so no test needs a live ``vm_stat`` and none of them
depend on the host's current memory state.
"""

from __future__ import annotations

import pytest

from gateway.lifecycle_ledger import (
    darwin_available_kib,
    parse_sysctl_memsize,
    parse_vm_stat,
)
from gateway.memory_status import classify_pressure

PAGE_SIZE = 16384  # vm_stat page size on the test host (Darwin/arm64)


def _vm_stat(**overrides: int) -> dict[str, int]:
    """A representative Darwin page-count map (values from an 8 GiB mini)."""
    base = {
        "free": 3904,
        "speculative": 1075,
        "purgeable": 2,
        "inactive": 99318,
        "active": 101564,
        "wired down": 138294,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# parse_vm_stat
# ---------------------------------------------------------------------------


def test_parse_vm_stat_reads_page_size_and_counts() -> None:
    text = (
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        "Pages free:                                     3904.\n"
        "Pages active:                                   101564.\n"
        "Pages inactive:                                 99318.\n"
        "Pages speculative:                              1075.\n"
        "Pages purgeable:                                2.\n"
    )
    page_size, counts = parse_vm_stat(text)
    assert page_size == 16384
    assert counts["free"] == 3904
    assert counts["inactive"] == 99318
    assert counts["purgeable"] == 2


def test_parse_vm_stat_handles_multiword_counters() -> None:
    """Darwin emits names with spaces ("wired down", "occupied by compressor")."""
    text = (
        "Mach Virtual Memory Statistics: (page size of 4096 bytes)\n"
        "Pages wired down:                               138294.\n"
        "Pages occupied by compressor:                   155055.\n"
    )
    page_size, counts = parse_vm_stat(text)
    assert page_size == 4096
    assert counts["wired down"] == 138294
    assert counts["occupied by compressor"] == 155055


def test_parse_vm_stat_on_garbage_returns_defaults() -> None:
    """A malformed vm_stat must not raise — it must degrade to an empty map."""
    page_size, counts = parse_vm_stat("not a vm_stat output at all\n")
    assert counts == {}
    assert page_size == 4096  # the safe default


# ---------------------------------------------------------------------------
# darwin_available_kib
# ---------------------------------------------------------------------------


def test_darwin_available_includes_reclaimable_inactive() -> None:
    """macOS ``inactive`` is file-backed and reclaimable; excluding it is what
    made this host read 65-97 MiB (critical) and would freeze every dispatcher."""
    kib = darwin_available_kib(_vm_stat(), page_size=PAGE_SIZE)
    assert kib == (3904 + 1075 + 2 + 99318) * PAGE_SIZE // 1024


def test_darwin_available_excludes_nonreclaimable_pages() -> None:
    """active + wired are NOT reclaimable and must not count as available."""
    base = darwin_available_kib(_vm_stat(), page_size=PAGE_SIZE)
    bloated = darwin_available_kib(
        _vm_stat(active=999999, **{"wired down": 999999}), page_size=PAGE_SIZE
    )
    assert bloated == base


def test_darwin_available_never_reports_critical_on_this_host() -> None:
    """Regression for the freeze: an 8 GiB host with a real Darwin sample must
    classify as ``ok``, never ``critical``."""
    total_kib = 8 * 1024 * 1024
    avail = darwin_available_kib(_vm_stat(), page_size=PAGE_SIZE)
    assert classify_pressure(avail, total_kib) == "ok"


def test_darwin_available_is_zero_for_empty_sample() -> None:
    assert darwin_available_kib({}, page_size=PAGE_SIZE) == 0


# ---------------------------------------------------------------------------
# parse_sysctl_memsize
# ---------------------------------------------------------------------------


def test_parse_sysctl_memsize() -> None:
    assert parse_sysctl_memsize("8589934592") == 8 * 1024 * 1024  # KiB
    assert parse_sysctl_memsize(" 8589934592 \n") == 8 * 1024 * 1024


def test_parse_sysctl_memsize_on_garbage_is_zero() -> None:
    assert parse_sysctl_memsize("") == 0
    assert parse_sysctl_memsize("not a number") == 0
    assert parse_sysctl_memsize(None) == 0


# ---------------------------------------------------------------------------
# The real sampler on this host
# ---------------------------------------------------------------------------


@pytest.mark.platforms("macos")
def test_sample_memory_reports_keys_on_macos() -> None:
    """The live sampler must populate the two keys the dashboard and the
    dispatch guard read."""
    from gateway.lifecycle_ledger import sample_memory

    sample = sample_memory()
    assert sample.get("mem_total_kib", 0) > 0
    assert "mem_available_kib" in sample
    assert classify_pressure(
        sample.get("mem_available_kib"), sample.get("mem_total_kib")
    ) in ("ok", "elevated")
