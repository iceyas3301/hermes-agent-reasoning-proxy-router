import importlib.util
from pathlib import Path
from types import SimpleNamespace

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "reasoning-proxy-router" / "__init__.py"
spec = importlib.util.spec_from_file_location("reasoning_proxy_router", PLUGIN)
assert spec is not None and spec.loader is not None
rpr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rpr)


class Platform:
    value = "telegram"


class Gateway:
    def __init__(self):
        self.config_data = {"reasoning_proxy_router": {}}
        self.overrides = {}

    def _session_key_for_source(self, source):
        return f"{source.platform.value}:{source.user_id}:{source.chat_id}:{source.thread_id or ''}"

    def _set_session_reasoning_override(self, session_key, reasoning_config):
        self.overrides[session_key] = reasoning_config


class NormalizingGateway(Gateway):
    def _normalize_source_for_session_key(self, source):
        return SimpleNamespace(**{**source.__dict__, "thread_id": "recovered"})


def source(text="hello", thread_id=None):
    return SimpleNamespace(
        platform=Platform(),
        user_id="USER_ID",
        chat_id="CHAT_ID",
        thread_id=thread_id,
        text=text,
    )


def event(text="hello", thread_id=None):
    return SimpleNamespace(text=text, source=source(text, thread_id))


def test_classifier_routes_core_efforts():
    assert rpr.classify_message("thanks")[0] == "none"
    assert rpr.classify_message("what time is it?")[0] == "low"
    assert rpr.classify_message("does the plugin require a config change?")[0] == "medium"
    assert rpr.classify_message("build and verify the setup")[0] == "high"
    assert rpr.classify_message("architect secure rollback auth multi-system deploy test config")[0] == "xhigh"


def test_bad_config_values_fall_back_safely():
    effort, _ = rpr.classify_message("build and test it", {"xhigh_high_match_threshold": "abc", "low_char_limit": None})
    assert effort == "high"
    assert rpr._bool("false", True) is False
    assert rpr._bool("true", False) is True


def test_provider_reasoning_maps_none_and_xhigh():
    assert rpr._provider_reasoning_config("none") == {"enabled": False}
    assert rpr._provider_reasoning_config("xhigh") == {"enabled": True, "effort": "high"}


def test_pre_gateway_dispatch_uses_normalized_session_key():
    gw = NormalizingGateway()
    rpr.pre_gateway_dispatch(event("build it", thread_id=None), gateway=gw)
    assert "telegram:USER_ID:CHAT_ID:recovered" in gw.overrides
    assert gw.overrides["telegram:USER_ID:CHAT_ID:recovered"] == {"enabled": True, "effort": "high"}


def test_disabled_string_false_is_honored():
    gw = Gateway()
    gw.config_data = {"reasoning_proxy_router": {"enabled": "false"}}
    assert rpr.pre_gateway_dispatch(event("build it"), gateway=gw) == {"action": "allow"}
    assert gw.overrides == {}


def test_pending_approval_inherits_and_rejection_clears():
    rpr._PENDING.clear()
    gw = Gateway()
    rpr.post_llm_call(session_id="s1", session_key="telegram:USER_ID:CHAT_ID:topic", user_message="can you set this up?", assistant_response="Should I proceed to implement and test it?")
    assert "s1" in rpr._PENDING
    rpr.pre_gateway_dispatch(event("no", thread_id="topic"), gateway=gw)
    assert not rpr._PENDING

    rpr.post_llm_call(session_id="s1", session_key="telegram:USER_ID:CHAT_ID:topic", user_message="can you set this up?", assistant_response="Should I proceed to implement and test it?")
    rpr.pre_gateway_dispatch(event("yes", thread_id="topic"), gateway=gw)
    assert gw.overrides["telegram:USER_ID:CHAT_ID:topic"] == {"enabled": True, "effort": "high"}
    assert not rpr._PENDING


def test_commitment_sentence_does_not_store_pending():
    rpr._PENDING.clear()
    rpr.post_llm_call(session_id="s1", user_message="build it", assistant_response="I will proceed to implement and test it.")
    assert not rpr._PENDING


def test_decision_log_relative_path_stays_under_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    gw = Gateway()
    gw.config_data = {"reasoning_proxy_router": {"decision_log": True, "decision_log_path": "/tmp/escape.jsonl"}}
    rpr.pre_gateway_dispatch(event("thanks"), gateway=gw)
    assert (tmp_path / "tmp" / "escape.jsonl").exists()
    assert not Path("/tmp/escape.jsonl").exists()


def test_command_status_test_and_unknown():
    status = rpr.json.loads(rpr.reasoning_proxy_router_command("status"))
    assert status["ok"] is True
    tested = rpr.json.loads(rpr.reasoning_proxy_router_command("test build it"))
    assert tested["effort"] == "high"
    unknown = rpr.json.loads(rpr.reasoning_proxy_router_command("bogus"))
    assert unknown["ok"] is False
