"""Best-effort queue observations. Sampling is outside the coordinator lock."""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path


def observe(event, **fields):
    if sys.platform != "darwin":
        return
    launcher = Path.home() / ".local/bin/developer-host"
    payload = json.dumps(
        {
            "source": "simqueue",
            "event": event,
            "scope": "default",
            "event_ms": time.time_ns() // 1_000_000,
            **fields,
        }
    ).encode()
    try:
        worker = subprocess.Popen(
            [launcher, "queue-metrics"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return
    try:
        with worker.stdin as pipe:
            pipe.write(payload)
    except OSError:
        pass
    threading.Thread(target=worker.wait, daemon=True).start()
