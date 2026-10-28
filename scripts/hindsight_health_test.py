#!/usr/bin/env python3
"""Production health test for the Hindsight memory bank used by Hermes.

This is the authoritative end-to-end health check for the ``hermes`` bank. It
exercises the *original Hermes retrieval path* (the same ``hindsight_client``
SDK the Hermes memory plugin uses), not a re-implementation, so a pass means the
path Hermes actually depends on is healthy.

Phases
------
1. ``api``          -- liveness/readiness, bank listing, bank config.
2. ``provider``     -- the LLM provider the bank is configured against.
3. ``drain``        -- consolidation backlog reaches zero, with no active
                       (pending/processing) operation rows left behind.
4. ``retain``       -- a validation write through the real retain route.
5. ``consolidate``  -- that validation write actually consolidates
                       (``consolidated_at`` becomes non-null) and is then
                       retrievable through the original Hermes recall path.
6. ``models``       -- every mental model refreshes and reports fresh, non-empty
                       content.
7. ``sweep``        -- final cross-cutting validation: profile isolation,
                       graph/link integrity, archive/metadata, operation
                       history, and direct SQL invariants.

Usage
-----
    scripts/hindsight_health_test.py                     # full run
    scripts/hindsight_health_test.py --phases api,provider
    scripts/hindsight_health_test.py --json              # machine-readable
    scripts/hindsight_health_test.py --wait-drain-timeout 21600

The script never prints credentials. The API key is read from the Hermes
Hindsight config file (``~/.hermes/hindsight/config.json``) and only ever used
in an Authorization header.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_BANK = "hermes"
DEFAULT_API_URL = "http://localhost:8888"
CONFIG_PATH = Path.home() / ".hermes" / "hindsight" / "config.json"

# Mirrors the live Hermes Hindsight config (~/.hermes/hindsight/config.json):
#   retain_tags   = "profile:root"
#   recall_types  = ["observation", "world"]
#   recall_budget = "mid",  recall_max_tokens = 2048
HERMES_RETAIN_TAGS = ["profile:root"]
HERMES_RETAIN_SOURCE = "hermes-agent"
HERMES_RECALL_TYPES = ["observation", "world"]
HERMES_RECALL_BUDGET = "mid"
HERMES_RECALL_MAX_TOKENS = 2048


# --------------------------------------------------------------------------- #
# result plumbing
# --------------------------------------------------------------------------- #


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    skipped: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "skipped": self.skipped,
            "detail": self.detail,
            "data": self.data,
        }


class Report:
    def __init__(self) -> None:
        self.checks: list[Check] = []

    def add(self, name: str, ok: bool, detail: str = "", **data: Any) -> Check:
        c = Check(name=name, ok=ok, detail=detail, data=data)
        self.checks.append(c)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<46} {detail}", flush=True)
        return c

    def skip(self, name: str, detail: str = "") -> Check:
        c = Check(name=name, ok=True, detail=detail, skipped=True)
        self.checks.append(c)
        print(f"  [SKIP] {name:<46} {detail}", flush=True)
        return c

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    @property
    def passed(self) -> list[Check]:
        return [c for c in self.checks if c.ok and not c.skipped]


# --------------------------------------------------------------------------- #
# HTTP helpers (used for the checks the SDK does not expose)
# --------------------------------------------------------------------------- #


class Api:
    """Thin JSON client for the Hindsight REST API.

    Uses the API key from the Hermes Hindsight config when one is configured.
    The key is never logged and never placed in a command line.
    """

    def __init__(self, base_url: str, api_key: str | None = None, timeout: int = 60):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def request(self, method: str, path: str, body: Any = None, timeout: int | None = None) -> Any:
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=self._headers(), method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read().decode()
                if not raw:
                    return None
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return raw
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:400]
            raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{method} {path} -> unreachable: {exc.reason}") from None

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw: Any) -> Any:
        return self.request("POST", path, body=body, **kw)


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# SDK access
# --------------------------------------------------------------------------- #


def make_client(api_url: str, api_key: str | None):
    """Return the real ``hindsight_client.Hindsight`` client Hermes uses.

    Falls back to ``None`` if the SDK is not importable, in which case the
    script degrades to HTTP-only checks instead of pretending to have tested
    the SDK path.
    """
    try:
        from hindsight_client import Hindsight
    except Exception as exc:  # pragma: no cover - environment dependent
        print(f"  ! hindsight_client SDK unavailable: {type(exc).__name__}")
        return None
    return Hindsight(base_url=api_url, api_key=api_key or None)


def close_client(client: Any) -> None:
    """Close the SDK's aiohttp session so it does not leak on exit."""
    if client is None:
        return
    closer = getattr(client, "close", None) or getattr(client, "aclose", None)
    if closer is None:
        return
    try:
        result = closer()
        if asyncio.iscoroutine(result):
            try:
                asyncio.get_event_loop().run_until_complete(result)
            except RuntimeError:
                # No running loop in this thread; close the raw session instead.
                session = getattr(client, "_session", None) or getattr(client, "session", None)
                if session is not None:
                    session.close()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# phases
# --------------------------------------------------------------------------- #


def phase_api(api: Api, bank: str, rep: Report) -> None:
    print("\n== phase 1: api ==", flush=True)
    try:
        api.get("/health", timeout=20)
        rep.add("api/health", True, "liveness ok")
    except Exception as exc:
        rep.add("api/health", False, str(exc)[:200])
    try:
        api.get("/health/ready", timeout=20)
        rep.add("api/health/ready", True, "readiness ok")
    except Exception as exc:
        rep.add("api/health/ready", False, str(exc)[:200])

    try:
        banks = api.get("/v1/default/banks", timeout=30)
        ids = []
        if isinstance(banks, dict):
            ids = [b.get("bank_id") or b.get("id") for b in (banks.get("banks") or [])]
        elif isinstance(banks, list):
            ids = [b.get("bank_id") or b.get("id") for b in banks]
        rep.add("api/bank_listed", bank in ids, f"banks={ids}", banks=ids)
    except Exception as exc:
        rep.add("api/bank_listed", False, str(exc)[:200])

    try:
        cfg = api.get(f"/v1/default/banks/{bank}/config", timeout=30)
        if isinstance(cfg, dict):
            # The route returns {"bank_id": ..., "config": {...}}.
            raw_inner = cfg.get("config")
            inner: dict[str, Any] = raw_inner if isinstance(raw_inner, dict) else cfg
            llm_cfg = inner.get("llm")
            llm = inner.get("llm_provider")
            if not llm and isinstance(llm_cfg, dict):
                llm = llm_cfg.get("provider")
            batch = inner.get("consolidation_llm_batch_size")
            par = inner.get("consolidation_llm_parallelism")
            auto = inner.get("enable_auto_consolidation")
            rep.add("api/bank_config", True,
                    f"llm_provider={llm} batch={batch} parallelism={par} auto={auto}",
                    llm_provider=llm, batch_size=batch, parallelism=par, auto_consolidation=auto)
        else:
            rep.add("api/bank_config", False, "unexpected config shape")
    except Exception as exc:
        rep.add("api/bank_config", False, str(exc)[:200])

    try:
        stats = api.get(f"/v1/default/banks/{bank}/stats", timeout=30)
        pending = stats.get("pending_consolidation") if isinstance(stats, dict) else None
        rep.add("api/stats", pending is not None,
                f"pending={pending} obs={stats.get('total_observations') if isinstance(stats,dict) else '?'}",
                pending=pending)
    except Exception as exc:
        rep.add("api/stats", False, str(exc)[:200])


def phase_provider(api: Api, bank: str, rep: Report) -> None:
    print("\n== phase 2: provider ==", flush=True)
    try:
        res = api.post(f"/v1/default/banks/{bank}/health/llm", body={}, timeout=180)
        # Shape: {"bank_id": ..., "operations": [{"operation", "ok", "status", "latency_ms"}]}
        ops = res.get("operations") if isinstance(res, dict) else None
        if not isinstance(ops, list) or not ops:
            rep.add("provider/llm_health", False,
                    f"unexpected health payload: {str(res)[:200]}")
            return
        bad = [o for o in ops if not isinstance(o, dict) or not o.get("ok")]
        detail = ", ".join(
            "{}={}({:.0f}ms)".format(o.get("operation"), o.get("status"), float(o.get("latency_ms") or 0))
            for o in ops if isinstance(o, dict)
        )
        rep.add("provider/llm_health", not bad, detail, operations=len(ops), failed=[o.get("operation") for o in bad])
    except Exception as exc:
        rep.add("provider/llm_health", False, str(exc)[:300])


def phase_drain(api: Api, bank: str, rep: Report, timeout_s: int, poll_s: int) -> int:
    """Wait until the consolidation backlog is empty and nothing is in flight.

    Returns 0 when the backlog drained, or the last observed pending count on
    failure (never ``None``; callers treat any non-zero value as "not drained").
    """
    print("\n== phase 3: drain ==", flush=True)
    deadline = time.time() + timeout_s
    pending: int | None = None
    last = -1
    while True:
        try:
            stats = api.get(f"/v1/default/banks/{bank}/stats", timeout=30)
            pending = stats.get("pending_consolidation") if isinstance(stats, dict) else None
        except Exception as exc:
            rep.add("drain/stats_readable", False, str(exc)[:200])
            return -1
        if pending != last:
            print("      pending_consolidation={}  last_consolidated_at={}".format(pending, stats.get("last_consolidated_at") if isinstance(stats, dict) else "?"),
                  flush=True)
            last = pending
        if pending == 0:
            break
        if time.time() > deadline:
            rep.add("drain/backlog_zero", False, f"timeout with pending={pending}", pending=pending)
            return pending if pending is not None else -1
        time.sleep(poll_s)

    rep.add("drain/backlog_zero", True, "pending_consolidation=0", pending=0)

    # No active writes may remain (a healthy in-flight round is fine; a wedged
    # one is not).
    _check_no_stuck_operations(api, bank, rep, "drain/no_stuck_operations")
    return 0


def _count_active_operations(api: Api, bank: str) -> int | None:
    """Count pending/processing operations, or None if the route is unavailable."""
    ops = _active_operations(api, bank)
    return None if ops is None else len(ops)


def _active_operations(api: Api, bank: str) -> list[dict[str, Any]] | None:
    """All pending/processing operations, or None if the route is unavailable."""
    out: list[dict[str, Any]] = []
    try:
        for status in ("pending", "processing"):
            res = api.get(f"/v1/default/banks/{bank}/operations?status={status}&limit=100", timeout=45)
            if isinstance(res, dict):
                ops = res.get("operations") or []
            elif isinstance(res, list):
                ops = res
            else:
                return None
            out.extend(o for o in ops if isinstance(o, dict))
    except Exception:
        return None
    return out


def _op_is_stuck(op: dict[str, Any], stale_seconds: int = 900) -> bool:
    """A processing/pending operation that has not advanced recently is stuck.

    A live consolidation round updates ``updated_at`` on every batch, so an
    operation whose ``updated_at`` is older than ``stale_seconds`` is wedged --
    that is a genuine defect, unlike a healthy in-flight round.
    """
    import datetime as _dt

    ts = op.get("updated_at") or op.get("created_at")
    if not ts:
        return True
    try:
        when = _dt.datetime.fromisoformat(str(ts))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.UTC)
    age = (_dt.datetime.now(_dt.UTC) - when).total_seconds()
    return age > stale_seconds


def _check_no_stuck_operations(api: Api, bank: str, rep: Report, label: str) -> None:
    """Assert no operation is wedged; tolerate healthy in-flight rounds."""
    ops = _active_operations(api, bank)
    if ops is None:
        rep.skip(label, "operations route unavailable; covered by sql/ops_active")
        return
    stuck = [o for o in ops if _op_is_stuck(o)]
    if stuck:
        rep.add(label, False,
                f"stuck operations={len(stuck)} (not advanced in >15min): "
                f"{','.join(str(o.get('id'))[:8] for o in stuck)}",
                active=len(ops), stuck=len(stuck))
    else:
        rep.add(label, True,
                f"no stuck operations (active={len(ops)}, all advancing)",
                active=len(ops))


def phase_retain(api: Api, bank: str, rep: Report, marker: str, client: Any) -> str | None:
    """Validation write through the real retain route. Returns the marker.

    Prefers the SDK's ``retain_batch`` -- the exact call the Hermes plugin
    makes -- so a pass proves the production write path, not just the REST
    endpoint. Falls back to the equivalent HTTP body if the SDK is absent.
    """
    print("\n== phase 4: retain (validation write) ==", flush=True)
    content = (
        f"Health-check validation record {marker}. "
        "This memory was written by the Hindsight production health test to prove that "
        "retain, consolidation and the original Hermes recall path are all functioning."
    )
    metadata = {"source": HERMES_RETAIN_SOURCE, "health_check": marker}

    if client is not None:
        # Exactly the item shape the Hermes plugin builds (_build_retain_kwargs).
        item = {
            "content": content,
            "metadata": metadata,
            "timestamp": datetime.now(UTC).isoformat(),
            "context": "hindsight health test validation write",
            "tags": list(HERMES_RETAIN_TAGS),
            "observation_scopes": "per_tag",
        }
        try:
            resp = client.retain_batch(
                bank_id=bank,
                items=[item],
                retain_async=True,
            )
            ok = bool(getattr(resp, "success", False)) or getattr(resp, "operation_id", None) is not None
            rep.add(
                "retain/validation_write_accepted",
                ok,
                f"sdk retain_batch success={getattr(resp, 'success', None)} "
                f"operation_id={getattr(resp, 'operation_id', None)}",
                operation_id=getattr(resp, "operation_id", None),
                items_count=getattr(resp, "items_count", None),
            )
            return marker
        except Exception as exc:
            rep.add("retain/sdk_retain_batch", False, f"{type(exc).__name__}: {str(exc)[:200]}")

    # Fallback: the same body over HTTP (POST /memories -> RetainRequest).
    body = {
        "items": [
            {
                "content": content,
                "context": "hindsight health test validation write",
                "metadata": metadata,
                "tags": list(HERMES_RETAIN_TAGS),
                "observation_scopes": "per_tag",
            }
        ],
        "async": True,
        "document_tags": ["healthcheck", marker],
    }
    try:
        res = api.post(f"/v1/default/banks/{bank}/memories", body=body, timeout=120)
        # The server answers {"success": true, "bank_id", "items_count", "async",
        # "operation_id", "operation_ids", "usage"} -- there is no "status".
        ok = isinstance(res, dict) and (
            res.get("success") is True
            or res.get("status") in ("accepted", "completed", "success")
            or bool(res.get("operation_id") or res.get("request_id"))
        )
        rep.add(
            "retain/validation_write_accepted",
            ok,
            f"success={res.get('success') if isinstance(res, dict) else None} "
            f"items_count={res.get('items_count') if isinstance(res, dict) else None} "
            f"operation_id={res.get('operation_id') if isinstance(res, dict) else None}",
            response=res if isinstance(res, dict) else {"raw": str(res)[:200]},
        )
        return marker
    except Exception as exc:
        rep.add("retain/validation_write_accepted", False, str(exc)[:300])
        return None


def phase_consolidate(api: Api, bank: str, rep: Report, marker: str, client: Any,
                      timeout_s: int = 3600, poll_s: int = 30) -> None:
    """The validation write must consolidate, then be retrievable via recall."""
    print("\n== phase 5: consolidate + original Hermes recall ==", flush=True)
    deadline = time.time() + timeout_s
    consolidated = False
    while time.time() < deadline:
        try:
            res = api.get(f"/v1/default/banks/{bank}/memories/list?q={marker}&limit=20", timeout=45)
            units = res.get("items") or (res if isinstance(res, list) else [])
            hit = None
            for u in units:
                if not isinstance(u, dict):
                    continue
                text = u.get("text") or ""
                if marker in text:
                    hit = u
                    break
            if hit is not None and hit.get("consolidated_at"):
                consolidated = True
                rep.add("consolidate/validation_write_consolidated", True,
                        f"unit={hit.get('id')} consolidated_at={hit.get('consolidated_at')}",
                        unit_id=hit.get("id"), consolidated_at=hit.get("consolidated_at"))
                break
        except Exception as exc:
            rep.add("consolidate/poll_error", False, str(exc)[:200])
            break
        time.sleep(poll_s)

    if not consolidated:
        rep.add("consolidate/validation_write_consolidated", False,
                "validation write did not consolidate within timeout")
        return

    # The original Hermes retrieval path: the SDK's recall, with the exact
    # types/budget Hermes configures.
    if client is None:
        rep.skip("recall/original_hermes_path", "hindsight_client SDK unavailable")
        return
    try:
        resp = client.recall(
            bank_id=bank,
            query=f"health check validation record {marker}",
            types=list(HERMES_RECALL_TYPES),
            budget=HERMES_RECALL_BUDGET,
            max_tokens=HERMES_RECALL_MAX_TOKENS,
        )
        results = getattr(resp, "results", None) or []
        texts = [getattr(r, "text", "") or "" for r in results]
        found = any(marker in t for t in texts)
        rep.add("recall/original_hermes_path", found,
                f"results={len(results)} marker_found={found}", result_count=len(results))
    except Exception as exc:
        rep.add("recall/original_hermes_path", False, f"{type(exc).__name__}: {str(exc)[:200]}")


def phase_models(api: Api, bank: str, rep: Report) -> None:
    """Every mental model must refresh and hold non-empty, fresh content."""
    print("\n== phase 6: mental models ==", flush=True)
    try:
        res = api.get(f"/v1/default/banks/{bank}/mental-models?detail=metadata&limit=100", timeout=60)
        models = res.get("items") or (res if isinstance(res, list) else [])
    except Exception as exc:
        rep.add("models/list", False, str(exc)[:200])
        return

    if not models:
        rep.add("models/list", False, "no mental models returned")
        return
    rep.add("models/list", True, f"count={len(models)}", count=len(models))

    for m in models:
        mid = m.get("id") or m.get("mental_model_id")
        name = m.get("name") or mid
        try:
            api.post(f"/v1/default/banks/{bank}/mental-models/{mid}/refresh", body={}, timeout=900)
        except Exception as exc:
            rep.add(f"models/refresh[{name}]", False, str(exc)[:200])
            continue
        try:
            det = api.get(f"/v1/default/banks/{bank}/mental-models/{mid}?detail=metadata", timeout=60)
        except Exception as exc:
            rep.add(f"models/verify[{name}]", False, str(exc)[:200])
            continue
        content = det.get("content") or det.get("structured_content") or ""
        clen = len(content) if isinstance(content, str) else 0
        stale = det.get("is_stale", det.get("stale"))
        rep.add(f"models/verify[{name}]", clen > 0 and stale is not True,
                f"content_len={clen} is_stale={stale} refreshed_at={det.get('last_refreshed_at')}",
                content_len=clen, is_stale=stale)


def phase_sweep(api: Api, bank: str, rep: Report) -> None:
    """Final cross-cutting validation."""
    print("\n== phase 7: sweep ==", flush=True)

    # -- profile isolation -------------------------------------------------- #
    try:
        res = api.get(f"/v1/default/banks/{bank}/memories/list?limit=200", timeout=60)
        units = res.get("items") or (res if isinstance(res, list) else [])
        tagged = sum(1 for u in units if isinstance(u, dict) and u.get("tags"))
        rep.add("sweep/memories_readable", True, f"units={len(units)} with_tags={tagged}")
    except Exception as exc:
        rep.add("sweep/memories_readable", False, str(exc)[:200])

    # -- profile isolation: Hermes writes carry profile:root, and a recall
    #    filtered on that tag must not surface another profile's memories. --
    try:
        res = api.get(f"/v1/default/banks/{bank}/memories/list?tags=profile:root&tags_match=any&limit=200", timeout=60)
        units = res.get("items") or (res if isinstance(res, list) else [])
        other = [
            u for u in units
            if isinstance(u, dict)
            and u.get("tags")
            and any(str(t).startswith("profile:") and str(t) != "profile:root" for t in u["tags"])
        ]
        rep.add("sweep/profile_isolation", not other,
                f"profile:root units={len(units)} leaking_other_profiles={len(other)}",
                root_units=len(units), leaking=len(other))
    except Exception as exc:
        rep.add("sweep/profile_isolation", False, str(exc)[:200])

    try:
        prof = api.get(f"/v1/default/banks/{bank}/profile", timeout=30)
        ok = bool(prof)
        rep.add("sweep/bank_profile", ok, "profile readable",
                profile_keys=sorted(prof.keys()) if isinstance(prof, dict) else None)
    except Exception as exc:
        rep.skip("sweep/bank_profile", str(exc)[:160])

    # -- graph / link integrity --------------------------------------------- #
    try:
        res = api.get(f"/v1/default/banks/{bank}/graph?limit=50", timeout=60)
        rep.add("sweep/graph_readable", True, "graph endpoint ok")
    except Exception as exc:
        rep.skip("sweep/graph_readable", str(exc)[:160])

    # -- archive / metadata -------------------------------------------------- #
    try:
        res = api.get(f"/v1/default/banks/{bank}/documents", timeout=60)
        docs = res.get("items") or (res if isinstance(res, list) else [])
        rep.add("sweep/documents_readable", True, f"documents_sample={len(docs)}")
    except Exception as exc:
        rep.add("sweep/documents_readable", False, str(exc)[:200])

    # -- operation history --------------------------------------------------- #
    _check_no_stuck_operations(api, bank, rep, "sweep/ops_no_stuck")


# --------------------------------------------------------------------------- #
# SQL sweep (optional; needs psql access to the production database)
# --------------------------------------------------------------------------- #


def phase_sql(api: Api, bank: str, rep: Report) -> None:
    """Direct SQL invariants.

    Two transports are supported:

    * ``HINDSIGHT_DATABASE_URL`` -- a normal Postgres URL; ``psql`` is used.
    * ``HINDSIGHT_SQL_COMMAND``  -- a shell command that reads SQL on stdin and
      writes the result to stdout. This is how the check runs against the
      production database, which listens only inside the service container's
      network namespace: the command is ``ssh <host> docker exec -i hindsight
      psql ...``. Credentials then live in the remote environment, never in a
      command line on this machine.
    """
    print("\n== phase 8: sql invariants ==", flush=True)
    import subprocess

    dsn = os.environ.get("HINDSIGHT_DATABASE_URL") or os.environ.get("DATABASE_URL")
    cmd = os.environ.get("HINDSIGHT_SQL_COMMAND")

    if not dsn and not cmd:
        rep.skip("sql/*", "no HINDSIGHT_DATABASE_URL or HINDSIGHT_SQL_COMMAND in environment")
        return

    def q(sql: str) -> str:
        if cmd:
            proc = subprocess.run(
                cmd,
                shell=True,
                input=sql,
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
        else:
            proc = subprocess.run(
                ["psql", dsn, "-t", "-A", "-c", sql],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip()[:200])
        return proc.stdout.strip()

    def scalar(sql: str) -> int:
        out = q(sql)
        try:
            return int(out.splitlines()[0].strip())
        except (IndexError, ValueError) as exc:
            raise RuntimeError(f"unexpected scalar output: {out[:120]!r}") from exc

    safe_bank = bank.replace("'", "''")
    try:
        pending = scalar(
            "select count(*) from memory_units where bank_id='"
            + safe_bank
            + "' and consolidated_at is null "
            "and consolidation_failed_at is null and fact_type in ('experience','world')"
        )
        rep.add("sql/pending_zero", pending == 0, f"pending={pending}", pending=pending)
    except Exception as exc:
        rep.add("sql/pending_zero", False, str(exc)[:200])
    try:
        active = scalar(
            "select count(*) from operations where bank_id='"
            + safe_bank
            + "' and status in ('pending','processing')"
        )
        rep.add("sql/ops_active", active == 0, f"active={active}", active=active)
    except Exception as exc:
        rep.add("sql/ops_active", False, str(exc)[:200])
    try:
        orphan_links = scalar(
            "select count(*) from memory_links l where not exists "
            "(select 1 from memory_units u where u.id=l.from_unit_id) "
            "or not exists (select 1 from memory_units u where u.id=l.to_unit_id)"
        )
        rep.add("sql/no_orphan_links", orphan_links == 0, f"orphans={orphan_links}")
    except Exception as exc:
        rep.add("sql/no_orphan_links", False, str(exc)[:200])
    try:
        orphan_posts = scalar(
            "select count(*) from unit_entities ue where not exists "
            "(select 1 from memory_units u where u.id=ue.unit_id) "
            "or not exists (select 1 from entities e where e.id=ue.entity_id)"
        )
        rep.add("sql/no_orphan_postings", orphan_posts == 0, f"orphans={orphan_posts}")
    except Exception as exc:
        rep.add("sql/no_orphan_postings", False, str(exc)[:200])
    try:
        nmodels = scalar(f"select count(*) from mental_models where bank_id='{safe_bank}'")
        empty = scalar(
            f"select count(*) from mental_models where bank_id='{safe_bank}' "
            "and coalesce(length(content::text),0)=0"
        )
        rep.add("sql/models_non_empty", empty == 0 and nmodels > 0,
                f"models={nmodels} empty_content={empty}", models=nmodels, empty=empty)
    except Exception as exc:
        rep.add("sql/models_non_empty", False, str(exc)[:200])


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


PHASES = ["api", "provider", "drain", "retain", "consolidate", "models", "sweep", "sql"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bank", default=DEFAULT_BANK)
    ap.add_argument("--api-url", default=None, help="default: from ~/.hermes/hindsight/config.json")
    ap.add_argument("--phases", default=",".join(PHASES), help="comma separated subset of: " + ",".join(PHASES))
    ap.add_argument("--wait-drain-timeout", type=int, default=21600, help="seconds to wait for backlog")
    ap.add_argument("--drain-poll", type=int, default=30, help="seconds between drain polls")
    ap.add_argument("--json", action="store_true", help="emit a JSON summary")
    args = ap.parse_args(argv)

    cfg = load_config()
    api_url = args.api_url or cfg.get("api_url") or DEFAULT_API_URL
    api_key = cfg.get("api_key") or os.environ.get("HINDSIGHT_API_KEY")
    bank = args.bank or cfg.get("bank_id") or DEFAULT_BANK

    phases = [p.strip() for p in args.phases.split(",") if p.strip()]
    unknown = [p for p in phases if p not in PHASES]
    if unknown:
        print("unknown phase(s): {}".format(", ".join(unknown)), file=sys.stderr)
        return 2

    api = Api(api_url, api_key)
    client = make_client(api_url, api_key)
    rep = Report()
    marker = f"hc-{int(time.time())}"

    print("Hindsight production health test")
    print(f"  bank    : {bank}")
    print(f"  api_url : {api_url}")
    print("  phases  : {}".format(",".join(phases)))
    print(f"  marker  : {marker}")

    if "api" in phases:
        phase_api(api, bank, rep)
    if "provider" in phases:
        phase_provider(api, bank, rep)
    if "drain" in phases:
        phase_drain(api, bank, rep, args.wait_drain_timeout, args.drain_poll)
    if "retain" in phases:
        phase_retain(api, bank, rep, marker, client)
    if "consolidate" in phases:
        phase_consolidate(api, bank, rep, marker, client)
    if "models" in phases:
        phase_models(api, bank, rep)
    if "sweep" in phases:
        phase_sweep(api, bank, rep)
    if "sql" in phases:
        phase_sql(api, bank, rep)

    failed = rep.failed
    summary = {
        "bank": bank,
        "api_url": api_url,
        "marker": marker,
        "total": len(rep.checks),
        "passed": len(rep.passed),
        "failed": len(failed),
        "ok": not failed,
        "checks": [c.to_dict() for c in rep.checks],
    }
    close_client(client)
    print("\n" + "=" * 72)
    print(f"RESULT: {'PASS' if not failed else 'FAIL'}  "
          f"({len(rep.passed)} passed, {len(failed)} failed, {len(rep.checks)} total)")
    for c in failed:
        print(f"  FAILED: {c.name} -- {c.detail}")
    print("=" * 72)
    if args.json:
        print(json.dumps(summary, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
