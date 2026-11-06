import json
import os
import uuid

import pytest

from simqueue.backend import Attention
from simqueue.core import Queue


class Backend:
    def __init__(self):
        self.items = {}
        self.deleted = []
        self.fail_create = False
        self.fail_delete = False
        self.fail_inventory = False

    def validate(self, device_type, runtime):
        if (device_type, runtime) != ("phone", "ios"):
            raise ValueError("Unavailable runtime or device type")

    def inventory(self):
        return {"devices": self.devices()}

    def devices(self):
        if self.fail_inventory:
            raise Attention("Inventory unavailable")
        return dict(self.items)

    def create(self, name, device_type, runtime):
        uid = str(uuid.uuid4()).upper()
        self.items[uid] = {
            "udid": uid,
            "name": name,
            "runtime": runtime,
            "deviceTypeIdentifier": device_type,
            "state": "Shutdown",
        }
        if self.fail_create:
            raise Attention("Create timed out after side effect")
        return uid

    def data_exists(self, uid):
        return uid in self.items

    def boot(self, uid):
        self.items[uid]["state"] = "Booted"

    def shutdown(self, uid):
        self.items[uid]["state"] = "Shutdown"

    def delete(self, uid):
        if self.fail_delete:
            raise Attention("Deletion blocked")
        self.deleted.append(uid)
        del self.items[uid]


class Processes:
    def __init__(self):
        self.dead = set()
        self.unknown = set()

    def fingerprint(self, pid):
        return None if pid in self.dead else f"start-{pid}"

    def alive(self, pid, stamp):
        if pid in self.unknown:
            raise Attention("Process identity unavailable")
        return pid not in self.dead and stamp == self.fingerprint(pid)


@pytest.fixture
def setup(tmp_path):
    b, p, now = Backend(), Processes(), [1000]
    q = Queue(tmp_path / "state", b, p, clock=lambda: now[0], max_devices=1)
    return q, b, p, now


def acquire(q, **kwargs):
    return q.acquire("agent", "test", "phone", "ios", **kwargs)


def test_fifo_shared_last_holder_and_boot(setup):
    q, b, _, _ = setup
    first = acquire(q, share=True)
    joined = q.join(first["share_token"], "other", "test")
    second, third = acquire(q), acquire(q)
    assert (second["queue_position"], third["queue_position"]) == (1, 2)
    q.boot(first["lease_token"])
    assert b.items[first["uuid"]]["state"] == "Booted"
    q.release(first["lease_token"])
    assert first["uuid"] in b.items
    q.release(joined["lease_token"])
    assert first["uuid"] in b.deleted
    assert q.poll(second["lease_token"])["state"] == "granted"
    assert q.poll(third["lease_token"])["state"] == "queued"
    q.release(second["lease_token"])
    q.release(third["lease_token"])
    assert not b.items


def test_capabilities_redacted_cancel_idempotent(setup):
    q, b, _, _ = setup
    a = acquire(q, share=True)
    queued = acquire(q)
    q.release(queued["lease_token"])
    q.release(queued["lease_token"])
    status = json.dumps(q.status())
    assert a["lease_token"] not in status and a["share_token"] not in status
    with pytest.raises(ValueError):
        q.release("wrong")
    assert len(b.items) == 1
    q.release(a["lease_token"])
    with pytest.raises(ValueError):
        q.join(a["share_token"], "late", "test")


def test_expiry_live_unknown_dead_owners(setup):
    q, b, p, now = setup
    a = acquire(q, ttl=60)
    now[0] += 61
    assert q.poll(a["lease_token"])["state"] == "stale-held"
    p.unknown.add(os.getpid())
    assert q.poll(a["lease_token"])["state"] == "stale-held"
    assert b.items
    p.unknown.clear()
    p.dead.add(os.getpid())
    assert q.poll(a["lease_token"])["state"] == "expired"
    assert not b.items


def test_registered_process_holds_after_release(setup):
    q, b, p, _ = setup
    a = acquire(q)
    q.track_process(a["lease_token"], 1234)
    result = q.release(a["lease_token"])
    assert result["attention"] and b.items
    p.dead.add(1234)
    q.status(sweep=True)
    assert not b.items


def test_uncertain_create_reconciles_without_duplicate(setup):
    q, b, _, _ = setup
    b.fail_create = True
    a = acquire(q)
    assert a["state"] == "creating" and len(b.items) == 1
    b.fail_create = False
    assert q.poll(a["lease_token"])["state"] == "granted"
    assert len(b.items) == 1
    q.release(a["lease_token"])
    assert not b.items


def test_unresolved_intent_reserves_capacity(setup):
    q, b, _, _ = setup

    def uncertain(*args):
        raise Attention("No known outcome")

    b.create = uncertain
    a = acquire(q)
    other = acquire(q)
    assert a["state"] == "creating" and other["state"] == "queued"
    assert q.release(a["lease_token"])["attention"]
    assert q.poll(other["lease_token"])["state"] == "queued"


def test_delete_failure_retry_and_identity_protection(setup):
    q, b, _, _ = setup
    a = acquire(q)
    b.items[a["uuid"]]["name"] = "regular working device"
    result = q.release(a["lease_token"])
    assert "identity changed" in result["attention"] and not b.deleted
    b.items[a["uuid"]]["name"] = q.status()["devices"][0]["name"]
    b.fail_delete = True
    assert q.poll(a["lease_token"])["attention"]
    b.fail_delete = False
    assert q.poll(a["lease_token"])["device_state"] == "deleted"


def test_inventory_failure_returns_capability_and_heartbeat(setup):
    q, b, _, now = setup
    b.fail_inventory = True
    a = acquire(q)
    assert a["lease_token"] and a["attention"]
    b.fail_inventory = False
    now[0] += 30
    result = q.poll(a["lease_token"], renew=True)
    assert result["expires"] == now[0] + 900 and result["state"] == "granted"


def test_cross_instance_and_corrupt_state(setup):
    q, b, p, now = setup
    a = acquire(q)
    other = Queue(q.root, b, p, clock=lambda: now[0], max_devices=1)
    assert other.poll(a["lease_token"])["uuid"] == a["uuid"]
    path = q.root / "state.json"
    state = json.loads(path.read_text())
    state["devices"][next(iter(state["devices"]))]["name"] = "ordinary device"
    path.write_text(json.dumps(state))
    with pytest.raises(Attention):
        other.release(a["lease_token"])
    assert not b.deleted


def test_expired_creating_request_cleanup(setup):
    q, b, p, now = setup
    b.fail_create = True
    a = acquire(q, ttl=60)
    now[0] += 61
    p.dead.add(os.getpid())
    assert q.poll(a["lease_token"])["state"] == "expired"
    assert not b.items


def test_symlink_and_capacity_mismatch(setup, tmp_path):
    q, b, p, _ = setup
    a = acquire(q)
    with pytest.raises(Attention):
        Queue(q.root, b, p, max_devices=2).status(sweep=True)
    link = tmp_path / "link"
    link.symlink_to(q.root)
    with pytest.raises(Attention):
        Queue(link, b, p).status(sweep=True)
    assert len(b.items) == 1
    q.release(a["lease_token"])


def test_create_uuid_known_but_inventory_uncertain(setup):
    q, b, _, _ = setup
    original = b.create

    def create(*args):
        uid = original(*args)
        b.fail_inventory = True
        return uid

    b.create = create
    a = acquire(q)
    assert a["state"] == "creating"
    b.fail_inventory = False
    assert q.poll(a["lease_token"])["state"] == "granted"
    assert len(b.items) == 1 and not b.deleted
    q.release(a["lease_token"])


def test_registered_process_unknown_and_pid_reuse(setup):
    q, b, p, _ = setup
    a = acquire(q)
    q.track_process(a["lease_token"], 1234)
    p.unknown.add(1234)
    assert q.release(a["lease_token"])["attention"]
    assert b.items
    p.unknown.clear()
    p.fingerprint = lambda pid: "new-start"
    q.status(sweep=True)
    assert not b.items


def test_prune_retains_active_and_unresolved_records(setup):
    q, _b, _, now = setup
    a = acquire(q)
    q.release(a["lease_token"])
    now[0] += 10 * 86400
    assert not q.status()["requests"]
    assert not q.status()["devices"]


def test_shutdown_not_confirmed_never_deletes_booted_device(setup):
    q, b, _, _ = setup
    a = acquire(q)
    q.boot(a["lease_token"])
    b.shutdown = lambda uid: None
    result = q.release(a["lease_token"])
    assert "not shut down" in result["attention"]
    assert a["uuid"] in b.items and not b.deleted


def test_missing_device_with_remaining_data_blocks_capacity(setup):
    q, b, _, _ = setup
    a = acquire(q)
    del b.items[a["uuid"]]
    b.data_exists = lambda uid: True
    result = q.release(a["lease_token"])
    assert "directory remains" in result["attention"]
    assert acquire(q)["state"] == "queued"


def test_sweeper_skips_protected_ready_devices(setup):
    q, b, _, _ = setup
    held = acquire(q)
    b.fail_inventory = True
    result = q.status(sweep=True)
    assert result["attention"] is None
    assert result["requests"][0]["state"] == "granted"
    b.fail_inventory = False
    assert q.release(held["lease_token"])["device_state"] == "deleted"
