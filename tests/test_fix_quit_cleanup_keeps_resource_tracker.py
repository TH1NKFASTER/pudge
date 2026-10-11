"""Quit cleanup leaves multiprocessing's resource tracker running (behaviour).

Pudge stops every descendant of its process on quit, and tests run that cleanup inside
pytest. On macOS the resource tracker is one of those descendants: killing it made
multiprocessing warn "resource_tracker: process died unexpectedly" and print a KeyError
traceback at exit."""

import subprocess
import sys
import warnings
from multiprocessing import resource_tracker

from pudge.process_cleanup import OwnedProcessTree


def test_quit_cleanup_stops_children_but_not_the_resource_tracker():
    resource_tracker.ensure_running()
    tracker = resource_tracker._resource_tracker._pid
    leftover = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        OwnedProcessTree().stop(grace=0.2)

        assert leftover.wait(timeout=5) is not None
        with warnings.catch_warnings():
            # A killed tracker is relaunched with exactly that warning.
            warnings.simplefilter("error")
            resource_tracker.ensure_running()
        assert resource_tracker._resource_tracker._pid == tracker
    finally:
        if leftover.poll() is None:
            leftover.kill()
            leftover.wait(timeout=5)
