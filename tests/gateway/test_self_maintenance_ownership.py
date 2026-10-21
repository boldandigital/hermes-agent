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