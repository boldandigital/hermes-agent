"""SEP-2549 schema-cache TTL expiry (tools/mcp_schema_cache.py)."""

import time

import pytest

from tools import mcp_schema_cache as sc


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "_cache_path", lambda: tmp_path / "cache.json")
    yield


def test_entry_without_ttl_never_expires():
    sc.write_cache_entry("srv", "fp", tools=[{"name": "t"}])
    assert sc.get_cached_entry("srv", "fp") is not None


def test_entry_within_ttl_served():
    sc.write_cache_entry("srv", "fp", tools=[{"name": "t"}], ttl_ms=60_000)
    entry = sc.get_cached_entry("srv", "fp")
    assert entry is not None
    assert entry["ttl_ms"] == 60_000
    assert "written_at" in entry


def test_entry_past_ttl_is_a_miss(monkeypatch):
    sc.write_cache_entry("srv", "fp", tools=[{"name": "t"}], ttl_ms=1_000)
    real_time = time.time
    monkeypatch.setattr(sc.time, "time", lambda: real_time() + 2.0)
    assert sc.get_cached_entry("srv", "fp") is None


def test_ttl_rewrite_advances_written_at():
    sc.write_cache_entry("srv", "fp", tools=[{"name": "t"}], ttl_ms=60_000)
    first = sc.get_cached_entry("srv", "fp")["written_at"]
    time.sleep(0.01)
    # Identical payload would previously short-circuit; TTL'd entries must
    # rewrite so written_at advances on every live reconfirmation.
    sc.write_cache_entry("srv", "fp", tools=[{"name": "t"}], ttl_ms=60_000)
    second = sc.get_cached_entry("srv", "fp")["written_at"]
    assert second > first


def test_cache_scope_round_trips():
    sc.write_cache_entry(
        "srv", "fp", tools=[{"name": "t"}], ttl_ms=60_000, cache_scope="private"
    )
    assert sc.get_cached_entry("srv", "fp")["cache_scope"] == "private"


# --- ttl_ms=0 (SEP-2549 hint absent, SDK default) ---------------------------
#
# The MCP SDK models ListToolsResult.ttl_ms with default 0, so a server that never
# sent a ttlMs hint yields 0. The writer used to store that 0 and the reader used to
# read it as "expired the instant it was written", so every cache entry was born
# stale and `lazy: true` silently fell back to an eager connect on every startup.


def test_zero_ttl_is_stored_as_no_ttl():
    """A synthesized ttl_ms=0 must not be persisted as a real hint."""
    sc.write_cache_entry("srv", "fp", tools=[{"name": "t"}], ttl_ms=0)
    entry = sc._load_all()["srv"]
    assert "ttl_ms" not in entry
    assert "written_at" not in entry


def test_zero_ttl_entry_is_served():
    """The whole point: a ttl_ms=0 entry must still be a cache HIT."""
    sc.write_cache_entry("srv", "fp", tools=[{"name": "t"}], ttl_ms=0)
    assert sc.get_cached_entry("srv", "fp") is not None


def test_zero_ttl_from_a_full_page_is_served():
    """`not items` in _paginate_full_list is true on the FIRST page, not only on an
    empty page, so a full tools/list page records ttl_ms=0 too. Pin that the entry
    written from that shape survives a later read."""
    sc.write_cache_entry("srv", "fp", tools=[{"name": f"t{i}"} for i in range(5)], ttl_ms=0)
    time.sleep(0.01)
    assert sc.get_cached_entry("srv", "fp") is not None


def test_legacy_zero_ttl_entry_on_disk_is_served():
    """An entry written by the buggy build already carries ttl_ms:0 + written_at on
    disk. The READ path needs its own guard, or machines that ran that build stay
    broken until the server happens to be re-probed live."""
    sc._save_all({"srv": {"fingerprint": "fp", "tools": [{"name": "t"}],
                          "utility_tools": [], "ttl_ms": 0, "written_at": time.time()}})
    assert sc.get_cached_entry("srv", "fp") is not None


def test_bool_ttl_is_not_a_one_millisecond_hint():
    """bool is an int subclass; True would otherwise store a 1ms TTL."""
    sc.write_cache_entry("srv", "fp", tools=[{"name": "t"}], ttl_ms=True)
    assert sc.get_cached_entry("srv", "fp") is not None
    assert "ttl_ms" not in sc._load_all()["srv"]


def test_zero_ttl_write_then_positive_ttl_upgrades_in_place():
    """Re-probing a server that starts advertising a real hint must start recording it."""
    sc.write_cache_entry("srv", "fp", tools=[{"name": "t"}], ttl_ms=0)
    assert "ttl_ms" not in sc._load_all()["srv"]
    sc.write_cache_entry("srv", "fp", tools=[{"name": "t"}], ttl_ms=60_000)
    entry = sc._load_all()["srv"]
    assert entry["ttl_ms"] == 60_000
    assert "written_at" in entry
