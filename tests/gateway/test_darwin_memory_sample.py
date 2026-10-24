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
    """The live sampler must populate the keys the dashboard and the dispatch
    guard read, and mark the figure as an estimate."""
    from gateway.lifecycle_ledger import sample_memory

    sample = sample_memory()
    assert sample.get("mem_total_kib", 0) > 0
    assert "mem_available_kib" in sample
    assert sample.get("mem_available_estimated") is True
    assert classify_pressure(
        sample.get("mem_available_kib"), sample.get("mem_total_kib")
    ) in ("ok", "elevated")


# ---------------------------------------------------------------------------
# _sysctl_pagesize — the authoritative page size (D1)
# ---------------------------------------------------------------------------

# A headerless vm_stat whose reclaimable pages total ~13.4% of an 8 GiB host.
# With the true 16384-byte page size this reads elevated; with a guessed 4096
# it reads ~3.4% -> critical -> the whole fleet freezes.
_HEADERLESS_VM_STAT = (
    "Pages free:                                      1000.\n"
    "Pages speculative:                                100.\n"
    "Pages purgeable:                                    0.\n"
    "Pages inactive:                                 69300.\n"
)
_HEADERLESS_AVAILABLE_KIB = (1000 + 100 + 0 + 69300) * 16384 // 1024
_EIGHT_GIB_KIB = 8 * 1024 * 1024


class _FakeProc:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.returncode = 0


def _fake_subprocess(monkeypatch, *, vm_stat: str, pagesize: str, memsize: str = "8589934592"):
    """Replace ``lifecycle_ledger.subprocess`` with a scripted shim; returns the argv log."""
    import gateway.lifecycle_ledger as ll

    calls: list[list[str]] = []

    class _Shim:
        # _sysctl_pagesize's except clause resolves SubprocessError off this module.
        SubprocessError = __import__("subprocess").SubprocessError

        @staticmethod
        def run(argv, **_kwargs):
            calls.append(list(argv))
            joined = " ".join(argv)
            if "vm_stat" in joined:
                return _FakeProc(vm_stat)
            if "hw.pagesize" in joined:
                return _FakeProc(pagesize)
            if "hw.memsize" in joined:
                return _FakeProc(memsize)
            raise AssertionError(f"unexpected argv: {argv}")

    monkeypatch.setattr(ll, "subprocess", _Shim)
    return calls


def test_sysctl_pagesize_reads_the_os_value(monkeypatch) -> None:
    from gateway.lifecycle_ledger import _sysctl_pagesize

    _fake_subprocess(monkeypatch, vm_stat="", pagesize="16384\n")
    assert _sysctl_pagesize() == 16384


def test_sysctl_pagesize_rejects_non_power_of_two(monkeypatch) -> None:
    """A garbled read must not be trusted as a page size."""
    from gateway.lifecycle_ledger import _sysctl_pagesize

    _fake_subprocess(monkeypatch, vm_stat="", pagesize="12345\n")
    assert _sysctl_pagesize() == 0


def test_sysctl_pagesize_rejects_out_of_band(monkeypatch) -> None:
    from gateway.lifecycle_ledger import _sysctl_pagesize

    _fake_subprocess(monkeypatch, vm_stat="", pagesize="1024\n")
    assert _sysctl_pagesize() == 0


def test_sysctl_pagesize_garbage_is_zero(monkeypatch) -> None:
    from gateway.lifecycle_ledger import _sysctl_pagesize

    _fake_subprocess(monkeypatch, vm_stat="", pagesize="not a number\n")
    assert _sysctl_pagesize() == 0


# ---------------------------------------------------------------------------
# _darwin_memory_sample — page-size provenance (D1)
# ---------------------------------------------------------------------------


def test_headerless_vm_stat_still_uses_the_real_page_size(monkeypatch) -> None:
    """REGRESSION: a missing page-size header must NOT fall back to 4096.

    On Apple Silicon that fallback under-reports 4x, landing near 4% available
    -> ``critical`` -> dispatch frozen for every profile. sysctl is asked
    first, so the true 16384 is used and the host reads elevated.
    """
    from gateway.lifecycle_ledger import _darwin_memory_sample

    _fake_subprocess(monkeypatch, vm_stat=_HEADERLESS_VM_STAT, pagesize="16384\n")
    sample = _darwin_memory_sample()

    assert sample["mem_available_kib"] == _HEADERLESS_AVAILABLE_KIB
    assert sample["mem_available_estimated"] is True
    level = classify_pressure(sample["mem_available_kib"], sample["mem_total_kib"])
    assert level != "critical", f"headerless vm_stat must not read critical (got {level})"


def test_guessed_4096_would_have_read_critical() -> None:
    """The falsification: with the old 4096 fallback the same pages read critical."""
    page_size, counts = parse_vm_stat(_HEADERLESS_VM_STAT)
    assert page_size == 4096  # parse_vm_stat's documented default is unchanged
    wrong = darwin_available_kib(counts, page_size=page_size)
    right = darwin_available_kib(counts, page_size=16384)
    assert classify_pressure(wrong, _EIGHT_GIB_KIB) == "critical"
    assert classify_pressure(right, _EIGHT_GIB_KIB) != "critical"
    assert right == wrong * 4  # exactly the 4x under-report


def test_sampler_falls_back_to_header_page_size_when_sysctl_fails(monkeypatch) -> None:
    """sysctl unavailable but the header is present: use the header, don't give up."""
    from gateway.lifecycle_ledger import _darwin_memory_sample

    text = (
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        "Pages free:                                      1000.\n"
        "Pages speculative:                                100.\n"
        "Pages purgeable:                                    0.\n"
        "Pages inactive:                                 69300.\n"
    )
    _fake_subprocess(monkeypatch, vm_stat=text, pagesize="\n")
    sample = _darwin_memory_sample()

    assert sample["mem_available_kib"] == _HEADERLESS_AVAILABLE_KIB
    assert classify_pressure(sample["mem_available_kib"], sample["mem_total_kib"]) != "critical"


def test_sampler_reports_unknown_when_no_page_size_is_available(monkeypatch) -> None:
    """Neither sysctl nor the header yields a page size: report nothing, don't guess.

    An unknown sample imposes no restriction; a guessed 4096 would freeze the
    fleet. Unknown is the safe failure.
    """
    from gateway.lifecycle_ledger import _darwin_memory_sample

    _fake_subprocess(monkeypatch, vm_stat=_HEADERLESS_VM_STAT, pagesize="\n")
    assert _darwin_memory_sample() == {}


def test_sampler_reports_unknown_when_vm_stat_is_empty(monkeypatch) -> None:
    """A failed vm_stat must not report available=0.

    ``{total: 8 GiB, available: 0}`` would classify as critical AND make the
    lifecycle ledger's ``_suspected_oom`` heuristic flag a healthy host.
    """
    from gateway.lifecycle_ledger import _darwin_memory_sample

    _fake_subprocess(monkeypatch, vm_stat="", pagesize="16384\n")
    assert _darwin_memory_sample() == {}
