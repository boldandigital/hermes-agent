"""The gateway runtime can take exclusive maintenance on the profile it owns.

``flock`` is bound to the open file description, not the process: a second descriptor on an
inode this process already holds conflicts EVEN WITHIN ONE PROCESS. The gateway runtime holds its
profile's ``gateway.lock`` for its whole life through the module-global ``process_ownership``, so
before the fix its own in-process web handler could never take the reservation on its own store
and every session mutation was refused forever.

These tests pin the fixed behaviour AND the guard that must survive it: a DIFFERENT process
holding the home is still refused, and still told it is a gateway.
"""
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[2]

# A real SEPARATE process holding the home, reserving through the same primitive the gateway
# runtime uses. In-process reuse must never paper over this.
_OTHER_PROCESS_HOLDER = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from gateway.runtime_ownership import process_ownership
home = Path(sys.argv[1])
process_ownership.reserve([home])
print('held', flush=True)
sys.stdin.readline()
process_ownership.release(home)
"""


@pytest.mark.platforms("posix")
def test_runtime_owner_takes_maintenance_on_its_own_home(tmp_path):
    """The exact failure: reserve the home the way the gateway runtime does, then take
    maintenance on that same home in the same process. This raised OwnershipConflict."""
    from gateway import runtime_ownership

    home = tmp_path / 'gateway-home'
    runtime_ownership.process_ownership.reserve([home])
    assert runtime_ownership.process_reserves(home) is runtime_ownership.process_ownership
    try:
        with runtime_ownership.exclusive_maintenance([home]):
            pass  # the owner's own store is writable by the owner
    finally:
        runtime_ownership.process_ownership.release(home)
    assert runtime_ownership.process_reserves(home) is None


@pytest.mark.platforms("posix")
def test_reused_reservation_leaves_the_runtime_lock_record_intact(tmp_path):
    """Reuse must not rewrite ``gateway.lock``: ``live_runtime_owner()`` and gateway discovery
    read that record, and a maintenance-marked one would make the owner look absent."""
    from gateway import runtime_ownership

    home = tmp_path / 'gateway-home'
    runtime_ownership.process_ownership.reserve([home])
    record = json.loads((home / 'gateway.lock').read_text(encoding='utf-8'))
    assert 'purpose' not in record, 'a runtime reservation is not a maintenance reservation'
    try:
        with runtime_ownership.exclusive_maintenance([home]):
            after = json.loads((home / 'gateway.lock').read_text(encoding='utf-8'))
        assert after == record, 'maintenance clobbered the runtime lock record'
        assert runtime_ownership.live_runtime_owner(home) is not None
    finally:
        runtime_ownership.process_ownership.release(home)


@pytest.mark.platforms("posix")
def test_a_different_process_is_still_refused_and_still_named_a_gateway(tmp_path):
    """The cross-process guard is load-bearing: reuse is for THIS process only."""
    from gateway.runtime_ownership import OwnershipConflict, exclusive_maintenance

    home = tmp_path / 'gateway-home'
    holder = subprocess.Popen([sys.executable, '-c', _OTHER_PROCESS_HOLDER, str(home), str(ROOT)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=str(ROOT))
    try:
        assert holder.stdout.readline().strip() == 'held'
        with pytest.raises(OwnershipConflict) as refusal:
            with exclusive_maintenance([home]):
                pytest.fail('a second process must never take maintenance on an owned home')
        assert 'gateway' in str(refusal.value).lower()
        assert 'stop the gateway' in str(refusal.value)
    finally:
        holder.stdin.close()
        holder.wait(timeout=20)
    # Once the other process is gone the home is reservable again.
    with exclusive_maintenance([home]):
        pass


@pytest.mark.platforms("posix")
def test_sibling_maintenance_thread_is_refused_without_a_gateway_remedy(tmp_path):
    """Reuse gives maintenance no flock of its own, so it is serialised explicitly: a concurrent
    sibling thread is refused rather than run twice, and is NOT told to stop the gateway."""
    from gateway import runtime_ownership

    home = tmp_path / 'gateway-home'
    runtime_ownership.process_ownership.reserve([home])
    entered, refusals = threading.Event(), []

    def sibling():
        try:
            with runtime_ownership.exclusive_maintenance([home]):
                entered.set()
        except runtime_ownership.OwnershipConflict as exc:
            refusals.append(str(exc))

    try:
        with runtime_ownership.exclusive_maintenance([home]):
            worker = threading.Thread(target=sibling)
            worker.start()
            worker.join(timeout=20)
        assert not worker.is_alive()
        assert not entered.is_set(), 'a sibling maintenance step ran concurrently'
        assert len(refusals) == 1, refusals
        # Assert the misleading REMEDY is gone (the path may legitimately contain "gateway").
        assert 'stop the gateway' not in refusals[0].lower(), (
            'no other process is involved, so naming the gateway is a lie')
        # Nested maintenance in the SAME thread still reuses the outer reservation.
        with runtime_ownership.exclusive_maintenance([home]):
            with runtime_ownership.exclusive_maintenance([home]):
                pass
    finally:
        runtime_ownership.process_ownership.release(home)


@pytest.mark.platforms("posix")
def test_the_real_web_maintenance_path_succeeds_on_a_home_this_process_owns(tmp_path, monkeypatch):
    """Driven through the real caller: ``_with_session_maintenance`` used to answer 409 here."""
    from gateway import runtime_ownership
    from hermes_cli import web_server_cron, web_server_sessions

    home = tmp_path / 'gateway-home'
    runtime_ownership.process_ownership.reserve([home])
    monkeypatch.setattr(web_server_cron, '_cron_profile_home', lambda profile: ('p', home))
    try:
        assert web_server_sessions._with_session_maintenance(
            'p', lambda: 'MUTATED') == 'MUTATED'
    finally:
        runtime_ownership.process_ownership.release(home)


@pytest.mark.platforms("posix")
def test_fresh_in_process_holder_is_not_treated_as_runtime_owner(tmp_path):
    """The reuse grant must be restricted to the runtime singleton. Any other
    ``ProfileOwnership`` in this process is a *test or sibling maintenance* holder; granting
    it maintenance turns a live runtime owner's refusal into a silent destructive bulk-write
    (the B1 regression pair: ``test_maintenance_routes_refuse_live_owner_without_changing_rows``
    and ``test_import_and_startup_exclude_each_other_before_publication`` both simulate the
    holder with a fresh ``ProfileOwnership`` and assert the refusal). Falsified: with the
    runtime-singleton check reverted to ``is not None``, the runtime control below ALSO
    bypasses the refusal and this test stops biting — exactly the bug being fixed."""
    from gateway import runtime_ownership
    from gateway.runtime_ownership import OwnershipConflict

    home = tmp_path / 'gateway-home'
    foreign = runtime_ownership.ProfileOwnership()
    foreign.reserve([home])
    try:
        # A non-runtime in-process holder must still be refused with the gateway-named remedy.
        with pytest.raises(OwnershipConflict) as refused:
            with runtime_ownership.exclusive_maintenance([home]):
                pytest.fail('a non-runtime in-process holder was treated as the runtime owner')
        assert 'gateway' in str(refused.value).lower()
        assert 'stop the gateway' in str(refused.value), refused.value
    finally:
        foreign.release(home)

    # Control on a SEPARATE home: the runtime singleton IS reused (the bug being fixed).
    runtime_home = tmp_path / 'runtime-home'
    runtime_ownership.process_ownership.reserve([runtime_home])
    try:
        with runtime_ownership.exclusive_maintenance([runtime_home]):
            pass
    finally:
        runtime_ownership.process_ownership.release(runtime_home)


@pytest.mark.platforms("posix")
def test_depth_three_nesting_succeeds_when_set_grows_at_depth_two(tmp_path):
    """``exclusive_maintenance`` writes ``_maintenance.state`` on every frame (not only the
    outermost) so a home acquired at depth 2 is in ``held`` when a depth-3 frame tries to
    acquire its per-home ``threading.Lock``. Without the fix, depth 3 re-acquires a
    ``threading.Lock`` the current thread already holds (``threading.Lock`` is not reentrant,
    so ``acquire(blocking=False)`` returns False on the owning thread), and the code mistakes
    that self-acquire for a sibling-thread contention and raises ``_InProcessMaintenanceBusy``
    against itself. Reproduced A/B against BASE: ``[h],[h,h2],[h,h2]`` and
    ``[h],[h,h2],[h,h2,h3],[h,h2,h3]`` SUCCEED at BASE and REFUSE at HEAD without this fix.
    Falsified: with the ``state_held`` write reduced to the outermost branch only, the same
    sets raise ``_InProcessMaintenanceBusy`` and both tests go red."""
    from contextlib import ExitStack
    from gateway import runtime_ownership

    h1 = tmp_path / 'home-1'
    h2 = tmp_path / 'home-2'
    h3 = tmp_path / 'home-3'

    # Shape A: depth 2 grows, depth 3 repeats.
    with ExitStack() as stack:
        stack.enter_context(runtime_ownership.exclusive_maintenance([h1]))
        stack.enter_context(runtime_ownership.exclusive_maintenance([h1, h2]))
        stack.enter_context(runtime_ownership.exclusive_maintenance([h1, h2]))

    # Shape B: depth 3 grows too.
    with ExitStack() as stack:
        stack.enter_context(runtime_ownership.exclusive_maintenance([h1]))
        stack.enter_context(runtime_ownership.exclusive_maintenance([h1, h2]))
        stack.enter_context(runtime_ownership.exclusive_maintenance([h1, h2, h3]))
        stack.enter_context(runtime_ownership.exclusive_maintenance([h1, h2, h3]))


@pytest.mark.platforms("posix")
def test_depth_three_nesting_succeeds_for_the_runtime_owner(tmp_path):
    """The B2 fix applies to the runtime-owner path as well: ``process_ownership`` owning one
    home and a depth-3 maintenance over the SAME home plus a newly-added one must SUCCEED. The
    depth-2 frame acquires the new home's ``threading.Lock``; a depth-3 frame must see that
    lock in ``held`` (per-frame ``state_held`` write) and reuse it, not re-acquire it. Pins
    that the fix did not narrow to foreign holders only, and that the runtime owner's flock
    and the thread-local ``held`` union stay consistent across nested frames."""
    from contextlib import ExitStack
    from gateway import runtime_ownership

    home = tmp_path / 'gateway-home'
    sibling = tmp_path / 'sibling-home'
    runtime_ownership.process_ownership.reserve([home])
    try:
        with ExitStack() as stack:
            stack.enter_context(runtime_ownership.exclusive_maintenance([home]))
            stack.enter_context(runtime_ownership.exclusive_maintenance([home, sibling]))
            stack.enter_context(runtime_ownership.exclusive_maintenance([home, sibling]))
    finally:
        runtime_ownership.process_ownership.release(home)


@pytest.mark.platforms("posix")
def test_depth_three_self_busy_does_not_block_a_sibling_thread(tmp_path):
    """The depth-3 ``held`` union is per-thread, so it must not silently exclude a *different*
    thread's maintenance step on one of the same homes. The sibling-thread BUSY path still
    fires (this is the serialisation against itself), but the cause is the sibling holding
    the per-home ``threading.Lock``, not this thread's own self-acquire."""
    from gateway import runtime_ownership

    home = tmp_path / 'gateway-home'
    runtime_ownership.process_ownership.reserve([home])
    completed = threading.Event()
    refused = []

    def sibling():
        try:
            with runtime_ownership.exclusive_maintenance([home]):
                completed.set()
        except runtime_ownership.OwnershipConflict as exc:
            refused.append(str(exc))

    try:
        with runtime_ownership.exclusive_maintenance([home]):
            worker = threading.Thread(target=sibling)
            worker.start()
            worker.join(timeout=20)
        assert not worker.is_alive()
        assert not completed.is_set(), 'sibling maintenance ran concurrently with the runtime owner'
        assert len(refused) == 1, refused
        # Self-busy paths raise ``_InProcessMaintenanceBusy``, which the outer handler must
        # NOT re-wrap in "stop the gateway" — that would be a lie (no other process owns it).
        assert 'stop the gateway' not in refused[0].lower(), refused[0]
    finally:
        runtime_ownership.process_ownership.release(home)