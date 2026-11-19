"""Independent processes must obey the same capacity and FIFO journal."""

import json
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from test_queue import Backend, Processes

from simqueue.backend import Attention
from simqueue.core import Queue


class FileBackend(Backend):
    def __init__(self, path):
        super().__init__()
        self.path = Path(path)

    def devices(self):
        self.items = json.loads(self.path.read_text()) if self.path.exists() else {}
        return dict(self.items)

    def create(self, *args):
        self.devices()
        result = super().create(*args)
        self.path.write_text(json.dumps(self.items))
        return result

    def delete(self, uid):
        self.devices()
        super().delete(uid)
        self.path.write_text(json.dumps(self.items))


def worker(root, inventory, ready, stop):
    q = Queue(root, FileBackend(inventory), Processes(), max_devices=2)
    ready.put(q.acquire("worker", "concurrent", "phone", "ios"))
    stop.wait(20)


def test_cross_process_lock_and_capacity(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    ready, stop = ctx.Queue(), ctx.Event()
    root, inventory = tmp_path / "state", tmp_path / "inventory.json"
    children = [ctx.Process(target=worker, args=(root, inventory, ready, stop)) for _ in range(6)]
    try:
        for child in children:
            child.start()
        results = [ready.get(timeout=15) for _ in children]
        q = Queue(root, FileBackend(inventory), Processes(), max_devices=2)
        results = [
            {**r, **q.poll(r["lease_token"])} if r["state"] == "queued" else r for r in results
        ]
        assert sum(r["state"] == "granted" for r in results) == 2
        assert sum(r["state"] == "queued" for r in results) == 4
        assert len(json.loads(inventory.read_text())) == 2
        for result in sorted(results, key=lambda r: r["queue_position"] or 0):
            q.release(result["lease_token"])
        assert not json.loads(inventory.read_text())
    finally:
        stop.set()
        for child in children:
            child.join(5)
            if child.is_alive():
                child.terminate()
                child.join()


@pytest.mark.parametrize("kind", ["boot", "install"])
def test_slow_native_operation_keeps_lease_controls_responsive(tmp_path, kind):
    entered, resume = Event(), Event()

    class SlowBackend(Backend):
        def block(self):
            entered.set()
            assert resume.wait(12)

        def boot(self, uid):
            self.block()
            super().boot(uid)

        def install(self, uid, path):
            self.block()
            return {"installed": True}

    backend = SlowBackend()
    queue = Queue(tmp_path / "queue", backend, Processes(), max_devices=1)
    peer = Queue(queue.root, backend, Processes(), max_devices=1)
    lease = queue.acquire("agent", "slow operation", "phone", "ios")
    token, uid = lease["lease_token"], lease["uuid"]
    args = (token,)
    if kind == "install":
        backend.items[uid]["state"] = "Booted"
        args += ("/example.app",)
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(getattr(queue, kind), *args)
        try:
            assert entered.wait(2)

            def controls():
                assert peer.status()["requests"][0]["operation"]["kind"] == kind
                assert queue.poll(token, renew=True)["state"] == "granted"
                with pytest.raises(Attention, match="already in progress"):
                    peer.boot(token)
                assert peer.release(token)["device_state"] == "ready"
                assert uid in backend.items

            pool.submit(controls).result(timeout=3)
        finally:
            resume.set()
        running.result(timeout=3)
    assert peer.poll(token)["device_state"] == "deleted"


def test_slow_create_keeps_existing_lease_controls_responsive(tmp_path):
    entered, resume = Event(), Event()
    backend = Backend()
    queue = Queue(tmp_path / "queue", backend, Processes(), max_devices=2)
    peer = Queue(queue.root, backend, Processes(), max_devices=2)
    lease = queue.acquire("agent", "existing lease", "phone", "ios")
    original_create = backend.create

    def slow_create(*args):
        entered.set()
        assert resume.wait(12)
        return original_create(*args)

    backend.create = slow_create
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(queue.acquire, "agent", "slow create", "phone", "ios")
        try:
            assert entered.wait(2)

            def controls():
                pending = next(r for r in peer.status()["requests"] if r["task"] == "slow create")
                assert pending["state"] == "creating"
                assert pending["operation"]["kind"] == "create"
                assert peer.poll(lease["lease_token"], renew=True)["state"] == "granted"

            pool.submit(controls).result(timeout=3)
        finally:
            resume.set()
        created = running.result(timeout=3)
    assert created["state"] == "granted"
    assert peer.release(created["lease_token"])["device_state"] == "deleted"
    assert peer.release(lease["lease_token"])["device_state"] == "deleted"


def test_late_create_is_cleaned_before_another_allocation(tmp_path):
    backend = Backend()
    queue = Queue(tmp_path / "queue", backend, Processes(), max_devices=2)
    original_create = backend.create
    pending = []

    def timeout(*args):
        pending.append(args)
        raise Attention("Create timed out")

    backend.create = timeout
    first = queue.acquire("agent", "timed-out create", "phone", "ios")
    assert first["state"] == "creating"
    assert queue.release(first["lease_token"])["device_state"] == "creating"
    second = queue.acquire("agent", "next allocation", "phone", "ios")
    assert second["state"] == "queued" and len(pending) == 1

    late_uuid = original_create(*pending[0])
    backend.create = original_create
    token = second["lease_token"]
    second = queue.poll(token)
    assert backend.deleted == [late_uuid]
    assert second["state"] == "granted" and second["uuid"] != late_uuid
    assert queue.release(token)["device_state"] == "deleted"
