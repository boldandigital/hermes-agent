#!/usr/bin/env python3
"""MCP (Model Context Protocol) client: connects to the ``mcp_servers`` configured in
~/.hermes/config.yaml (stdio, Streamable HTTP or SSE), discovers their tools and registers them
into the hermes tool registry. The ``mcp`` package is optional (no-op without it).

One background event loop (``_mcp_loop``) in a daemon thread runs each server as a long-lived
Task (``MCPServerTask``) so the transport's anyio cancel scopes enter and exit in one Task; every
``_servers``/loop mutation holds ``_lock``. This module keeps the SDK loader, ``MCPServerTask`` and
all shared state; the ``mcp_tool_*`` siblings read that state back through ``tools.mcp_tool`` at
call time (``_core``) and are imported directly by their callers."""

import asyncio
import contextvars
import importlib
import importlib.util
import inspect
import logging
import os
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

from tools.mcp_tool_common import _DEFAULT_TOOL_TIMEOUT, mcp_field
from tools.mcp_tool_config import _get_mcp_stderr_log, _npx_cached_bin
from tools.mcp_tool_sampling import ElicitationHandler, SamplingHandler
from tools.mcp_tool_transport import MCPServerTransportMixin
from tools.mcp_tool_server_run import MCPServerRunMixin
from tools.mcp_tool_health import MCPServerHealthMixin


# Wall-clock bound on the fail-open OSV malware preflight before a stdio spawn; just ABOVE
# osv_check._TIMEOUT (10s) so it only bites when a stalled SSL handshake defeats that.
_OSV_MALWARE_CHECK_TIMEOUT_S = 12.0


async def _preflight_stdio_command(server_name: str, command: str, args: list) -> tuple[str, list]:
    """OSV malware preflight (off-loop, wall-clock bound, fail-open on timeout), THEN the
    cached-npx swap. The preflight must see the REAL command/args: anything that rewrites argv to a
    wrapper or resolved binary has to happen after it, or the check silently inspects the wrapper
    and becomes a no-op (``_infer_ecosystem`` keys off the command basename being npx/uvx/pipx)."""
    from tools.osv_check import check_package_for_malware
    try:
        malware_error = await asyncio.wait_for(
            asyncio.to_thread(check_package_for_malware, command, args), timeout=_OSV_MALWARE_CHECK_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning("MCP server '%s': OSV malware preflight timed out after %.0fs "
                       "(network slow/unreachable) — proceeding without the check.",
                       server_name, _OSV_MALWARE_CHECK_TIMEOUT_S)
        malware_error = None
    if malware_error:
        raise ValueError(f"MCP server '{server_name}': {malware_error}")

    # npx resolves the package and then FORKS, staying resident as the real server's parent for
    # nothing (~48 MB per server, measured). Hermes already supervises the child (shared death
    # supervisor), so a cached package is spawned directly; a cache miss leaves npx untouched.
    if os.path.basename(command).lower().startswith("npx"):
        cached = _npx_cached_bin(args)
        if cached:
            direct_command, direct_args = cached
            logger.debug("MCP server '%s': using cached npx binary %s (skipping the "
                         "resident `npm exec` parent)", server_name, direct_command)
            command, args = direct_command, direct_args
    return command, args


# ---- Optional MCP SDK: availability probe now, symbol import on first use ----

_MCP_AVAILABLE = _MCP_HTTP_AVAILABLE = _MCP_NEW_HTTP = _MCP_LEGACY_HTTP = False
_MCP_SAMPLING_TYPES = _MCP_NOTIFICATION_TYPES = _MCP_ELICITATION_TYPES = False
_MCP_MESSAGE_HANDLER_SUPPORTED = _MCP_LOGGING_CALLBACK_SUPPORTED = False
sse_client = None
# Fallback for SDKs without LATEST_PROTOCOL_VERSION (Streamable HTTP arrived with 2025-03-26).
LATEST_PROTOCOL_VERSION = "2025-03-26"
# Newest revision ``ClientSession.initialize()`` speaks; from 2026-07-28 the handshake is a
# per-request envelope so this can be OLDER than LATEST_PROTOCOL_VERSION, and the
# MCP-Protocol-Version header must be seeded from THIS one.
LATEST_HANDSHAKE_VERSION = LATEST_PROTOCOL_VERSION

# Importing ``mcp`` costs ~260ms, so it is deferred to first use (_ensure_mcp_sdk); availability
# is decided now via find_spec so every ``if not _MCP_AVAILABLE`` gate / patch / skipif holds.
try:
    _MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None
except Exception:
    _MCP_AVAILABLE = False
if not _MCP_AVAILABLE:
    logger.debug("mcp package not installed -- MCP tool support disabled")

ClientSession: Any = None
_MCP_SDK_IMPORT_ATTEMPTED = False
_MCP_SDK_IMPORT_LOCK = threading.Lock()

# Optional SDK type families (module, names, debug message when absent), bound in this order to
# _MCP_SAMPLING_TYPES / _MCP_ELICITATION_TYPES / _MCP_NOTIFICATION_TYPES; an older SDK only
# loses that feature, not MCP.
_OPTIONAL_TYPE_FAMILIES = (
    ("mcp.types", ("CreateMessageResult", "CreateMessageResultWithTools", "ErrorData", "SamplingCapability",
                   "SamplingToolsCapability", "TextContent", "ToolUseContent"),
     "MCP sampling types not available -- sampling disabled"),
    ("mcp.types", ("ElicitRequestParams", "ElicitResult"),
     "MCP elicitation types not available -- elicitation disabled"),
    ("mcp.types", ("ServerNotification", "ToolListChangedNotification", "PromptListChangedNotification",
                   "ResourceListChangedNotification"),
     "MCP notification types not available -- dynamic tool discovery disabled"),
)
# Bound by _ensure_mcp_sdk(); module __getattr__ (PEP 562) imports the SDK on first external
# access so mock.patch("tools.mcp_tool.stdio_client") sees a real original, never clobbered.
_MCP_SDK_LAZY_SYMBOLS = frozenset(
    {"StdioServerParameters", "stdio_client", "streamablehttp_client", "streamable_http_client"}
    | {n for _mod, names, _msg in _OPTIONAL_TYPE_FAMILIES for n in names})


def __getattr__(name: str):
    if name in _MCP_SDK_LAZY_SYMBOLS:
        _ensure_mcp_sdk()
        try:
            return globals()[name]
        except KeyError:
            pass  # SDK missing or symbol absent on this SDK build
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _import_sdk_names(module: str, names: tuple, missing_msg: Optional[str] = None) -> bool:
    """Bind ``names`` from SDK ``module`` into this module's globals; False (nothing bound,
    optional debug line) when this SDK build lacks the module or any of the names."""
    try:
        mod = importlib.import_module(module)
        values = {n: getattr(mod, n) for n in names}
    except (ImportError, AttributeError):
        if missing_msg:
            logger.debug(missing_msg)
        return False
    globals().update(values)
    return True


def _ensure_mcp_sdk() -> bool:
    """Import the optional ``mcp`` SDK on first use; return availability. Idempotent and
    thread-safe; honors a test-patched ``_MCP_AVAILABLE=False`` (no import) and pre-installed
    mocks (``ClientSession`` already set means no re-import)."""
    global _MCP_SDK_IMPORT_ATTEMPTED, _MCP_AVAILABLE, _MCP_HTTP_AVAILABLE, _MCP_NEW_HTTP, _MCP_LEGACY_HTTP
    global _MCP_SAMPLING_TYPES, _MCP_NOTIFICATION_TYPES, _MCP_ELICITATION_TYPES, sse_client
    global _MCP_MESSAGE_HANDLER_SUPPORTED, _MCP_LOGGING_CALLBACK_SUPPORTED, LATEST_HANDSHAKE_VERSION
    global _JSONRPC_METHOD_NOT_FOUND
    if not _MCP_AVAILABLE:
        return False
    if _MCP_SDK_IMPORT_ATTEMPTED or ClientSession is not None:
        return _MCP_AVAILABLE
    with _MCP_SDK_IMPORT_LOCK:
        if _MCP_SDK_IMPORT_ATTEMPTED or ClientSession is not None:
            return _MCP_AVAILABLE
        if (_import_sdk_names("mcp", ("ClientSession", "StdioServerParameters"))
                and _import_sdk_names("mcp.client.stdio", ("stdio_client",))):
            _MCP_AVAILABLE = True
            # mcp >= 1.24 ships streamable_http_client; 2.0 dropped the deprecated
            # streamablehttp_client alias. Either one gives HTTP.
            _MCP_NEW_HTTP = _import_sdk_names("mcp.client.streamable_http", ("streamable_http_client",))
            _MCP_LEGACY_HTTP = _import_sdk_names("mcp.client.streamable_http", ("streamablehttp_client",))
            _MCP_HTTP_AVAILABLE = _MCP_NEW_HTTP or _MCP_LEGACY_HTTP
            _import_sdk_names("mcp.types", ("LATEST_PROTOCOL_VERSION",),
                              "mcp.types.LATEST_PROTOCOL_VERSION not available -- using fallback protocol version")
            if not _import_sdk_names("mcp.client.session", ("LATEST_HANDSHAKE_VERSION",)):
                # Pre-2.x SDKs: newest revision IS the handshake revision.
                LATEST_HANDSHAKE_VERSION = LATEST_PROTOCOL_VERSION
            if not _import_sdk_names("mcp.client.sse", ("sse_client",),
                                     "mcp.client.sse.sse_client not available -- SSE transport disabled"):
                sse_client = None
            _MCP_SAMPLING_TYPES, _MCP_ELICITATION_TYPES, _MCP_NOTIFICATION_TYPES = [
                _import_sdk_names(*family) for family in _OPTIONAL_TYPE_FAMILIES]
        else:
            logger.debug("mcp package not installed -- MCP tool support disabled")
        if _MCP_AVAILABLE:
            try:
                _JSONRPC_METHOD_NOT_FOUND = importlib.import_module("mcp.types").METHOD_NOT_FOUND
            except Exception:  # pragma: no cover — SDK without the constant
                pass
        _MCP_MESSAGE_HANDLER_SUPPORTED = _client_session_accepts("message_handler")
        if _MCP_AVAILABLE and not _MCP_MESSAGE_HANDLER_SUPPORTED:
            logger.debug("MCP SDK does not support message_handler -- dynamic tool discovery disabled")
        _MCP_LOGGING_CALLBACK_SUPPORTED = _client_session_accepts("logging_callback")
        _MCP_SDK_IMPORT_ATTEMPTED = True
        return _MCP_AVAILABLE


_SDK_HTTPX_MOD = None


def sdk_httpx():
    """The httpx module the *installed* MCP SDK is built against (mcp 2.0 moved to ``httpx2``).
    Every object crossing the SDK boundary (AsyncClient, OAuth Request, exception classes) must
    come from the module the SDK itself imports or it fails at the transport layer. Resolved
    from the SDK's transport module, else the newest present; ``None`` if neither imports."""
    global _SDK_HTTPX_MOD
    if _SDK_HTTPX_MOD is not None:
        return _SDK_HTTPX_MOD
    try:
        from mcp.client import streamable_http as _transport
        _SDK_HTTPX_MOD = getattr(_transport, "httpx2", None) or getattr(_transport, "httpx", None)
    except ImportError:
        _SDK_HTTPX_MOD = None
    for fallback in ("httpx2", "httpx"):
        if _SDK_HTTPX_MOD is not None:
            break
        try:
            _SDK_HTTPX_MOD = importlib.import_module(fallback)
        except ImportError:
            pass
    return _SDK_HTTPX_MOD


def _client_session_accepts(kwarg: str) -> bool:
    """Whether this SDK's ``ClientSession.__init__`` takes ``kwarg`` (older SDKs lack
    ``message_handler`` and ``logging_callback``)."""
    if not _MCP_AVAILABLE:
        return False
    try:
        return kwarg in inspect.signature(ClientSession).parameters
    except (TypeError, ValueError):
        return False


# MCP logging levels (RFC 5424 syslog severities) -> Python logging levels.
# Port of anomalyco/opencode#34529's serverLog mapping.
_MCP_LOG_LEVEL_MAP = {
    "debug": logging.DEBUG, "info": logging.INFO, "notice": logging.INFO,
    "warning": logging.WARNING, "error": logging.ERROR, "critical": logging.ERROR,
    "alert": logging.ERROR, "emergency": logging.ERROR}

# ---- Reconnect / keepalive tuning ----

_DEFAULT_CONNECT_TIMEOUT = 60    # seconds for initial connection per server
_MAX_RECONNECT_RETRIES = 5
_MAX_INITIAL_CONNECT_RETRIES = 3 # retries for the very first connection attempt
_MAX_BACKOFF_SECONDS = 60
# While parked (reconnect budget exhausted, tools deregistered) the run task
# wakes on this cadence and attempts one revival probe. Without it a parked
# server is unrevivable: its tools are out of the registry, so no tool call
# can ever reach the circuit-breaker half-open probe or _signal_reconnect.
_PARKED_RETRY_INTERVAL = 300     # seconds between parked self-probes (steady-state)
_PARKED_RETRY_INTERVAL_FAST = 60  # Hermes patch 2026-08-21: faster probes for fresh failures

def _adaptive_parked_interval(probe_count):
    """Hermes patch 2026-08-21: 60s/60s/60s then 180s/180s/180s then 300s."""
    if probe_count < 3:
        return _PARKED_RETRY_INTERVAL_FAST
    elif probe_count < 6:
        return 180
    return _PARKED_RETRY_INTERVAL

_RECYCLED_RECONNECT_TIMEOUT = 15.0
# Parked servers (tools deregistered) self-probe on this cadence: nothing else can revive them.
_PARKED_RETRY_INTERVAL = 300
# Bounded wait for a respawned stdio child when a call finds it dead (gateway restarts kill
# every MCP child); bounded so a broken server still parks via run()'s rapid-drop budget.
_STDIO_RESPAWN_WAIT_SEC = 15.0
# The client MUST ping faster than the server's idle-session TTL (short-TTL servers need a
# smaller configured ``keepalive_interval``); the floor stops a tiny interval busy-looping.
_DEFAULT_KEEPALIVE_INTERVAL, _MIN_KEEPALIVE_INTERVAL = 180, 5
# One bounded cancellation cycle at final shutdown so resistant tasks cannot hang exit.
_MCP_LOOP_DRAIN_TIMEOUT = 3.0
# JSON-RPC 2.0 "method not found" (server without optional ``ping``); _ensure_mcp_sdk()
# overrides it from mcp.types once loaded.
_JSONRPC_METHOD_NOT_FOUND = -32601
# nextCursor pagination cap so a forever-cursor cannot spin discovery (50 pages = thousands).
_MCP_LIST_MAX_PAGES = 50


async def _paginate_full_list(list_method, items_attr: str, server_name: str,
                              cache_meta_out: Optional[dict] = None):
    """Drain a paginated ``list_*`` call by following ``nextCursor``; ``cache_meta_out`` gets the
    first page's SEP-2549 hints. Callers must hold the server's ``_rpc_lock``."""
    items: list = []
    cursor = None
    for _ in range(_MCP_LIST_MAX_PAGES):
        if not cursor:
            result = await list_method()
        else:
            # mcp 2.0 takes params=PaginatedRequestParams, 1.x takes cursor=.
            # Inspect before awaiting: an internal TypeError is not a signature mismatch.
            import inspect

            try:
                signature = inspect.signature(list_method)
            except (TypeError, ValueError):
                accepts_params = True  # Opaque callables use the current SDK convention.
            else:
                accepts_params = any(
                    p.kind == inspect.Parameter.VAR_KEYWORD
                    or (p.name == "params" and p.kind != inspect.Parameter.POSITIONAL_ONLY)
                    for p in signature.parameters.values()
                )
            if accepts_params:
                import mcp.types as _types  # late: keeps the SDK import lazy
                _params_cls = getattr(_types, "PaginatedRequestParams", None)
                if _params_cls is not None:
                    result = await list_method(params=_params_cls(cursor=cursor))
                else:
                    result = await list_method(cursor=cursor)
            else:
                result = await list_method(cursor=cursor)
        if cache_meta_out is not None and not items:
            for key, snake, camel in (("ttl_ms", "ttl_ms", "ttlMs"), ("cache_scope", "cache_scope", "cacheScope")):
                hint = mcp_field(result, snake, camel)
                if hint is not None:
                    cache_meta_out[key] = hint
        items.extend(getattr(result, items_attr, None) or [])
        cursor = mcp_field(result, "next_cursor", "nextCursor")
        # Cursor is an opaque string; anything else (incl. mocks) = last page.
        if not isinstance(cursor, str) or not cursor:
            break
    else:
        logger.warning("MCP server '%s': %s pagination exceeded %d pages; truncating at %d items",
                       server_name, items_attr, _MCP_LIST_MAX_PAGES, len(items))
    return items


# ---- Server task -- each MCP server lives in one long-lived asyncio Task ----

class MCPServerTask(MCPServerRunMixin, MCPServerTransportMixin, MCPServerHealthMixin):
    """One MCP server connection in one long-lived asyncio Task (the transport's anyio cancel
    scopes must enter/exit in the same Task). Run state machine, transport bring-up and
    keepalive/liveness live in the three mixins."""

    __slots__ = (
        "name", "session", "tool_timeout", "_task", "_ready", "_shutdown_event", "_reconnect_event",
        "_tools", "_error", "_config", "_sampling", "_elicitation", "_registered_tool_names",
        "_auth_type", "_refresh_lock", "_rpc_lock", "_pending_refresh_tasks", "_pending_call_context",
        "_lifecycle_started_at", "_last_tool_call_at", "_idle_timeout_seconds", "_max_lifetime_seconds",
        "_recycled_reason", "initialize_result", "_ping_unsupported", "_list_cache_meta",
        "_reconnect_retries", "_session_proven", "_was_parked", "_inflight_tasks", "_reconnecting",
        "_suspect_reason", "_teardown_race", "_permanent_grace_used", "_stdio_child_pids",
        "_ever_connected")

    def __init__(self, name: str):
        self.name = name
        self.session: Optional[Any] = None
        self.tool_timeout: float = _DEFAULT_TOOL_TIMEOUT
        self._task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()
        self._shutdown_event = asyncio.Event()
        # Set -> _run_http/_run_stdio exit cleanly and run() re-enters the transport.
        self._reconnect_event = asyncio.Event()
        self._tools: list = []
        self._registered_tool_names: list[str] = []
        self._config: dict = {}
        self._error: Optional[Exception] = None
        self._sampling: Optional[SamplingHandler] = None
        self._elicitation: Optional[ElicitationHandler] = None
        self._reconnect_retries: int = 0
        # Rapid-drop budget: a session is UNPROVEN until it survives a keepalive interval or a
        # successful call; only a proven session clears the budget, so a post-handshake flapper
        # still parks.
        # Rapid-drop budget (#62212): a freshly (re)established session is UNPROVEN until it demonstrates
        # real health — it survived at least one full keepalive interval (keepalive success path) or served
        # at least one successful tool call. Only a proven session clears the reconnect budget; a transport
        # that flaps right after the handshake keeps getting charged and still reaches the park instead of
        # hot-cycling respawns forever.
        self._session_proven: bool = False
        # Never cleared (unlike _ready): separates first-connect from reconnect failures.
        self._ever_connected: bool = False
        # True from park until proven healthy again; logs the revival once.
        self._was_parked: bool = False
        self._parked_probe_count: int = 0  # Hermes patch 2026-08-21
        # In-flight RPC tasks so a deliberate teardown fails them fast; _reconnecting is True
        # during that teardown so _track_inflight_rpc turns the cancel into a retryable error.
        # In-flight RPC bookkeeping (#48069 salvage): user-visible requests registered while running so a
        # reconnect/shutdown teardown can fail them fast instead of orphaning them on a dying transport.
        self._inflight_tasks: set = set()
        self._reconnecting: bool = False
        # Latched by races (teardown-vs-keepalive, auth-lock corruption); ensure_healthy()
        # verifies before the next call.
        # See #77765, #81051, #84132.
        self._suspect_reason: Optional[str] = None
        # Teardown that failed in-flight calls => next reconnect is RACE RECOVERY, not a
        # budget charge.
        self._teardown_race: bool = False
        # One-time grace: auth/permanent failure on a PROVEN session gets one suspect+reconnect
        # cycle before parking.
        self._permanent_grace_used: bool = False
        # Children of the current stdio transport: in-flight calls fail FAST when one dies.
        # PIDs of the stdio subprocess spawned for the current transport (captured in _run_stdio). Used to
        # fail in-flight calls FAST when the child dies instead of waiting out the full tool timeout
        # (#81995).
        self._stdio_child_pids: Set[int] = set()
        self._auth_type: str = ""
        self._refresh_lock = asyncio.Lock()
        # A stdio session is one JSON-RPC stream (a concurrent list_tools can wedge a tool
        # call): serialize client-initiated RPCs per server (HTTP too, for ordering).
        self._rpc_lock = asyncio.Lock()
        self._pending_refresh_tasks: set[asyncio.Task] = set()
        # contextvars snapshot inside session.call_tool(): the SDK runs elicitation/create on a
        # task that does not inherit HERMES_SESSION_PLATFORM, so the callback replays this.
        self._pending_call_context: Optional[contextvars.Context] = None
        self._lifecycle_started_at = self._last_tool_call_at = time.monotonic()
        self._idle_timeout_seconds = self._max_lifetime_seconds = self._recycled_reason = None
        # Handshake InitializeResult: the server's REAL advertised capabilities.
        # Captures the ``InitializeResult`` returned by ``await session.initialize()`` so downstream code
        # can inspect the server's real advertised capabilities (``.capabilities.resources``,
        # ``.capabilities.prompts``) instead of assuming every ``ClientSession`` method attribute
        # corresponds to a supported server method. See #18051.
        self.initialize_result: Optional[Any] = None
        # SEP-2549 cache hints from the last tools/list (ttl_ms, cache_scope).
        self._list_cache_meta: dict = {}
        # Latched when ``ping`` returns -32601; keepalives then use list_tools. Reset per connect.
        self._ping_unsupported: bool = False

    # Content types a real Streamable-HTTP endpoint may return on the initial POST/GET;
    # anything else on a 2xx means the URL is not an MCP endpoint.
    _MCP_CONTENT_TYPES = ("application/json", "text/event-stream")

    async def _preflight_content_type(
        self,
        url: str,
        *,
        headers: Optional[dict] = None,
        ssl_verify: bool = True,
        client_cert=None,
        timeout: float = 5.0,
    ) -> None:
        """Probe *url* for an MCP-shaped response before the SDK connects.

        A misconfigured ``mcp_servers.<name>.url`` pointed at a plain web app
        returns HTML (or some other non-MCP body). The MCP SDK then sits on
        the connection for the full ``connect_timeout`` (default 60 s) before
        surfacing an opaque ``CancelledError``. A cheap, short-timeout probe
        here catches that in ≤ ``timeout`` seconds and raises
        :class:`NonMcpEndpointError` with an actionable message.

        Detection is allow-list based: a 2xx response is rejected only when it
        carries a definite content type that is NOT one an MCP endpoint uses
        (``application/json`` / ``text/event-stream``).  When HEAD/GET returns
        a non-MCP content type (e.g. ``text/html``), a lightweight JSON-RPC
        ``initialize`` POST is attempted before giving up — some servers
        (e.g. DocuSeal) serve a web UI on GET but speak Streamable HTTP only
        via POST.

        A missing or empty content type, non-2xx status, or any
        network/transport error passes through silently — the probe is
        strictly best-effort, and the real handshake remains the source of
        truth for everything except the unambiguous "this is a web page,
        not MCP" case.

        Runs on its own httpx client OUTSIDE the SDK's anyio task group, so the
        raised error propagates as itself rather than being wrapped in an
        ``ExceptionGroup`` (which is what defeats hooks installed inside the
        SDK transport).
        """
        try:
            import httpx as _httpx
        except ImportError:
            return  # No httpx → skip probe; SDK import would have failed first.

        client_kwargs: dict = {
            "verify": ssl_verify,
            "follow_redirects": True,
            "timeout": _httpx.Timeout(timeout),
        }
        if client_cert is not None:
            client_kwargs["cert"] = client_cert

        probe_headers = dict(headers) if headers else {}
        try:
            async with _httpx.AsyncClient(**client_kwargs) as client:
                # HEAD is cheapest; fall back to GET if the server doesn't
                # implement it (405 Method Not Allowed / 501 Not Implemented).
                resp = await client.head(url, headers=probe_headers)
                if resp.status_code in (405, 501):
                    resp = await client.get(url, headers=probe_headers)

                # Some MCP servers (e.g. DocuSeal) serve their web UI on
                # HEAD/GET but speak Streamable HTTP only via POST.  Before
                # rejecting the endpoint, try a lightweight JSON-RPC POST
                # probe so we don't false-positive on POST-only servers.
                ct = (
                    resp.headers.get("content-type", "")
                    .split(";")[0]
                    .strip()
                    .lower()
                )
                if (
                    ct
                    and ct not in self._MCP_CONTENT_TYPES
                    and 200 <= resp.status_code < 300
                ):
                    post_resp = await client.post(
                        url,
                        headers={
                            **probe_headers,
                            "Content-Type": "application/json",
                            "Accept": "application/json, text/event-stream",
                        },
                        content=(
                            '{"jsonrpc":"2.0","id":"_probe",'
                            '"method":"initialize",'
                            '"params":{"protocolVersion":"2025-03-26",'
                            '"capabilities":{},'
                            '"clientInfo":{"name":"hermes-probe",'
                            '"version":"0.1"}}}'
                        ),
                    )
                    if 200 <= post_resp.status_code < 300:
                        post_ct = (
                            post_resp.headers.get("content-type", "")
                            .split(";")[0]
                            .strip()
                            .lower()
                        )
                        if post_ct in self._MCP_CONTENT_TYPES:
                            resp = post_resp
        except _httpx.HTTPError:
            return  # DNS/connect/timeout/transport error — let the SDK try.

        # Only judge successful responses. A 4xx/5xx may be an auth challenge
        # or a transient error the real handshake handles correctly.
        if not (200 <= resp.status_code < 300):
            return

        ct_base = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if not ct_base:
            return  # No content type advertised — don't second-guess the SDK.
        if ct_base in self._MCP_CONTENT_TYPES:
            return  # Looks like a real MCP endpoint.

        raise NonMcpEndpointError(
            f"MCP server '{self.name}' at {url} returned Content-Type "
            f"'{ct_base}', not an MCP response (expected one of: "
            f"{', '.join(self._MCP_CONTENT_TYPES)}). The URL most likely "
            "points at a web page rather than an MCP endpoint — check it "
            "resolves to a Streamable HTTP / SSE endpoint "
            "(e.g. https://host/mcp, not https://host/)."
        )

    def _reconnect_or_reraise_group(self, eg: BaseExceptionGroup) -> str:
        """Map an SDK transport TaskGroup failure to a clean ``"reconnect"``.

        Streamable-HTTP / SSE transports run their stream pump inside an anyio
        TaskGroup. A transient stream drop (idle timeout, brief backend blip,
        server-side TCP close) surfaces as a ``BaseExceptionGroup`` escaping the
        transport context manager. Left unwrapped it reaches ``run()``'s error
        path, which applies exponential backoff and eventually *parks* the
        server for 300s and deregisters its tools — a multi-minute tool outage
        for what is usually a sub-second glitch while the POST path stays
        healthy (issue #66092).

        Returning ``"reconnect"`` instead lets ``run()`` rebuild the session
        immediately with no backoff, no park, and no tool deregistration.

        Re-raise (rather than mask) when the failure is not a transient drop:
        - shutdown is in progress (``shutdown()`` sets ``_shutdown_event``
          before it ever cancels the task);
        - the group carries a ``KeyboardInterrupt`` / ``SystemExit`` — fatal
          signals must propagate to the interpreter, never be converted into
          a reconnect;
        - the group carries a real ``CancelledError`` (task cancellation must
          propagate to asyncio, mirroring the ``run()`` guard for #9930);
        - we never reached a live session this attempt (``_ready`` unset) — a
          connect/handshake failure SHOULD fall through to ``run()``'s backoff
          rather than hot-loop reconnects against a broken endpoint.
        """
        if self._shutdown_event.is_set():
            raise eg
        fatal, _rest = eg.split((KeyboardInterrupt, SystemExit))
        if fatal is not None:
            raise eg
        cancelled, _rest = eg.split(asyncio.CancelledError)
        if cancelled is not None:
            raise eg
        if not self._ready.is_set():
            raise eg
        logger.debug(
            "MCP server '%s': transport TaskGroup exited after a live session "
            "(%r) — reconnecting immediately instead of backing off",
            self.name, eg,
        )
        return "reconnect"

    async def _run_http(self, config: dict):
        """Run the server using HTTP/StreamableHTTP transport."""
        _ensure_mcp_sdk()
        if not _MCP_HTTP_AVAILABLE:
            raise ImportError(
                f"MCP server '{self.name}' requires HTTP transport but "
                "mcp.client.streamable_http is not available. "
                "Upgrade the mcp package to get HTTP support."
            )

        url = config["url"]
        headers = dict(config.get("headers") or {})
        # Portable Agent Plugins v1 packages set strict_redirect_headers:
        # configured headers are visible package data and MUST NOT be
        # forwarded to a different origin through a redirect (spec §7.2.1).
        # Capture the configured header names before client-generated
        # headers (identity, protocol version) are merged in.
        _strict_cfg_headers = bool(config.get("strict_redirect_headers"))
        _configured_header_names = {key.lower() for key in headers}
        # Optional per-user identity header (config-gated; static or
        # profile-derived). Explicit headers of the same name win.
        headers = _apply_identity_header(self.name, config, headers)
        # Some MCP servers require MCP-Protocol-Version on the initial
        # initialize request and reject session-less POSTs otherwise.
        # Seed it as a client-level default, but treat user overrides as
        # case-insensitive so conventional casing is preserved.
        #
        # Seeded from the HANDSHAKE version, not the latest one: this transport
        # connects via `ClientSession.initialize()`, which sends
        # LATEST_HANDSHAKE_VERSION (2025-11-25) in the body. Advertising
        # 2026-07-28 in the header routes the request onto the server's
        # per-request-envelope ladder, which then rejects the legacy body for
        # missing its required `params._meta` envelope keys. The header has to
        # agree with what the body actually speaks.
        if not any(key.lower() == "mcp-protocol-version" for key in headers):
            headers["mcp-protocol-version"] = LATEST_HANDSHAKE_VERSION
        connect_timeout = config.get("connect_timeout", _DEFAULT_CONNECT_TIMEOUT)
        ssl_verify = config.get("ssl_verify", True)
        client_cert = _resolve_client_cert(self.name, config)

        # OAuth 2.1 PKCE: route through the central MCPOAuthManager so the
        # same provider instance is reused across reconnects, pre-flow
        # disk-watch is active, and config-time CLI code paths share state.
        # If OAuth setup fails (e.g. non-interactive env without cached
        # tokens), re-raise so this server is reported as failed without
        # blocking other MCP servers from connecting.
        _oauth_auth = None
        if self._auth_type == "oauth":
            try:
                from tools.mcp_oauth_manager import get_manager
                _oauth_auth = get_manager().get_or_build_provider(
                    self.name, url, config.get("oauth"),
                )
            except Exception as exc:
                logger.warning("MCP OAuth setup failed for '%s': %s", self.name, exc)
                raise

        sampling_kwargs = self._sampling.session_kwargs() if self._sampling else {}
        if self._elicitation:
            sampling_kwargs.update(self._elicitation.session_kwargs())
        if _MCP_NOTIFICATION_TYPES and _MCP_MESSAGE_HANDLER_SUPPORTED:
            sampling_kwargs["message_handler"] = self._make_message_handler()
        if _MCP_LOGGING_CALLBACK_SUPPORTED:
            sampling_kwargs["logging_callback"] = self._make_logging_callback()

        # SSE transport (for MCP servers that implement the SSE transport protocol
        # rather than Streamable HTTP). Configure with ``transport: sse`` in the
        # mcp_servers entry in config.yaml.
        if config.get("transport") == "sse":
            if _strict_cfg_headers:
                # Portable packages never translate to SSE; if a config
                # combines both anyway, fail closed rather than run a
                # transport that cannot enforce the redirect boundary.
                raise ValueError(
                    f"MCP server '{self.name}': strict_redirect_headers is "
                    "not supported on the SSE transport."
                )
            if sse_client is None:
                raise ImportError(
                    f"MCP server '{self.name}' requires SSE transport but "
                    "mcp.client.sse.sse_client is not available. "
                    "Upgrade the mcp package to get SSE support."
                )
            # sse_read_timeout governs how long sse_client will wait between
            # events on the SSE stream. Using the tool_timeout (default 60s)
            # here is wrong: SSE servers commonly hold the stream idle for
            # minutes between events, so a 60s read timeout drops the
            # connection after the first slow stretch. 300s matches the
            # Streamable HTTP code path's httpx read timeout below. Original
            # observation from @amiller in PR #5981 (Router Teamwork,
            # Supermemory on Cloudflare Workers idle-disconnect at ~60s).
            _sse_kwargs: dict = {
                "url": url,
                "headers": headers or None,
                "timeout": float(connect_timeout),
                "sse_read_timeout": 300.0,
            }
            if _oauth_auth is not None:
                # Pass OAuth auth through to sse_client so SSE MCP servers
                # behind OAuth 2.1 PKCE work. Previously built but never
                # forwarded — SSE OAuth would silently fail with 401s.
                _sse_kwargs["auth"] = _oauth_auth
            if client_cert is not None or ssl_verify is not True:
                # SSE transport doesn't expose verify/cert as kwargs, so route
                # them through an httpx_client_factory that wraps the SDK's
                # defaults (follow_redirects=True) and adds our TLS settings.
                # The SDK calls the factory with (headers, auth, timeout); we
                # forward all of those and layer verify/cert on top.
                # The client MUST come from the SDK's own httpx module
                # (httpx2 on mcp >= 2.0) — see sdk_httpx().
                _httpx_mod = sdk_httpx()

                _cert_for_factory = client_cert
                _verify_for_factory = ssl_verify

                def _mcp_http_client_factory(
                    headers=None, timeout=None, auth=None,
                ):
                    kwargs: dict = {
                        "follow_redirects": True,
                        "verify": _verify_for_factory,
                    }
                    if timeout is not None:
                        kwargs["timeout"] = timeout
                    else:
                        kwargs["timeout"] = _httpx_mod.Timeout(30.0, read=300.0)
                    if headers is not None:
                        kwargs["headers"] = headers
                    if auth is not None:
                        kwargs["auth"] = auth
                    if _cert_for_factory is not None:
                        kwargs["cert"] = _cert_for_factory
                    return _httpx_mod.AsyncClient(**kwargs)

                _sse_kwargs["httpx_client_factory"] = _mcp_http_client_factory
            try:
                async with sse_client(**_sse_kwargs) as (read_stream, write_stream):
                    async with ClientSession(
                        read_stream, write_stream, **sampling_kwargs
                    ) as session:
                        # Bound the handshake — same orphaned-task hang as the
                        # stdio path (#59349): an endpoint that accepts the
                        # connection but never answers ``initialize`` parks this
                        # coroutine forever on the background loop.
                        self.initialize_result = await self._negotiate_session(
                            session, float(connect_timeout)
                        )
                        self.session = session
                        await self._discover_tools()
                        self._ready.set()
                        self._ever_connected = True
                        # Session is live again: clear any breaker state from a
                        # prior outage so the first call after recovery isn't
                        # gated on a stale consecutive-failure count (#16788).
                        _reset_server_error(self.name)
                        # Unproven until keepalive/tool-call success (#62212).
                        self._session_proven = False
                        reason = await self._wait_for_lifecycle_event()
                        if reason == "reconnect":
                            logger.info(
                                "MCP server '%s': reconnect requested — "
                                "tearing down SSE session", self.name,
                            )
            except BaseExceptionGroup as _eg:
                # SSE transport TaskGroup dropped (idle timeout / stream blip):
                # reconnect immediately instead of backoff/park (#66092).
                reason = self._reconnect_or_reraise_group(_eg)
            return reason

        if _MCP_NEW_HTTP:
            # New API (mcp >= 1.24.0): build an explicit AsyncClient matching
            # the SDK's own create_mcp_http_client defaults. It has to come
            # from the SDK's httpx module (httpx2 on mcp >= 2.0), because the
            # SDK sends its own Request objects through this client — see
            # sdk_httpx().
            httpx = sdk_httpx()

            _original_url = httpx.URL(url)

            _strip_auth_on_cross_origin_redirect = _make_redirect_header_stripper(
                _original_url,
                strict=_strict_cfg_headers,
                configured_header_names=_configured_header_names,
            )

            client_kwargs: dict = {
                "follow_redirects": True,
                "timeout": httpx.Timeout(float(connect_timeout), read=300.0),
                "verify": ssl_verify,
                "event_hooks": {"response": [_strip_auth_on_cross_origin_redirect]},
            }
            if headers:
                client_kwargs["headers"] = headers
            if _oauth_auth is not None:
                client_kwargs["auth"] = _oauth_auth
            if client_cert is not None:
                client_kwargs["cert"] = client_cert

            # Caller owns the client lifecycle — the SDK skips cleanup when
            # http_client is provided, so we wrap in async-with.
            try:
                async with httpx.AsyncClient(**client_kwargs) as http_client:
                    # Unpacked positionally rather than by fixed arity: mcp
                    # 1.x yields (read, write, get_session_id) and 2.x yields
                    # (read, write). This file supports both SDK generations,
                    # and get_session_id was never used here.
                    async with streamable_http_client(url, http_client=http_client) as _streams:
                        read_stream, write_stream = _streams[0], _streams[1]
                        async with ClientSession(read_stream, write_stream, **sampling_kwargs) as session:
                            # Bound the handshake (#59349) — see stdio path.
                            self.initialize_result = await self._negotiate_session(
                                session, float(connect_timeout)
                            )
                            self.session = session
                            await self._discover_tools()
                            self._ready.set()
                            self._ever_connected = True
                            # Session is live again: clear any breaker state from
                            # a prior outage so the first call after recovery
                            # isn't gated on a stale failure count (#16788).
                            _reset_server_error(self.name)
                            # Unproven until keepalive/tool-call success (#62212).
                            self._session_proven = False
                            reason = await self._wait_for_lifecycle_event()
                            if reason == "reconnect":
                                logger.info(
                                    "MCP server '%s': reconnect requested — "
                                    "tearing down HTTP session", self.name,
                                )
            except BaseExceptionGroup as _eg:
                # Streamable-HTTP transport TaskGroup dropped: reconnect
                # immediately instead of backoff/park (#66092).
                reason = self._reconnect_or_reraise_group(_eg)
            return reason
        else:
            # Deprecated API (mcp < 1.24.0): manages httpx client internally.
            if _strict_cfg_headers:
                # Fail closed: without an owned httpx client we cannot hook
                # redirects, so the v1 cross-origin header boundary cannot be
                # enforced on this SDK version.
                raise ImportError(
                    f"MCP server '{self.name}' requires mcp >= 1.24.0 to "
                    "enforce the portable redirect-header boundary "
                    "(strict_redirect_headers). Upgrade the mcp package."
                )
            _http_kwargs: dict = {
                "headers": headers,
                "timeout": float(connect_timeout),
                "verify": ssl_verify,
            }
            if _oauth_auth is not None:
                _http_kwargs["auth"] = _oauth_auth
            try:
                async with streamablehttp_client(url, **_http_kwargs) as (
                    read_stream, write_stream, _get_session_id,
                ):
                    async with ClientSession(read_stream, write_stream, **sampling_kwargs) as session:
                        # Bound the handshake (#59349) — see stdio path.
                        self.initialize_result = await self._negotiate_session(
                            session, float(connect_timeout)
                        )
                        self.session = session
                        await self._discover_tools()
                        self._ready.set()
                        self._ever_connected = True
                        # Session is live again: clear any breaker state from a
                        # prior outage so the first call after recovery isn't
                        # gated on a stale consecutive-failure count (#16788).
                        _reset_server_error(self.name)
                        # Unproven until keepalive/tool-call success (#62212).
                        self._session_proven = False
                        reason = await self._wait_for_lifecycle_event()
                        if reason == "reconnect":
                            logger.info(
                                "MCP server '%s': reconnect requested — "
                                "tearing down legacy HTTP session", self.name,
                            )
            except BaseExceptionGroup as _eg:
                # Legacy Streamable-HTTP transport TaskGroup dropped: reconnect
                # immediately instead of backoff/park (#66092).
                reason = self._reconnect_or_reraise_group(_eg)
            return reason

    async def _discover_tools(self):
        """Discover tools from the connected session.

        Capability-gated: prompt-only / resource-only MCP servers don't
        implement ``tools/list``, and calling it raises ``MCPError(-32601)``,
        which previously aborted the connection — those servers could never
        stay connected for their prompts/resources. Skip the call when the
        server doesn't advertise the ``tools`` capability.
        (Ported from anomalyco/opencode#31271.)
        """
        # Fresh transport connection → re-probe with the cheap ``ping`` path.
        # Clears any latch from a prior connection in case the server gained
        # ping support across the reconnect.
        self._ping_unsupported = False
        if self.session is None:
            return
        if not self._advertises_tools():
            logger.info(
                "MCP server '%s': does not advertise 'tools' capability — "
                "skipping tools/list (prompts/resources remain available)",
                self.name,
            )
            self._tools = []
            self._register_discovered_tools_if_needed()
            return
        async with self._rpc_lock:
            self._list_cache_meta = {}
            self._tools = await _paginate_full_list(
                self.session.list_tools, "tools", self.name,
                cache_meta_out=self._list_cache_meta,
            )
        self._register_discovered_tools_if_needed()

    def _register_discovered_tools_if_needed(self) -> None:
        """Re-register tools after an owned server reconnects if needed.

        Initial registration is performed by ``_discover_and_register_server``
        after ``start()`` completes. During a later reconnect, outage handling
        may clear ``_ready`` before discovery and may deregister stale tools.
        A managed server can still be identified by its entry in ``_servers``;
        publish its freshly discovered tools before transport readiness is
        restored so a successful revival cannot come back with zero tools.
        A server retained after a recoverable initial failure is likewise
        registry-owned before its first successful session, so ownership also
        authorizes its first publication.
        """
        if self._registered_tool_names:
            return
        if not self._ready.is_set():
            with _lock:
                if _servers.get(self.name) is not self:
                    return
        self._registered_tool_names = _register_server_tools(
            self.name, self, self._config
        )
        # A retained initial-failure server that just published tools has
        # recovered: drop its stale connect error so status surfaces stop
        # reporting it as failed.
        with _lock:
            if _servers.get(self.name) is self:
                _server_connect_errors.pop(self.name, None)

    async def run(self, config: dict):
        """Long-lived coroutine: connect, discover tools, wait, disconnect.

        Includes automatic reconnection with exponential backoff if the
        connection drops unexpectedly (unless shutdown was requested).
        """
        self._config = config
        self.tool_timeout = _resolve_tool_timeout(config)
        self._auth_type = (config.get("auth") or "").lower().strip()
        self._idle_timeout_seconds = _get_lifecycle_seconds(config, "idle_timeout_seconds")
        self._max_lifetime_seconds = _get_lifecycle_seconds(config, "max_lifetime_seconds")

        # Bind the lazily-imported SDK before reading feature flags below
        # (_MCP_SAMPLING_TYPES / _MCP_ELICITATION_TYPES are False until the
        # SDK import actually runs).
        _ensure_mcp_sdk()

        # Set up sampling handler if enabled and SDK types are available
        sampling_config = config.get("sampling", {})
        if sampling_config.get("enabled", True) and _MCP_SAMPLING_TYPES:
            self._sampling = SamplingHandler(self.name, sampling_config)
        else:
            self._sampling = None

        # Set up elicitation handler if enabled and SDK types are available.
        # Servers use elicitation/create to ask the client for structured
        # input mid-tool-call (e.g. payment authorization). The handler
        # routes those requests through Hermes' approval system.
        elicitation_config = config.get("elicitation", {})
        if elicitation_config.get("enabled", True) and _MCP_ELICITATION_TYPES:
            self._elicitation = ElicitationHandler(self.name, elicitation_config, owner=self)
        else:
            self._elicitation = None

        # Validate: warn if both url and command are present
        if "url" in config and "command" in config:
            logger.warning(
                "MCP server '%s' has both 'url' and 'command' in config. "
                "Using HTTP transport ('url'). Remove 'command' to silence "
                "this warning.",
                self.name,
            )

        # Validate remote URL once, up front.  Raising here (rather than
        # letting it blow up inside the SDK's httpx layer on every retry)
        # means a typo in config.yaml fails fast with a clear error — and
        # critically, no reconnect-backoff burn.  (Ported from
        # anomalyco/opencode#25019.)
        if self._is_http():
            try:
                _validate_remote_mcp_url(self.name, config.get("url"))
            except InvalidMcpUrlError as exc:
                logger.warning("%s", exc)
                self._error = exc
                self._ready.set()
                return

            # Pre-flight content-type probe (Streamable HTTP only; SSE is
            # exercised by its own client and legitimately serves
            # text/event-stream). A URL pointed at a web-app root returns
            # HTML, which makes the SDK hang for the full connect_timeout
            # before surfacing an opaque CancelledError. Probing here — once,
            # outside the SDK task group — fails fast and non-retryably with
            # an actionable message, mirroring the URL-validation path above.
            # Skip the probe when _ready is already set (reconnect after a
            # prior successful connect) — the endpoint was validated once,
            # re-probing is a redundant round-trip. Also skip for OAuth servers:
            # without a cached token the endpoint returns HTML or 401, which
            # would incorrectly block the OAuth flow before it can run.
            if config.get("transport") != "sse" and not config.get("skip_preflight") and not self._ready.is_set() and self._auth_type != "oauth":
                try:
                    _probe_headers = dict(config.get("headers") or {})
                    await self._preflight_content_type(
                        config["url"],
                        headers=_probe_headers,
                        ssl_verify=config.get("ssl_verify", True),
                        client_cert=_resolve_client_cert(self.name, config),
                    )
                except NonMcpEndpointError as exc:
                    logger.warning("%s", exc)
                    self._error = exc
                    self._ready.set()
                    return

        self._reconnect_retries = 0
        initial_retries = 0
        backoff = 1.0

        while True:
            try:
                if self._is_http():
                    lifecycle_reason = await self._run_http(config)
                else:
                    lifecycle_reason = await self._run_stdio(config)
                # Transport returned cleanly. Two cases:
                #  - _shutdown_event was set: exit the run loop entirely.
                #  - _reconnect_event was set (auth recovery): loop back and
                #    rebuild the MCP session with fresh credentials. Do NOT
                #    touch the retry counters — this is not a failure.
                if self._shutdown_event.is_set():
                    break
                if lifecycle_reason == "recycle":
                    logger.info(
                        "MCP server '%s': stdio session recycled after %s; "
                        "waiting for lazy reconnect",
                        self.name, self._recycled_reason,
                    )
                    self.session = None
                    await self._wait_for_lazy_reconnect()
                    if self._shutdown_event.is_set():
                        break
                    self._reconnect_event.clear()
                    continue
                # Per-cycle reconnect chatter — DEBUG. In the flapping case
                # this fires on every rebuild; the WARNINGs live on the
                # state transitions.
                logger.debug(
                    "MCP server '%s': reconnecting (OAuth recovery or "
                    "manual refresh)",
                    self.name,
                )
                # A clean transport return means a session was established and
                # then asked to rebuild (auth recovery / manual refresh /
                # keepalive failure / transport TaskGroup drop). That alone is
                # NOT proof of health: a flapping transport handshakes fine and
                # drops moments later, and resetting the budget here let such
                # servers respawn forever (#62212 — 6212 spawns in 63h).
                # Only clear the consecutive-failure budget once the session
                # PROVED healthy — survived >=1 full keepalive interval or
                # served >=1 successful tool call (_mark_session_proven).
                if self._teardown_race and not self._session_proven:
                    # The previous cycle ended because a teardown cancelled
                    # in-flight calls (keepalive/refresh race, auth recovery)
                    # — that is RECOVERY, not a transport failure. Do NOT
                    # charge the rapid-drop budget: a single race must never
                    # reach the park (#81051/#77765/#84132). Only genuinely
                    # repeated unproven drops still exhaust the budget below.
                    logger.info(
                        "MCP server '%s': reconnect after teardown race "
                        "(in-flight calls were failed); not charging the "
                        "rapid-drop budget",
                        self.name,
                    )
                    self._teardown_race = False
                    backoff = 1.0
                elif self._session_proven:
                    self._reconnect_retries = 0
                    backoff = 1.0
                else:
                    # Unproven session: charge the rapid-drop budget so a
                    # flapping transport still reaches the park.
                    self._reconnect_retries += 1
                    if self._reconnect_retries > _MAX_RECONNECT_RETRIES:
                        logger.warning(
                            "MCP server '%s': %d consecutive reconnects "
                            "without a healthy session (rapid-drop budget "
                            "exhausted), parking; will self-probe every %ds "
                            "until it recovers (state: degraded → parked)",
                            self.name, _MAX_RECONNECT_RETRIES,
                            _PARKED_RETRY_INTERVAL,
                        )
                        self._was_parked = True
                        self._deregister_tools()
                        self._reconnect_event.clear()
                        parked = await self._wait_for_reconnect_or_shutdown(
                            timeout=_adaptive_parked_interval(self._parked_probe_count)
                        )
                        self._parked_probe_count += 1  # Hermes patch 2026-08-21
                        if parked == "shutdown":
                            break
                        logger.debug(
                            "MCP server '%s': attempting revival from parked "
                            "state (self-probe or explicit reconnect request); "
                            "rebuilding transport.",
                            self.name,
                        )
                        # One probe attempt per wake — see the exception-path
                        # park below.
                        self._reconnect_retries = _MAX_RECONNECT_RETRIES
                        backoff = 1.0
                # Reset the session reference and readiness; _run_http/_run_stdio
                # will repopulate both on successful re-entry.  Leaving
                # _ready set here lets handler-side recovery mistake the stale
                # pre-reconnect session for a fresh one and retry too early.
                self._ready.clear()
                self.session = None
                continue
            except asyncio.CancelledError:
                # Task was cancelled (shutdown, gateway restart, explicit
                # task.cancel()). Don't treat this as a connection failure —
                # CancelledError inherits from BaseException (not Exception)
                # in Python 3.11+, so the broad ``except Exception`` below
                # would NOT catch it; we'd silently exit the reconnect loop
                # and the MCP server would stay dead until Hermes is fully
                # restarted. Re-raise so the task's cancellation propagates
                # correctly to asyncio's task machinery and ``shutdown()``'s
                # ``await self._task`` completes. See #9930.
                self.session = None
                raise
            except Exception as exc:
                self.session = None
                # Unwrap anyio TaskGroup wrappers first: str(exc) on a
                # BaseExceptionGroup is "unhandled errors in a TaskGroup
                # (N sub-exceptions)" — useless in logs, and it hides the
                # root cause from the auth/permanence classification below.
                # Empty dead-pipe errors still get a name this way
                # (e.g. "BrokenPipeError: ").
                root = _unwrap_exception_group(exc)
                failure_class = _classify_mcp_failure(root)
                if self._is_recycled_stdio():
                    logger.warning(
                        "MCP server '%s': lazy reconnect after stdio recycle "
                        "failed, marking unavailable while retrying: %s: %s",
                        self.name, type(root).__name__, root,
                    )
                    self._recycled_reason = None

                # If this is the first connection attempt, retry with backoff
                # before giving up. A transient DNS/network blip at startup
                # should not permanently kill the server. Gated on
                # ``_ever_connected`` rather than ``_ready`` — ``_ready`` is
                # cleared on every reconnect cycle (see below), so a server
                # that already registered tools once and then dropped would
                # otherwise be misclassified as never having connected and
                # re-enter this initial-connect ladder (#94654).
                # ``_ever_connected`` itself is set once and never cleared.
                # (Ported from Kilo Code's MCP resilience fix.)
                if not self._ever_connected:
                    if failure_class == "permanent":
                        # Deterministic failure (bad command, non-MCP URL,
                        # 401/403): every retry hits the same wall. Park
                        # immediately instead of burning the retry ladder
                        # and spamming N identical warnings (#65673).
                        #
                        # Auth failures park here too rather than returning.
                        # Returning ends the run task, and with it the only
                        # listener on ``_reconnect_event`` — so a 401 on the
                        # very first connect left the server unrevivable for
                        # the life of the process, even after the user
                        # re-authenticated with ``hermes mcp login``. Parking
                        # keeps the task alive so the 300s self-probe (and an
                        # explicit /mcp refresh) can pick up fresh tokens.
                        if _is_auth_error(root):
                            logger.warning(
                                "MCP server '%s' failed initial authentication, "
                                "parking until credentials change; re-authenticate "
                                "with `hermes mcp login %s` "
                                "(state: connecting → parked): %s: %s",
                                self.name, self.name,
                                type(root).__name__, root,
                            )
                        else:
                            logger.warning(
                                "MCP server '%s' failed initial connection with a "
                                "permanent error, parking without retries "
                                "(state: connecting → parked): %s: %s",
                                self.name, type(root).__name__, root,
                            )
                        self._error = exc
                        self._ready.set()
                        self._was_parked = True
                        self._deregister_tools()
                        self._reconnect_event.clear()
                        parked = await self._wait_for_reconnect_or_shutdown(
                            timeout=_adaptive_parked_interval(self._parked_probe_count)
                        )
                        self._parked_probe_count += 1  # Hermes patch 2026-08-21
                        if parked == "shutdown":
                            return
                        logger.debug(
                            "MCP server '%s': attempting revival after "
                            "permanent initial failure (self-probe or explicit "
                            "reconnect request); rebuilding transport.",
                            self.name,
                        )
                        initial_retries = 0
                        self._reconnect_retries = 0
                        backoff = 1.0
                        self._error = None
                        self._ready.clear()
                        continue

                    initial_retries += 1
                    if initial_retries > _MAX_INITIAL_CONNECT_RETRIES:
                        logger.warning(
                            "MCP server '%s' failed initial connection after "
                            "%d attempts, parking until a reconnect is "
                            "requested (state: connecting → parked): %s: %s",
                            self.name, _MAX_INITIAL_CONNECT_RETRIES,
                            type(root).__name__, root,
                        )
                        self._error = exc
                        self._ready.set()
                        self._was_parked = True
                        self._deregister_tools()
                        self._reconnect_event.clear()
                        parked = await self._wait_for_reconnect_or_shutdown(
                            timeout=_adaptive_parked_interval(self._parked_probe_count)
                        )
                        self._parked_probe_count += 1  # Hermes patch 2026-08-21
                        if parked == "shutdown":
                            return
                        logger.debug(
                            "MCP server '%s': attempting revival after initial "
                            "connection failures (self-probe or explicit "
                            "reconnect request); rebuilding transport.",
                            self.name,
                        )
                        initial_retries = 0
                        self._reconnect_retries = 0
                        backoff = 1.0
                        self._error = None
                        self._ready.clear()
                        continue

                    logger.debug(
                        "MCP server '%s' initial connection failed "
                        "(attempt %d/%d), retrying in %.0fs: %s: %s",
                        self.name, initial_retries,
                        _MAX_INITIAL_CONNECT_RETRIES, backoff,
                        type(root).__name__, root,
                    )
                    await asyncio.sleep(_jittered(backoff))
                    backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)

                    # Check if shutdown was requested during the sleep
                    if self._shutdown_event.is_set():
                        self._error = exc
                        self._ready.set()
                        return
                    continue

                # If shutdown was requested, don't reconnect
                if self._shutdown_event.is_set():
                    logger.debug(
                        "MCP server '%s' disconnected during shutdown: %s: %s",
                        self.name, type(root).__name__, root,
                    )
                    return

                if failure_class == "permanent":
                    # Auth-lock corruption guard (#81051/#77765/#84132): an
                    # auth-classified permanent failure on a previously
                    # PROVEN session is often a transient/ambiguous state
                    # (OAuth flow lock left corrupt by a raced teardown),
                    # not truly revoked credentials. Grant ONE
                    # suspect+reconnect cycle before the park ladder: mark
                    # the connection suspect so the next call health-checks
                    # it, and rebuild the transport instead of parking.
                    if (
                        _is_auth_error(root)
                        and self._session_proven
                        and not self._permanent_grace_used
                    ):
                        self._permanent_grace_used = True
                        self.mark_suspect(
                            f"auth error on proven session: {root}"
                        )
                        logger.warning(
                            "MCP server '%s': auth error on a previously "
                            "healthy session — marking suspect and forcing "
                            "one reconnect instead of parking (state: "
                            "connected → suspect): %s: %s",
                            self.name, type(root).__name__, root,
                        )
                        self._reconnect_retries = 0
                        backoff = 1.0
                        await asyncio.sleep(_jittered(1.0))
                        if self._shutdown_event.is_set():
                            return
                        continue
                    # A previously-working server now fails deterministically
                    # (revoked credentials, URL now serving a web page, stdio
                    # binary uninstalled). Retrying can't help — park
                    # immediately without burning the retry ladder.
                    logger.warning(
                        "MCP server '%s' hit a permanent error, parking "
                        "without retries; will self-probe every %ds "
                        "(state: connected → parked): %s: %s",
                        self.name, _PARKED_RETRY_INTERVAL,
                        type(root).__name__, root,
                    )
                    self._was_parked = True
                    self._deregister_tools()
                    self._reconnect_event.clear()
                    parked = await self._wait_for_reconnect_or_shutdown(
                        timeout=_adaptive_parked_interval(self._parked_probe_count)
                    )
                    self._parked_probe_count += 1  # Hermes patch 2026-08-21
                    if parked == "shutdown":
                        return
                    logger.debug(
                        "MCP server '%s': attempting revival from parked state "
                        "(permanent error; self-probe or explicit reconnect "
                        "request); rebuilding transport.",
                        self.name,
                    )
                    self._reconnect_retries = _MAX_RECONNECT_RETRIES
                    backoff = 1.0
                    continue

                self._reconnect_retries += 1
                if self._reconnect_retries > _MAX_RECONNECT_RETRIES:
                    logger.warning(
                        "MCP server '%s' failed after %d reconnection attempts, "
                        "parking; will self-probe every %ds until it recovers "
                        "(state: degraded → parked): %s: %s",
                        self.name, _MAX_RECONNECT_RETRIES,
                        _PARKED_RETRY_INTERVAL,
                        type(root).__name__, root,
                    )
                    # Do NOT return — exiting the task orphans the server:
                    # nothing would ever listen for _reconnect_event again
                    # and the server would be permanently wedged for the
                    # life of the process (#16788). Instead, drop the phantom
                    # tools from the registry and park. Because parking
                    # deregisters the tools, no tool call can reach the
                    # circuit-breaker half-open probe or _signal_reconnect —
                    # so the park is a TIMED wait: every _PARKED_RETRY_INTERVAL
                    # we wake and attempt one reconnect ourselves (#57129).
                    # An explicit _reconnect_event.set() (OAuth recovery,
                    # manual /mcp refresh) still wakes us immediately.
                    self._was_parked = True
                    self._deregister_tools()
                    self._reconnect_event.clear()
                    parked = await self._wait_for_reconnect_or_shutdown(
                        timeout=_adaptive_parked_interval(self._parked_probe_count)
                    )
                    self._parked_probe_count += 1  # Hermes patch 2026-08-21
                    if parked == "shutdown":
                        return
                    logger.debug(
                        "MCP server '%s': attempting revival from parked state "
                        "(self-probe or explicit reconnect request); "
                        "rebuilding transport.",
                        self.name,
                    )
                    # One probe attempt per wake: budget of 1 so a still-dead
                    # server parks again for another interval instead of
                    # burning 5 rapid retries each cycle.
                    self._reconnect_retries = _MAX_RECONNECT_RETRIES
                    backoff = 1.0
                    continue

                # Per-attempt retry chatter stays at DEBUG; state transitions
                # (connected->degraded, degraded->parked, parked->revived)
                # carry the WARNINGs — one line per transition, not per try.
                logger.debug(
                    "MCP server '%s' connection lost (attempt %d/%d), "
                    "reconnecting in %.0fs: %s: %s",
                    self.name, self._reconnect_retries, _MAX_RECONNECT_RETRIES,
                    backoff, type(root).__name__, root,
                )
                await asyncio.sleep(_jittered(backoff))
                backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)

                # Check again after sleeping
                if self._shutdown_event.is_set():
                    return
            finally:
                self.session = None
                # Children of this transport are gone (or about to be);
                # stale PIDs must never fast-fail the NEXT transport's calls.
                self._stdio_child_pids = set()

    async def start(self, config: dict):
        """Create the background Task and wait until ready (or failed)."""
        self._task = asyncio.ensure_future(self.run(config))
        try:
            await self._ready.wait()
        except asyncio.CancelledError:
            # The caller's connect timeout (discover_mcp_tools wraps start()
            # in asyncio.wait_for) cancels *this* coroutine, but the
            # ensure_future'd run() task is independent and would otherwise
            # keep running detached — parked on a hung transport with no
            # owner to reap it (#59349). Propagate the cancellation so the
            # transport context managers unwind and their finally blocks
            # release the child process / FDs.
            if self._task and not self._task.done():
                self._task.cancel()
            raise
        if self._error:
            raise self._error

    async def shutdown(self):
        """Signal the Task to exit and wait for clean resource teardown."""
        self._shutdown_event.set()
        # Defensive: if _wait_for_lifecycle_event is blocking, we need ANY
        # event to unblock it. _shutdown_event alone is sufficient (the
        # helper checks shutdown first), but setting reconnect too ensures
        # there's no race where the helper misses the shutdown flag after
        # returning "reconnect".
        self._reconnect_event.set()
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except asyncio.TimeoutError:
                logger.warning(
                    "MCP server '%s' shutdown timed out, cancelling task",
                    self.name,
                )
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
        if self._pending_refresh_tasks:
            for task in list(self._pending_refresh_tasks):
                task.cancel()
            await asyncio.gather(*self._pending_refresh_tasks, return_exceptions=True)
            self._pending_refresh_tasks.clear()
        self._deregister_tools()
        self.session = None

    def _deregister_tools(self) -> None:
        """Drop this server's tools from the global registry (idempotent).

        Pulls the server's tool schemas out of the registry so the agent
        stops advertising them to the model. Called on shutdown AND when the
        reconnect budget is exhausted, so a dead server never leaves phantom
        tool definitions bloating the prompt cache and producing "not
        connected" errors on every turn.
        """
        from tools.registry import registry

        for tool_name in list(getattr(self, "_registered_tool_names", [])):
            registry.deregister(tool_name)
            _forget_mcp_tool_server(tool_name)
        self._registered_tool_names = []

    async def _wait_for_lazy_reconnect(self) -> None:
        """Wait while an intentionally recycled stdio server is dormant."""
        shutdown_task = asyncio.create_task(self._shutdown_event.wait())
        reconnect_task = asyncio.create_task(self._reconnect_event.wait())
        try:
            await asyncio.wait(
                {shutdown_task, reconnect_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (shutdown_task, reconnect_task):
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass


# ---------------------------------------------------------------------------
# Module-level state (every mutation under ``_lock``)
# ---------------------------------------------------------------------------

_servers: Dict[str, MCPServerTask] = {}
# Profile registry scope per live connection (None outside multiplex) so a multiplexed
# /reload-mcp tears down only its own profile's servers.
_server_scope_keys: Dict[str, Optional[str]] = {}
# Registry scopes that have adopted a live server connection. The owning scope above remains
# authoritative for connection teardown; this set preserves visibility for shared connections.
_server_tool_scopes: Dict[str, set] = {}
_server_connecting: set[str] = set()
_server_connect_errors: Dict[str, str] = {}
# Lazy startup: servers registered from the schema cache without connecting; popped on
# first real connection.
# Keyed by server name; entries are popped once a real connection is established on first use. See #56832.
_lazy_server_configs: Dict[str, dict] = {}
_lazy_server_fingerprints: Dict[str, str] = {}
_lazy_server_tool_names: Dict[str, List[str]] = {}
# Task-local claim around ``_connect_server``: discovery retains a recoverable parked task
# while standalone probes never publish failed servers into module-global ownership.
_connect_server_claim: contextvars.ContextVar[Optional[Callable[[MCPServerTask], None]]] = (
    contextvars.ContextVar("mcp_connect_server_claim", default=None))

# Per-server connect cooldown: a server that fails to spawn never reaches ``_servers``, so
# without it every ``discover_mcp_tools()`` (one per worker session) would respawn it — a
# restart storm whose unreaped children destabilise healthy servers. Exponential-backoff
# deadline honoured by ``register_mcp_servers``; cleared on success.
# Connection-retry cooldown (per-server isolation against restart storms). A single stdio MCP server that
# fails to spawn (bad PATH, ``exec: not found``, crash-on-start) is never recorded in ``_servers`` --
# ``start()`` raises and ``_discover_and_register_server`` aborts before the ``_servers[name] = server``
# line. Without a cooldown, EVERY subsequent ``discover_mcp_tools()`` (one per agent worker session, i.e.
# every few seconds) sees the server as "not connected" and re-spawns it from scratch. That is the restart
# storm in #50394: the failing server is re-attempted on the shared MCP event loop on every worker session,
# the subprocesses pile up unreaped, and the churn destabilises the healthy co-located servers (their tools
# intermittently surface as "Unknown tool"). Fix: after a failed connection attempt, stamp a monotonic
# ``retry_after`` deadline with exponential backoff. ``register_mcp_servers`` skips a server whose cooldown
# has not elapsed, so a chronically failing server is retried on a backoff schedule instead of on every
# worker session -- isolating it from the rest of the bridge. A successful connection clears the state.
_server_connect_retry_after: Dict[str, float] = {}   # name -> monotonic deadline
_server_connect_failures: Dict[str, int] = {}        # name -> consecutive failures
_CONNECT_RETRY_BASE_BACKOFF_SEC, _CONNECT_RETRY_MAX_BACKOFF_SEC = 30.0, 600.0

# Per-server circuit breaker: closed -> open (calls short-circuit until the cooldown) ->
# half-open (next call probes). Mutate only via _bump_server_error / _reset_server_error.
# After _CIRCUIT_BREAKER_THRESHOLD consecutive failures, the handler returns a "server unreachable" message
# that tells the model to stop retrying, preventing the 90-iteration burn loop described in #10447. State
# machine: closed    — error count below threshold; all calls go through. open      — threshold reached;
# calls short-circuit until the cooldown elapses. half-open — cooldown elapsed; the next call is a probe
# that actually hits the session. Probe success → closed. Probe failure → reopens (cooldown re-armed).
# ``_server_breaker_opened_at`` records the monotonic timestamp when the breaker most recently transitioned
# into the open state. Use the ``_bump_server_error`` / ``_reset_server_error`` helpers to mutate this state
# — they keep the count and timestamp in sync.
_server_error_counts: Dict[str, int] = {}
_server_breaker_opened_at: Dict[str, float] = {}
_CIRCUIT_BREAKER_THRESHOLD, _CIRCUIT_BREAKER_COOLDOWN_SEC = 3, 60.0

# Trust-tier gating (``trust: full | untrusted``): on an untrusted server every write-capable
# call (discovery-time ``readOnlyHint`` not exactly True; malformed fails closed) needs approval
# before the RPC fires. A lying readOnlyHint can only skip approval for calls the operator was
# already warned about, never widen access. Missing trust = full; unrecognized = untrusted (a
# typo must never disable the gate). Classified at CALL time from DISCOVERY data: no schema
# mutation, prompt cache intact.
_server_trust_levels: Dict[str, str] = {}
_tool_read_only_hints: Dict[str, Dict[str, bool]] = {}

_TRUST_FULL, _TRUST_UNTRUSTED = "full", "untrusted"


def _bump_server_error(server_name: str) -> None:
    """Count a failure; at the threshold (re)stamp the breaker-open time."""
    n = _server_error_counts.get(server_name, 0) + 1
    _server_error_counts[server_name] = n
    if n >= _CIRCUIT_BREAKER_THRESHOLD:
        _server_breaker_opened_at[server_name] = time.monotonic()


def _reset_server_error(server_name: str) -> None:
    """Close the breaker on any unambiguous success signal."""
    _server_error_counts[server_name] = 0
    _server_breaker_opened_at.pop(server_name, None)


# Raw server names opted into parallel tool calls (``foo-bar``/``foo_bar`` sanitize alike but
# must not share policy).
_parallel_safe_servers: set = set()
# registry tool name -> raw server name (the generated name is lossy; never re-parse it).
_mcp_tool_server_names: Dict[str, str] = {}

# Dedicated event loop in a background daemon thread; _lock guards the loop handles, _servers,
# the status maps and the PID ledgers.
_mcp_loop: Optional[asyncio.AbstractEventLoop] = None
_mcp_thread: Optional[threading.Thread] = None
_lock = threading.Lock()


# ---- Shared parent-death supervisor (state lives HERE: tests rebind ``_death_supervisor``) ----
# If this process dies without running its cleanup path (kill -9, OOM, crash, force-quit), stdio
# MCP children reparent to init and run forever; macOS has no PR_SET_PDEATHSIG, so something has
# to outlive us and reap them. ONE supervisor process serves all stdio servers and is told which
# process groups to reap over a pipe; it detects our death as EOF on that pipe (exact, instant)
# rather than polling getppid(). Replaced the per-server watchdog wrapper (~10 MB resident per
# server, plus a signal-forwarding layer because wrapping put the server in a different session
# from the pgid tracked for killpg). See tools/mcp_death_supervisor.py. POSIX-only, matching the
# killpg-based orphan cleanup below.
_death_supervisor = None  # Optional[subprocess.Popen]
_death_supervisor_lock = threading.Lock()
# Groups the supervisor is reaping on our behalf; replayed verbatim on respawn so a respawn never
# silently drops coverage for servers that are still running.
_supervised_pgids: set = set()


def _spawn_death_supervisor():
    """Start the shared supervisor, or None if it cannot be started."""
    import subprocess
    supervisor = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_death_supervisor.py")
    try:
        # start_new_session=True is load-bearing: shutdown paths killpg this process's own group,
        # which would kill the supervisor before it could reap anything.
        return subprocess.Popen(
            [sys.executable, supervisor, "--parent-pgid", str(os.getpgid(0))],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=_get_mcp_stderr_log(),
            start_new_session=True, close_fds=True, text=True)
    except Exception:
        # Never let supervisor bookkeeping block a real MCP connection: graceful shutdown paths
        # still reap normally; only the ungraceful-exit safety net is lost.
        logger.debug("Could not start the MCP parent-death supervisor", exc_info=True)
        return None


def _prune_dead_supervised_pgids() -> set:
    """Forget supervised groups with no members left; return what went. Caller holds
    ``_death_supervisor_lock``. Signal 0 is a pure existence probe (cannot terminate anything).
    It narrows, but cannot close, the window where a dead group's pgid is recycled before we
    notice (residual-risk note in ``tools/mcp_death_supervisor.py``)."""
    killpg = getattr(os, "killpg", None)
    if killpg is None:  # windows-footgun: ok - POSIX-only, guarded
        return set()
    stale = set()
    for pgid in list(_supervised_pgids):
        try:
            killpg(pgid, 0)
        except ProcessLookupError:
            stale.add(pgid)
        except (PermissionError, OSError):
            # Exists but not ours to signal, or the probe failed: keep it — dropping coverage on
            # an ambiguous answer is the more expensive mistake.
            pass
    _supervised_pgids.difference_update(stale)
    return stale


def _update_death_supervisor(verb: str, pgids) -> None:
    """Register or unregister process groups (``verb`` is ``"register"``/``"unregister"``) with
    the shared supervisor. Failures are swallowed: losing the safety net must never fail a live
    MCP session."""
    if os.name != "posix":
        return
    wanted = {int(pgid) for pgid in pgids}
    if not wanted:
        return

    global _death_supervisor
    with _death_supervisor_lock:
        if verb == "register":
            _supervised_pgids.update(wanted)
        else:
            _supervised_pgids.difference_update(wanted)

        # A registration outlives the server only while some member survives (e.g. an orphaned
        # grandchild teardown failed to kill, deliberately kept registered). Once that group is
        # empty its pgid can be recycled by a stranger, so prune here too — the orphan sweep
        # unregisters what it reaps but is not guaranteed to run in a given process.
        stale = _prune_dead_supervised_pgids()

        proc = _death_supervisor
        if proc is None or proc.poll() is not None:
            if not _supervised_pgids:
                # Nothing left to cover: nothing to tell and nothing to respawn for. Keyed on
                # the SET, not the verb: after a broken-pipe write dropped the supervisor with
                # groups still registered, an unregister must still rebuild coverage for the
                # survivors.
                return
            # See #93517.
            proc = _spawn_death_supervisor()
            _death_supervisor = proc
            if proc is None:
                return
            # A fresh supervisor knows nothing: replay live coverage (already reflects this
            # call's mutation and the prune, so pruned groups never reach the replacement).
            payload = "".join(f"register {pgid}\n" for pgid in _supervised_pgids)
        else:
            payload = "".join(f"{verb} {pgid}\n" for pgid in wanted)
            payload += "".join(f"unregister {pgid}\n" for pgid in stale)

        try:
            proc.stdin.write(payload)
            proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            # It exited between poll() and write(). Drop it so the next call respawns and replays
            # from ``_supervised_pgids`` (the set, not the pipe, is the record of what needs reaping).
            _death_supervisor = None
            return

        if not _supervised_pgids:
            # Nothing left to reap: release the supervisor rather than keep a ~15 MB process and a
            # pipe resident for the life of a gateway. Closing our write end is the same EOF parent
            # death sends; with an empty set it exits. The next register respawns and replays.
            try:
                proc.stdin.close()
            except (BrokenPipeError, ValueError, OSError):
                pass
            # Reap it, or the exited supervisor stays a zombie until the next Popen in this process.
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001 - timeout or already gone; either way we drop it
                pass
            _death_supervisor = None


def _mcp_registry_scope() -> Optional[str]:
    """Registry scope for MCP registrations: a profile overlay under a multiplexer, else None."""
    from agent.secret_scope import is_multiplex_active
    if not is_multiplex_active():
        return None
    from tools.registry import registry
    return registry.current_scope_key()


def _server_registry_scope(name: str) -> Optional[str]:
    """Scope owning *name*'s tools: the one captured at adoption (teardown runs on the MCP
    loop without the discovering profile's context), else the current one."""
    if name in _server_scope_keys:
        return _server_scope_keys[name]
    return _mcp_registry_scope()


def _server_visible_in_scope(name: str, scope: Optional[str]) -> bool:
    """Whether a live server is visible from ``scope`` without changing its teardown owner."""
    if scope is None:
        return True
    return (_server_scope_keys.get(name) == scope
            or scope in _server_tool_scopes.get(name, ()))


# Cross-process discovery guard: advisory file lock so gateway + CLI + TUI don't all discover.
# See issue #62771.
_LOCK_UNAVAILABLE: Any = object()  # sentinel: locking broken/unavailable
_MCP_DISCOVERY_LOCK_PATH: Optional[str] = None  # resolved lazily
# Bounded wait when another process holds the lock.
_MCP_DISCOVERY_LOCK_MAX_RETRIES, _MCP_DISCOVERY_LOCK_RETRY_DELAY_S = 240, 0.5


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from typing import Coroutine  # noqa: F401,E402
from types import SimpleNamespace  # noqa: F401,E402
from typing import Tuple  # noqa: F401,E402
from contextlib import asynccontextmanager  # noqa: F401,E402
import concurrent.futures  # noqa: F401,E402
from datetime import datetime  # noqa: F401,E402
import errno  # noqa: F401,E402
import fnmatch  # noqa: F401,E402
import json  # noqa: F401,E402
import math  # noqa: F401,E402
import random  # noqa: F401,E402
import re  # noqa: F401,E402
import shutil  # noqa: F401,E402
from urllib.parse import urlparse  # noqa: F401,E402


_PLUGIN_COMPAT_LAZY = {
    'InvalidMcpUrlError': ('tools.mcp_tool_errors', 'InvalidMcpUrlError'),
    'MCP_TOOL_NAME_PREFIX': ('tools.mcp_tool_schema', 'MCP_TOOL_NAME_PREFIX'),
    'NonMcpEndpointError': ('tools.mcp_tool_errors', 'NonMcpEndpointError'),
    'discover_mcp_tools': ('tools.mcp_tool_discovery', 'discover_mcp_tools'),
    'get_mcp_status': ('tools.mcp_tool_discovery', 'get_mcp_status'),
    'get_registered_mcp_server_names': ('tools.mcp_tool_discovery', 'get_registered_mcp_server_names'),
    'has_registered_mcp_tools': ('tools.mcp_tool_discovery', 'has_registered_mcp_tools'),
    'is_mcp_tool_parallel_safe': ('tools.mcp_tool_discovery', 'is_mcp_tool_parallel_safe'),
    'matches_name_filter': ('tools.mcp_tool_schema', 'matches_name_filter'),
    'mcp_prefixed_tool_name': ('tools.mcp_tool_schema', 'mcp_prefixed_tool_name'),
    'persist_agent_tool_names': ('tools.mcp_tool_agent', 'persist_agent_tool_names'),
    'probe_mcp_server_tools': ('tools.mcp_tool_discovery', 'probe_mcp_server_tools'),
    'reconnect_mcp_server': ('tools.mcp_tool_loop', 'reconnect_mcp_server'),
    'refresh_agent_mcp_tools': ('tools.mcp_tool_agent', 'refresh_agent_mcp_tools'),
    'register_mcp_servers': ('tools.mcp_tool_discovery', 'register_mcp_servers'),
    'reprobe_tool_availability': ('tools.mcp_tool_agent', 'reprobe_tool_availability'),
    'restore_agent_tool_prefix': ('tools.mcp_tool_agent', 'restore_agent_tool_prefix'),
    'sanitize_mcp_name_component': ('tools.mcp_tool_schema', 'sanitize_mcp_name_component'),
    'shutdown_mcp_servers': ('tools.mcp_tool_lifecycle', 'shutdown_mcp_servers'),
    'strip_unicode_tags': ('tools.ansi_strip', 'strip_unicode_tags'),
    'tool_error': ('tools.registry', 'tool_error'),
}

_plugin_compat_prev_getattr = __getattr__


def __getattr__(name):  # PEP 562 — chained onto the module's own __getattr__
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        return _plugin_compat_prev_getattr(name)
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
