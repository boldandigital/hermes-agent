"""A bulk prune must reach the live owner, not the lock it can never win (#138915).

A gateway that owns the profile holds the exclusive-maintenance reservation, so the
standalone dashboard's ``POST /api/sessions/prune`` died on a bare 409 ("session write
refused") instead of asking the owner to delete. The owner already speaks the canonical
``session.prune`` verb, so the route hands the write over instead of asking for a lock it
can never win.

The owner end here is the production ``AuthorityConnection`` dispatch against a real
store, so the cold/stored row whose owner is a dashboard principal (a per-row owner check
would refuse it) is proven to survive the trip as a deletion, not as a 403.
"""
import time
from contextlib import asynccontextmanager, closing
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

import hermes_state_runtime as rt
from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_controls import AuthorityConnection
from hermes_cli.web_routers.sessions import manage_router
from hermes_state import SessionDB

# The signed-in dashboard principal that owns the stored Desktop rows. Distinct from the
# socket peer subject the control-socket ticket acts as (below).
DASHBOARD_OWNER = 'basic:wweic'
# The Unix peer subject a control-socket ticket is minted for.
PEER_SUBJECT = 'uid:501'


def _aged(db, sid, days, *, ended=True):
    """Age a row's activity (and optionally end it), the way an old conversation looks."""
    at = time.time() - days * 86400

    def write(conn):
        conn.execute('UPDATE sessions SET started_at=?, last_activity_at=?, ended_at=? WHERE id=?',
                     (at, at, at if ended else None, sid))
        conn.execute('UPDATE messages SET timestamp=? WHERE session_id=?', (at, sid))
    db._execute_write(write)


@pytest.fixture
def owner_store(monkeypatch, tmp_path):
    """A live owner's own store, with a cold stored row owned by a dashboard principal."""
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr('hermes_state._default_db_path', lambda: tmp_path / 'state.db')
    db = SessionDB(db_path=tmp_path / 'state.db')
    epoch = rt.begin_runtime_epoch(db, instance_id='owner')
    retired = []
    runner = SimpleNamespace(_draining=False,
                             session_store=SimpleNamespace(retire_runtime_sessions=retired.extend))
    authority = SessionAuthority(runner, profile_id=str(tmp_path), instance_id='owner', db=db, epoch=epoch)
    # A cold, stored Desktop row owned by the signed-in dashboard principal.
    db.create_session('cold-desktop', source='desktop', user_id=DASHBOARD_OWNER)
    db.append_message('cold-desktop', 'user', 'stored turn')
    _aged(db, 'cold-desktop', 200)
    # An open row: a prune never deletes it, but the preview reports it as skipped.
    db.create_session('open-desktop', source='desktop', user_id=DASHBOARD_OWNER)
    db.append_message('open-desktop', 'user', 'still running')
    _aged(db, 'open-desktop', 200, ended=False)
    # A recent ended row: outside the implicit 90-day window.
    db.create_session('fresh-cli', source='cli')
    db.append_message('fresh-cli', 'user', 'today')
    _aged(db, 'fresh-cli', 1)
    authority.sessions['cold-desktop'] = LiveSession(None, 'route-cold-desktop')
    with closing(db):
        yield SimpleNamespace(db=db, authority=authority, retired=retired, home=tmp_path)


@pytest.fixture
def prune_app():
    app = FastAPI()
    app.include_router(manage_router)
    return app


def _endpoint(profile_id):
    from hermes_cli.gateway_runtime import GatewayEndpoint
    return GatewayEndpoint(profile_id=profile_id, instance_id='owner', authority_epoch=1,
                           runtime_protocol=1, api_origin='http://127.0.0.1:1', supervisor='none',
                           capabilities=frozenset({'session:prune'}))


def _discover(monkeypatch, endpoint, seen=None):
    """Read-only discovery: a ready owner on the selected home, or nothing at all."""
    from hermes_cli import gateway_runtime

    def fake_discover(home, timeout=2.0):
        if seen is not None:
            seen['home'] = home
            seen['timeout'] = timeout
        return SimpleNamespace(state='ready' if endpoint is not None else 'absent', endpoint=endpoint)
    monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint', fake_discover)


def _owner_connection(store, *, operator=True, native=True):
    """The control-socket connection: the native local operator, acting as the socket peer."""
    identity = {'user_id': PEER_SUBJECT, 'provider': 'local', 'profile_id': store.authority.profile_id,
                'instance_id': store.authority.instance_id, 'native_bootstrap': native,
                'capabilities': ['session:create', 'session:read', 'session:submit', 'session:control']}
    return AuthorityConnection(store.authority, object(), identity, operator=operator)


def _owner_socket(store, calls):
    """Dial the owner's real dispatch, the way the control socket does."""
    from hermes_cli.gateway_client import rpc_error

    @asynccontextmanager
    async def connect(endpoint=None, subject=None):
        connection = _owner_connection(store)
        calls['endpoint'] = endpoint
        calls['subject'] = subject

        async def rpc(method, _timeout=None, **params):
            calls['method'] = method
            calls['params'] = params
            frame = await connection.dispatch({'id': 1, 'method': method, 'params': params})
            if 'error' in frame:
                raise rpc_error(frame['error'])
            return frame['result']
        try:
            yield SimpleNamespace(rpc=rpc)
        finally:
            await connection.close()
    return connect


def _forbid_the_offline_lock(monkeypatch):
    """The offline reservation must never be asked for: the owner is the only writer."""
    from hermes_cli import web_server_sessions

    def refuse(*args, **kwargs):
        raise AssertionError('the offline maintenance lock must not be taken: the owner owns the store')
    monkeypatch.setattr(web_server_sessions, '_with_session_maintenance', refuse)


@pytest.mark.asyncio
async def test_prune_reaches_the_live_owner_and_deletes_the_stored_row(
        owner_store, prune_app, monkeypatch):
    """The owner deletes the cold stored row - no 403, no 409, no second writer."""
    from hermes_cli import gateway_client

    calls = {}
    _discover(monkeypatch, _endpoint(owner_store.authority.profile_id))
    monkeypatch.setattr(gateway_client, 'connect_gateway', _owner_socket(owner_store, calls))
    _forbid_the_offline_lock(monkeypatch)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=prune_app),
                                 base_url='http://localhost') as client:
        response = await client.post('/api/sessions/prune', json={})

    assert response.status_code == 200, response.text
    assert response.json() == {'ok': True, 'removed': 1, 'skipped_open': 1}
    # The canonical owner verb, not a new one.
    assert calls['method'] == 'session.prune'
    # The cold stored row owned by the dashboard principal is gone; the open row and the
    # recent row survive.
    assert owner_store.db.get_session('cold-desktop') is None
    assert owner_store.db.get_session('open-desktop') is not None
    assert owner_store.db.get_session('fresh-cli') is not None


@pytest.mark.asyncio
async def test_the_stored_row_is_one_a_per_row_owner_check_would_refuse(owner_store):
    """The row really is foreign-owned, so its deletion proves the verb is store-wide.

    ``authorize_history`` (the single-session path's rule) compares the actor subject
    against the row's stored owner, so the peer subject is refused on this row. A bulk
    prune must not inherit that rule.
    """
    from hermes_state_mutation_binding import authorize_history
    from hermes_state_runtime import RuntimeStoreError

    connection = _owner_connection(owner_store)
    try:
        with owner_store.db._read_ctx() as conn:
            with pytest.raises(RuntimeStoreError) as refusal:
                authorize_history(conn, connection.actor, 'cold-desktop')
        assert refusal.value.reason == 'permission_denied'
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_prune_forwards_the_translated_filters(owner_store, prune_app, monkeypatch):
    """The HTTP model becomes the owner's filter dict, faithfully."""
    from hermes_cli import gateway_client

    calls = {}

    @asynccontextmanager
    async def connect(endpoint=None, subject=None):
        calls['endpoint'] = endpoint

        async def rpc(method, _timeout=None, **params):
            calls['method'] = method
            calls['params'] = params
            return {'deleted': 0}
        yield SimpleNamespace(rpc=rpc)

    _discover(monkeypatch, _endpoint(owner_store.authority.profile_id))
    monkeypatch.setattr(gateway_client, 'connect_gateway', connect)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=prune_app),
                                 base_url='http://localhost') as client:
        response = await client.post('/api/sessions/prune', json={'source': 'cli'})

    assert response.status_code == 200, response.text
    assert calls['method'] == 'session.prune'
    filters = calls['params']['filters']
    # An attribute filter suppresses the implicit 90-day cutoff unless it was sent.
    assert filters['older_than_days'] is None
    assert filters['source'] == 'cli'
    # Archived handling, the window, and every unset filter travel as the direct path's.
    assert filters['archived'] is False
    assert filters['started_before'] is None and filters['started_after'] is None
    assert filters['min_messages'] is None and filters['title_like'] is None
    # Every key is one the owner accepts.
    from hermes_state_maintenance import _PRUNE_FILTER_NAMES
    assert set(filters) <= set(_PRUNE_FILTER_NAMES) | {'older_than_days'}


@pytest.mark.asyncio
async def test_a_bare_prune_keeps_the_implicit_ninety_day_window(owner_store, prune_app, monkeypatch):
    from hermes_cli import gateway_client

    calls = {}

    @asynccontextmanager
    async def connect(endpoint=None, subject=None):
        async def rpc(method, _timeout=None, **params):
            calls['params'] = params
            return {'deleted': 0}
        yield SimpleNamespace(rpc=rpc)

    _discover(monkeypatch, _endpoint(owner_store.authority.profile_id))
    monkeypatch.setattr(gateway_client, 'connect_gateway', connect)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=prune_app),
                                 base_url='http://localhost') as client:
        response = await client.post('/api/sessions/prune', json={})

    assert response.status_code == 200, response.text
    assert calls['params']['filters']['older_than_days'] == 90


@pytest.mark.asyncio
async def test_the_dry_run_stays_a_local_read_only_preview(owner_store, prune_app, monkeypatch):
    """A dry run is a preview: it never reaches the owner's write verb."""
    from hermes_cli import gateway_client

    @asynccontextmanager
    async def connect(endpoint=None, subject=None):
        raise AssertionError('a dry run must not dial the owner')
        yield  # pragma: no cover

    _discover(monkeypatch, _endpoint(owner_store.authority.profile_id))
    monkeypatch.setattr(gateway_client, 'connect_gateway', connect)
    _forbid_the_offline_lock(monkeypatch)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=prune_app),
                                 base_url='http://localhost') as client:
        response = await client.post('/api/sessions/prune', json={'dry_run': True})

    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview['ok'] is True and preview['removed'] == 0
    assert preview['matched'] == 1
    assert preview['skipped_open'] == 1
    assert [row['id'] for row in preview['sessions']] == ['cold-desktop']
    # A preview deletes nothing.
    assert owner_store.db.get_session('cold-desktop') is not None


@pytest.mark.asyncio
async def test_a_non_owner_without_a_discoverable_owner_is_still_refused(
        owner_store, prune_app, monkeypatch):
    """The guard is unchanged: no owner to route to, the reservation still refuses."""
    from gateway.runtime_ownership import ProfileOwnership

    _discover(monkeypatch, None)
    reservation = ProfileOwnership()
    reservation.reserve([owner_store.home])
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=prune_app),
                                     base_url='http://localhost') as client:
            response = await client.post('/api/sessions/prune', json={})
    finally:
        reservation.close()

    assert response.status_code == 409, response.text
    assert 'maintenance' in response.json()['detail'].lower()
    # The refused prune changed nothing.
    assert owner_store.db.get_session('cold-desktop') is not None


@pytest.mark.asyncio
async def test_an_owner_refusal_answers_with_the_direct_paths_status(
        owner_store, prune_app, monkeypatch):
    """An owner verdict is a verdict: the same status the direct path would give."""
    from hermes_cli import gateway_client
    from hermes_cli.gateway_client import GatewayRPCError

    @asynccontextmanager
    async def connect(endpoint=None, subject=None):
        async def rpc(method, _timeout=None, **params):
            raise GatewayRPCError('permission_denied')
        yield SimpleNamespace(rpc=rpc)

    _discover(monkeypatch, _endpoint(owner_store.authority.profile_id))
    monkeypatch.setattr(gateway_client, 'connect_gateway', connect)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=prune_app),
                                 base_url='http://localhost') as client:
        response = await client.post('/api/sessions/prune', json={})

    assert response.status_code == 403, response.text
    assert response.json()['detail'] == 'permission_denied'


@pytest.mark.asyncio
async def test_a_gateway_serving_this_profile_prunes_its_own_store(
        owner_store, prune_app, monkeypatch):
    """A gateway that serves this profile IS the owner: it prunes its own store.

    Handing the write to ourselves would be a self-RPC over our own control socket, and a
    draining gateway would refuse its own verb while the direct path still works. The home
    this process holds is the one the direct path can still take, so the direct path stays.
    """
    from hermes_cli import gateway_client
    from gateway.runtime_ownership import process_ownership

    @asynccontextmanager
    async def connect(endpoint=None, subject=None):
        raise AssertionError('a gateway must not dial itself')
        yield  # pragma: no cover

    _discover(monkeypatch, _endpoint(owner_store.authority.profile_id))
    monkeypatch.setattr(gateway_client, 'connect_gateway', connect)
    # The runtime reservation a serving gateway holds for its own profile.
    process_ownership.reserve([owner_store.home])
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=prune_app),
                                     base_url='http://localhost') as client:
            response = await client.post('/api/sessions/prune', json={})
    finally:
        process_ownership.release(owner_store.home)

    assert response.status_code == 200, response.text
    assert response.json() == {'ok': True, 'removed': 1, 'skipped_open': 1}
    assert owner_store.db.get_session('cold-desktop') is None
    assert owner_store.db.get_session('open-desktop') is not None


@pytest.mark.asyncio
async def test_an_unreachable_owner_is_a_retryable_error(owner_store, prune_app, monkeypatch):
    """A dial that dies on an endpoint discovery just identified is not a verdict."""
    from hermes_cli import gateway_client

    @asynccontextmanager
    async def connect(endpoint=None, subject=None):
        raise TimeoutError('first dial outlived its budget')
        yield  # pragma: no cover

    _discover(monkeypatch, _endpoint(owner_store.authority.profile_id))
    monkeypatch.setattr(gateway_client, 'connect_gateway', connect)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=prune_app),
                                 base_url='http://localhost') as client:
        response = await client.post('/api/sessions/prune', json={})

    assert response.status_code == 503, response.text
    assert response.json()['detail'] == 'owner_unreachable'
