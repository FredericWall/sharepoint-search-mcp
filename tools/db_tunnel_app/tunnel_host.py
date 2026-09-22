"""Idle process used only as a CF SSH endpoint for local database tunnels."""

from __future__ import annotations

import signal
import threading

stop = threading.Event()


def _stop(*_args) -> None:
    stop.set()


signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)
stop.wait()
