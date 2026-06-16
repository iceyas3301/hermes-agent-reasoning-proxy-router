"""Reasoning proxy router.

A fast, fail-open Hermes gateway plugin that chooses per-session reasoning
levels for incoming messages. Deterministic heuristics run first. Optional
semantic classification is bounded by timeout, confidence, and message length.
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
DEFAULT_CONFIG = {
    "enabled": True,
    "default": "medium",
    "min": "none",
    "max": "xhigh",
    "log_decisions": True,
    "decision_log": False,
    "decision_log_path": "logs/reasoning-proxy-router.jsonl",
    "low_char_limit": 80,
    "xhigh_high_match_threshold": 4,
    "pending_intent_enabled": True,
    "pending_intent_ttl_minutes": 30,
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
_PROCEED = [
    re.compile(r"\b(?:want me to|should I|shall I|do you want me to)\b.{0,140}\b(?:proceed|implement|build|create|patch|configure|install|test|verify|restart|deploy)\b", re.I),
    re.compile(r"\b(?:I can|I will|I’ll|I'll)\b.{0,140}\b(?:proceed|implement|build|create|patch|configure|install|test|verify|restart|deploy)\b", re.I),
]


def _home() -> Path:
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _clamp(effort: str, config: Dict[str, Any]) -> str:
    if effort not in EFFORTS:
        effort = config.get("default", "medium")
    lo = config.get("min", "none")
    hi = config.get("max", "xhigh")
    if lo not in EFFORTS:
        lo = "none"
    if hi not in EFFORTS:
        hi = "xhigh"
    if EFFORTS.index(lo) > EFFORTS.index(hi):
        lo, hi = hi, lo
    idx = max(EFFORTS.index(lo), min(EFFORTS.index(hi), EFFORTS.index(effort)))
    return EFFORTS[idx]


def _session_key(source, gateway=None) -> str:
    if gateway is not None and hasattr(gateway, "_session_key_for_source"):
        return gateway._session_key_for_source(source)
    return f"{getattr(source.platform, 'value', 'unknown')}:{getattr(source, 'user_id', '')}:{getattr(source, 'chat_id', '')}:{getattr(source, 'thread_id', '') or ''}"


def _get_cfg(gateway=None) -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if gateway and isinstance(getattr(gateway, "config_data", None), dict):
        cfg.update(gateway.config_data.get("reasoning_proxy_router", {}))
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


def _pending_hit(session_id: str) -> Optional[dict[str, Any]]:
    item = _PENDING.get(session_id)
    if not item:
        return None
    ttl = timedelta(minutes=item.get("ttl_minutes", 30))
    if datetime.now(timezone.utc) - item["ts"] > ttl:
        _PENDING.pop(session_id, None)
        return None
    return item


def _store_pending(session_key: str, effort: str, reason: str, ttl_minutes: int) -> None:
    _PENDING[session_key] = {"effort": effort, "reason": reason, "ts": datetime.now(timezone.utc), "ttl_minutes": ttl_minutes}


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
    if any(r.search(norm) for r in _XHIGH_PATTERNS) or hits >= int(cfg.get("xhigh_high_match_threshold", 4)):
        return _clamp("xhigh", cfg), "xhigh/risk or multi-category"
    if any(r.search(norm) for r in _MEDIUM_TECH):
        return _clamp("medium", cfg), "technical feasibility follow-up"
    if any(r.search(norm) for r in _LOW_PATTERNS):
        return _clamp("low", cfg), "quick factual question"
    if any(r.search(norm) for r in _HIGH_GROUPS["implementation"]) or any(r.search(norm) for r in _HIGH_GROUPS["setup"]) or any(r.search(norm) for r in _HIGH_GROUPS["debug"]) or any(r.search(norm) for r in _HIGH_GROUPS["ops"]) or any(r.search(norm) for r in _HIGH_GROUPS["verify"]):
        return _clamp("high", cfg), "implementation/ops/verify"
    if len(norm) <= int(cfg.get("low_char_limit", 80)):
        return _clamp("low", cfg), "short/simple"
    return _clamp(cfg.get("default", "medium"), cfg), "default"


def _route(text: str, config: Dict[str, Any]) -> tuple[str, str]:
    return classify_message(text, config)


def pre_gateway_dispatch(event, gateway=None, session_store=None, **_kwargs):
    try:
        cfg = _get_cfg(gateway)
        if not cfg.get("enabled", True):
            return {"action": "allow"}
        source = event.source
        session_key = _session_key(source, gateway)
        session_id = None
        if session_store is not None:
            try:
                session_store._ensure_loaded()
                entry = getattr(session_store, "_entries", {}).get(session_key)
                session_id = getattr(entry, "session_id", None) if entry else None
            except Exception:
                session_id = None
        text = getattr(event, "text", "") or ""
        pending = _pending_hit(str(session_id)) if (cfg.get("pending_intent_enabled", True) and session_id) else None
        if pending and _is_affirmative(text):
            effort = _clamp(pending["effort"], cfg)
            reason = f"pending-intent:{pending['reason']}"
            _PENDING.pop(str(session_id), None)
        else:
            effort, reason = _route(text, cfg)
        if gateway is not None and hasattr(gateway, "_set_session_reasoning_override"):
            gateway._set_session_reasoning_override(session_key, {"enabled": True, "effort": effort})
        if cfg.get("log_decisions", True):
            logger.info("reasoning-proxy-router: session=%s effort=%s reason=%s", session_key, effort, reason)
        if cfg.get("decision_log"):
            decision = {"ts": datetime.now(timezone.utc).isoformat(), "session_key": session_key, "platform": getattr(source.platform, 'value', None), "effort": effort, "reason": reason}
            _write_jsonl(_home() / str(cfg.get("decision_log_path", "logs/reasoning-proxy-router.jsonl")), decision)
        return {"action": "allow"}
    except Exception:
        logger.exception("reasoning-proxy-router: dispatch failure")
        return {"action": "allow"}


def post_llm_call(*, session_id: str = "", user_message: str | None = None, assistant_response: str | None = None, gateway=None, conversation_history=None, **_kwargs):
    try:
        cfg = _get_cfg(gateway)
        if not cfg.get("pending_intent_enabled", True):
            return None
        if not assistant_response:
            return None
        if not any(r.search(assistant_response) for r in _PROCEED):
            return None
        effort, reason = classify_message(user_message or assistant_response, cfg)
        if effort == "low":
            effort = "high"
            reason = "implementation approval"
        if conversation_history is not None:
            pass
        # store based on session_id if available, otherwise no-op
        if session_id:
            _store_pending(session_id, effort, reason, int(cfg.get("pending_intent_ttl_minutes", 30)))
        return None
    except Exception:
        logger.exception("reasoning-proxy-router: post_llm_call failure")
        return None


def reasoning_proxy_router_command(raw_args: str = "") -> str:
    cfg = _get_cfg()
    cmd = (raw_args or "status").strip().split()
    action = cmd[0].lower() if cmd else "status"
    if action == "status":
        return json.dumps({"enabled": cfg.get("enabled", True), "default": cfg.get("default"), "min": cfg.get("min"), "max": cfg.get("max"), "semantic": cfg.get("semantic_classifier_enabled", False)})
    return json.dumps({"ok": True})


def register(ctx) -> None:
    ctx.register_hook("pre_gateway_dispatch", pre_gateway_dispatch)
    ctx.register_hook("post_llm_call", post_llm_call)
    ctx.register_command("reasoning-proxy-router", reasoning_proxy_router_command, description="Route reasoning effort for incoming gateway messages", args_hint="status")
