"""Reasoning proxy router.

A fast, fail-open Hermes gateway plugin that chooses per-session reasoning
levels for incoming messages. Deterministic heuristics run first. Semantic
classifier keys are reserved for future use and are intentionally no-ops today.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

logger = logging.getLogger(__name__)

EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")
PROVIDER_EFFORT_MAP = {"minimal": "minimal", "low": "low", "medium": "medium", "high": "high", "xhigh": "high"}
DEFAULT_CONFIG = {
    "enabled": True,
    "default": "medium",
    "min": "none",
    "max": "xhigh",
    "log_decisions": False,
    "decision_log": False,
    "decision_log_path": "logs/reasoning-proxy-router.jsonl",
    "low_char_limit": 80,
    "xhigh_high_match_threshold": 4,
    "pending_intent_enabled": True,
    "pending_intent_ttl_minutes": 30,
    "pending_intent_max_entries": 512,
    "semantic_classifier_enabled": False,
    "semantic_classifier_url": "http://127.0.0.1:8080/v1/chat/completions",
    "semantic_classifier_model": "gpt-5.4-mini",
    "semantic_classifier_api_key": "",
    "semantic_classifier_timeout_seconds": 3,
    "semantic_classifier_min_confidence": 0.8,
    "semantic_classifier_max_chars": 800,
}

_PENDING: dict[str, dict[str, Any]] = {}
_NONE_PATTERNS = [re.compile(r"^(?:thanks|thank you|ok|okay|cool|nice|lol|haha)[.!?\s]*$", re.I)]
_LOW_PATTERNS = [re.compile(r"\b(what time|what date|who is|what is)\b", re.I)]
_MEDIUM_TECH = [
    re.compile(r"\b(?:does|would|will|could|can)\b.{0,80}\b(?:require|need|support|involve)\b", re.I),
    re.compile(r"\b(?:source|code|config|configuration|plugin|hook|gateway|hermes)\b.{0,80}\b(?:require|need|support|possible|change)\b", re.I),
]
_HIGH_GROUPS = {
    "implementation": (re.compile(r"\b(implement|build|add|create|modify|patch|refactor)\b", re.I),),
    "setup": (re.compile(r"\b(set\s*up|setup|configure|install|enable|disable)\b", re.I),),
    "debug": (re.compile(r"\b(debug|fix|troubleshoot|investigate|why\s+did)\b", re.I),),
    "ops": (re.compile(r"\b(restart|deploy|rollback|service|systemd|auth|oauth)\b", re.I),),
    "verify": (re.compile(r"\b(test|verify|lint|smoke\s*test)\b", re.I),),
    "logging": (re.compile(r"\b(log|audit|jsonl)\b", re.I),),
}
_XHIGH_PATTERNS = [
    re.compile(r"\b(architecture|architectural|security|auth|oauth|credential|secret|rollback-safe|data\s+loss)\b", re.I),
    re.compile(r"\b(multi[-\s]?system|cross[-\s]?system|multiple\s+systems|orchestrat(?:e|ion))\b", re.I),
]
_AFFIRMATIVE = [
    re.compile(r"^(?:y|yes|yep|yeah|ok|okay|sure|go ahead|do it|proceed|ship it)[.!?\s]*$", re.I),
]
_REJECTION = [re.compile(r"^(?:n|no|nope|nah|cancel|stop|wait|not yet)[.!?\s]*$", re.I)]
_APPROVAL_REQUEST = [
    re.compile(r"\b(?:want me to|should I|shall I|do you want me to)\b.{0,140}\b(?:proceed|implement|build|create|patch|configure|install|test|verify|restart|deploy)\b", re.I),
]
_PROCEED = [
    *_APPROVAL_REQUEST,
    re.compile(r"\b(?:I can|I will|I’ll|I'll)\b.{0,140}\b(?:proceed|implement|build|create|patch|configure|install|test|verify|restart|deploy)\b", re.I),
]


def _home() -> Path:
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "y", "on"}:
            return True
        if v in {"0", "false", "no", "n", "off", "none", ""}:
            return False
    return default


def _int(value: Any, default: int, *, minimum: int = 0, maximum: int = 10000) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _effort_index(effort: str) -> int:
    return EFFORTS.index(effort) if effort in EFFORTS else EFFORTS.index("medium")


def _max_effort(*efforts: str) -> str:
    valid = [e for e in efforts if e in EFFORTS]
    return max(valid or ["medium"], key=_effort_index)


def _clamp(effort: str, config: Dict[str, Any]) -> str:
    if effort not in EFFORTS:
        effort = str(config.get("default", "medium"))
    if effort not in EFFORTS:
        effort = "medium"
    lo = str(config.get("min", "none"))
    hi = str(config.get("max", "xhigh"))
    if lo not in EFFORTS:
        lo = "none"
    if hi not in EFFORTS:
        hi = "xhigh"
    if EFFORTS.index(lo) > EFFORTS.index(hi):
        lo, hi = hi, lo
    idx = max(EFFORTS.index(lo), min(EFFORTS.index(hi), EFFORTS.index(effort)))
    return EFFORTS[idx]


def _provider_reasoning_config(effort: str) -> dict[str, Any]:
    if effort == "none":
        return {"enabled": False}
    return {"enabled": True, "effort": PROVIDER_EFFORT_MAP.get(effort, "medium")}


def _source_for_key(source: Any, gateway=None) -> Any:
    if gateway is not None and hasattr(gateway, "_normalize_source_for_session_key"):
        try:
            return gateway._normalize_source_for_session_key(source)
        except Exception:
            return source
    return source


def _session_key(source, gateway=None) -> str:
    source = _source_for_key(source, gateway)
    if gateway is not None and hasattr(gateway, "_session_key_for_source"):
        return gateway._session_key_for_source(source)
    platform = getattr(source, "platform", None)
    return f"{getattr(platform, 'value', 'unknown')}:{getattr(source, 'user_id', '')}:{getattr(source, 'chat_id', '')}:{getattr(source, 'thread_id', '') or ''}"


def _get_cfg(gateway=None) -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if gateway and isinstance(getattr(gateway, "config_data", None), dict):
        data = gateway.config_data.get("reasoning_proxy_router", {})
        if isinstance(data, dict):
            cfg.update(data)
    if yaml is not None:
        path = _home() / "reasoning-proxy-router" / "config.yaml"
        if path.exists():
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                if isinstance(data, dict):
                    cfg.update(data)
            except Exception:
                logger.exception("reasoning-proxy-router: config load failed")
    return cfg


def _decision_log_path(cfg: Dict[str, Any]) -> Path:
    raw = str(cfg.get("decision_log_path", "logs/reasoning-proxy-router.jsonl") or "logs/reasoning-proxy-router.jsonl")
    path = Path(raw)
    if path.is_absolute():
        path = Path(*path.parts[1:]) if len(path.parts) > 1 else Path("reasoning-proxy-router.jsonl")
    return _home() / path


def _write_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def _is_affirmative(text: str) -> bool:
    t = _normalize(text)
    return any(r.match(t) for r in _AFFIRMATIVE) or any(r.match(t) for r in _PROCEED)


def _is_rejection(text: str) -> bool:
    t = _normalize(text)
    return any(r.match(t) for r in _REJECTION)


def _pending_hit(key: str) -> Optional[dict[str, Any]]:
    item = _PENDING.get(key)
    if not item:
        return None
    ttl = timedelta(minutes=_int(item.get("ttl_minutes"), 30, minimum=1, maximum=1440))
    if datetime.now(timezone.utc) - item["ts"] > ttl:
        _PENDING.pop(key, None)
        return None
    return item


def _pop_pending(keys: list[str], pending: Optional[dict[str, Any]] = None) -> None:
    all_keys = set(keys)
    if pending:
        all_keys.update(pending.get("keys", []))
    for key in all_keys:
        _PENDING.pop(key, None)


def _pending_for(keys: list[str]) -> Optional[dict[str, Any]]:
    for key in keys:
        item = _pending_hit(key)
        if item:
            return item
    return None


def _prune_pending(max_entries: int) -> None:
    for key in list(_PENDING):
        _pending_hit(key)
    while len(_PENDING) > max_entries:
        oldest = min(_PENDING, key=lambda k: _PENDING[k].get("ts", datetime.now(timezone.utc)))
        _PENDING.pop(oldest, None)


def _store_pending(keys: list[str], effort: str, reason: str, ttl_minutes: int, max_entries: int = 512) -> None:
    _prune_pending(max_entries)
    payload = {"effort": effort, "reason": reason, "ts": datetime.now(timezone.utc), "ttl_minutes": ttl_minutes, "keys": list(dict.fromkeys(keys))}
    for key in keys:
        if key:
            _PENDING[key] = dict(payload)


def classify_message(text: str, config: Optional[Dict[str, Any]] = None) -> tuple[str, str]:
    cfg = dict(DEFAULT_CONFIG)
    if config:
        cfg.update(config)
    norm = _normalize(text)
    if not norm:
        return _clamp("low", cfg), "empty/no-op"
    if any(r.match(norm) for r in _NONE_PATTERNS):
        return _clamp("none", cfg), "no-op"
    hits = sum(1 for pats in _HIGH_GROUPS.values() for r in pats if r.search(norm))
    threshold = _int(cfg.get("xhigh_high_match_threshold"), 4, minimum=1, maximum=len(_HIGH_GROUPS))
    if any(r.search(norm) for r in _XHIGH_PATTERNS) or hits >= threshold:
        return _clamp("xhigh", cfg), "xhigh/risk or multi-category"
    if any(r.search(norm) for r in _MEDIUM_TECH):
        return _clamp("medium", cfg), "technical feasibility follow-up"
    if any(r.search(norm) for r in _LOW_PATTERNS):
        return _clamp("low", cfg), "quick factual question"
    high_groups = ("implementation", "setup", "debug", "ops", "verify")
    if any(r.search(norm) for group in high_groups for r in _HIGH_GROUPS[group]):
        return _clamp("high", cfg), "implementation/ops/verify"
    if len(norm) <= _int(cfg.get("low_char_limit"), 80, minimum=1, maximum=10000):
        return _clamp("low", cfg), "short/simple"
    return _clamp(str(cfg.get("default", "medium")), cfg), "default"


def _route(text: str, config: Dict[str, Any]) -> tuple[str, str]:
    return classify_message(text, config)


def _session_id_for(session_key: str, session_store=None) -> Optional[str]:
    if session_store is None:
        return None
    try:
        session_store._ensure_loaded()
        entry = getattr(session_store, "_entries", {}).get(session_key)
        return getattr(entry, "session_id", None) if entry else None
    except Exception:
        return None


def pre_gateway_dispatch(event, gateway=None, session_store=None, **_kwargs):
    try:
        cfg = _get_cfg(gateway)
        if not _bool(cfg.get("enabled"), True):
            return {"action": "allow"}
        source = event.source
        normalized_source = _source_for_key(source, gateway)
        session_key = _session_key(normalized_source, gateway)
        session_id = _session_id_for(session_key, session_store)
        pending_keys = [k for k in (str(session_id) if session_id else "", session_key) if k]
        text = getattr(event, "text", "") or ""
        pending = _pending_for(pending_keys) if _bool(cfg.get("pending_intent_enabled"), True) else None
        if pending and _is_rejection(text):
            _pop_pending(pending_keys, pending)
            effort, reason = _route(text, cfg)
        elif pending and _is_affirmative(text):
            effort = _clamp(str(pending["effort"]), cfg)
            reason = f"pending-intent:{pending['reason']}"
            _pop_pending(pending_keys, pending)
        else:
            effort, reason = _route(text, cfg)
        if gateway is not None and hasattr(gateway, "_set_session_reasoning_override"):
            gateway._set_session_reasoning_override(session_key, _provider_reasoning_config(effort))
        if _bool(cfg.get("log_decisions"), False):
            logger.info("reasoning-proxy-router: session=%s effort=%s reason=%s", session_key, effort, reason)
        if _bool(cfg.get("decision_log"), False):
            platform = getattr(getattr(normalized_source, "platform", None), "value", None)
            decision = {"ts": datetime.now(timezone.utc).isoformat(), "session_key": session_key, "platform": platform, "effort": effort, "provider_effort": _provider_reasoning_config(effort), "reason": reason}
            _write_jsonl(_decision_log_path(cfg), decision)
        return {"action": "allow"}
    except Exception:
        logger.exception("reasoning-proxy-router: dispatch failure")
        return {"action": "allow"}


def _asks_for_approval(text: str) -> bool:
    norm = _normalize(text)
    return "?" in norm and any(r.search(norm) for r in _APPROVAL_REQUEST)


def post_llm_call(*, session_id: str = "", user_message: str | None = None, assistant_response: str | None = None, gateway=None, session_key: str = "", **_kwargs):
    try:
        cfg = _get_cfg(gateway)
        if not _bool(cfg.get("pending_intent_enabled"), True):
            return None
        if not assistant_response or not _asks_for_approval(assistant_response):
            return None
        user_effort, user_reason = classify_message(user_message or "", cfg)
        assistant_effort, assistant_reason = classify_message(assistant_response, cfg)
        effort = _max_effort(user_effort, assistant_effort)
        if _effort_index(effort) < _effort_index("high"):
            effort = "high"
            reason = "implementation approval"
        else:
            reason = assistant_reason if _effort_index(assistant_effort) >= _effort_index(user_effort) else user_reason
        keys = [k for k in (session_id, session_key) if k]
        if keys:
            ttl = _int(cfg.get("pending_intent_ttl_minutes"), 30, minimum=1, maximum=1440)
            max_entries = _int(cfg.get("pending_intent_max_entries"), 512, minimum=1, maximum=100000)
            _store_pending(keys, effort, reason, ttl, max_entries)
        return None
    except Exception:
        logger.exception("reasoning-proxy-router: post_llm_call failure")
        return None


def reasoning_proxy_router_command(raw_args: str = "") -> str:
    cfg = _get_cfg()
    cmd = (raw_args or "status").strip().split()
    action = cmd[0].lower() if cmd else "status"
    if action == "status":
        return json.dumps({"ok": True, "enabled": _bool(cfg.get("enabled"), True), "default": cfg.get("default"), "min": cfg.get("min"), "max": cfg.get("max"), "semantic": "reserved"})
    if action == "test":
        message = " ".join(cmd[1:])
        effort, reason = classify_message(message, cfg)
        return json.dumps({"ok": True, "message": message, "effort": effort, "provider_reasoning": _provider_reasoning_config(effort), "reason": reason})
    return json.dumps({"ok": False, "error": f"unknown action: {action}", "usage": "status | test <message>"})


def register(ctx) -> None:
    ctx.register_hook("pre_gateway_dispatch", pre_gateway_dispatch)
    ctx.register_hook("post_llm_call", post_llm_call)
    ctx.register_command("reasoning-proxy-router", reasoning_proxy_router_command, description="Route reasoning effort for incoming gateway messages", args_hint="status | test <message>")
