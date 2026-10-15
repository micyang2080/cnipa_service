# -*- coding: utf-8 -*-
import signal
import sys

from service import config
from service import runner_invoke
from service.app import app


def _stop_uia(*_a):
    try:
        runner_invoke.kill_current_uia()
    except Exception:
        pass
    try:
        runner_invoke.kill_all_uia_runners()
    except Exception:
        pass


def _on_signal(signum, frame):
    _stop_uia()
    sys.exit(128 + (signum if isinstance(signum, int) else 2))


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    try:
        app.run(host=config.listen_host(), port=config.listen_port(), threaded=True)
    finally:
        _stop_uia()
