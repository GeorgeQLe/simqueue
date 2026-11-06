"""Independent processes must obey the same capacity and FIFO journal."""

import json
import multiprocessing
from pathlib import Path

from test_queue import Backend, Processes

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
        assert sum(r["state"] == "granted" for r in results) == 2
        assert sum(r["state"] == "queued" for r in results) == 4
        assert len(json.loads(inventory.read_text())) == 2
        q = Queue(root, FileBackend(inventory), Processes(), max_devices=2)
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
