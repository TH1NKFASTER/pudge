"""The iPhone/iPad companion calls live in pudge/web_app_iphone.py (IPhoneApiMixin, a base of
WebAppApi; audit 72). The window still reaches every one of them by the same name through the
js bridge, and calling one through the bridge runs it on the API."""

from __future__ import annotations

from pudge import js_bridge, web_app_iphone
from pudge.web_app import WebAppApi

IPHONE_CALLS = {
    "iphone_enable",
    "iphone_disable",
    "iphone_status",
    "iphone_start_pairing",
    "iphone_cancel_pairing",
    "iphone_revoke_device",
    "iphone_approve_pairing",
    "iphone_deny_pairing",
    "iphone_resume_on_mac",
    "iphone_cancel_handoff",
    "audiobook_use_iphone_position",
}


def _bridge(api: WebAppApi) -> dict[str, object]:
    return {function.__name__: function for function in js_bridge.bridge_functions(api)}


def test_the_companion_calls_come_from_the_mixin() -> None:
    assert issubclass(WebAppApi, web_app_iphone.IPhoneApiMixin)
    for name in IPHONE_CALLS:
        assert name in vars(web_app_iphone.IPhoneApiMixin), name
        assert name not in vars(WebAppApi), name


def test_the_window_bridge_still_exposes_every_companion_call() -> None:
    api = WebAppApi.__new__(WebAppApi)
    functions = _bridge(api)
    assert IPHONE_CALLS <= set(functions)
    # Private helpers of the mixin stay out of the page's reach.
    assert not any(name.startswith("_") for name in functions)


def test_a_bridge_call_runs_the_mixin_method_on_the_api() -> None:
    api = WebAppApi.__new__(WebAppApi)
    functions = _bridge(api)
    assert functions["iphone_cancel_pairing"]() == {"ok": True}
    assert functions["iphone_resume_on_mac"](7, "op-1") == {"status": "disabled"}
    assert functions["iphone_cancel_handoff"]("op-1") == {"ok": True}
    assert functions["audiobook_use_iphone_position"](7, "checkpoint", 1) == {"status": "disabled"}
