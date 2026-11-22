"""Per-test watchdog (Debian's python3-pytest has no timeout plugin)."""
import faulthandler
import os
import signal
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

TIMEOUT = int(os.environ.get("RB_TEST_TIMEOUT", "150"))


@pytest.fixture(autouse=True)
def _watchdog():
    def expired(signum, frame):
        faulthandler.dump_traceback(all_threads=True)
        raise TimeoutError(f"test exceeded {TIMEOUT} s")
    old = signal.signal(signal.SIGALRM, expired)
    signal.alarm(TIMEOUT)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, old)
