"""The suite's own isolation: environment and pudge log handlers do not leak between tests."""

import logging
import os

_LEAK_ENV = "PUDGE_TESTS1_LEAK_PROBE"
_HANDLER_NAME = "tests1-leak-probe"


def test_1_leaves_an_env_var_and_a_log_handler_behind():
    os.environ[_LEAK_ENV] = "1"
    handler = logging.NullHandler()
    handler.set_name(_HANDLER_NAME)
    logging.getLogger("pudge").addHandler(handler)


def test_2_sees_neither():
    assert _LEAK_ENV not in os.environ
    assert all(h.get_name() != _HANDLER_NAME for h in logging.getLogger("pudge").handlers)
