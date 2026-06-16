# Configuration

This document explains the reasoning proxy router setup in public-safe form.

## One-step install

From a local clone:

```bash
git clone https://github.com/iceyas3301/hermes-agent-reasoning-proxy-router.git
cd hermes-agent-reasoning-proxy-router
./scripts/install.sh
```

The installer copies the plugin to `~/.hermes/plugins/reasoning-proxy-router`, enables it in the target config, adds missing `reasoning_proxy_router` defaults, and writes backups first. Use `./scripts/install.sh --profile my-profile` for a named profile.

It does not restart Hermes. Start a new CLI session or restart the gateway when you are ready.

## Required plugin enablement

The plugin must be listed under `plugins.enabled` in the active Hermes profile config:

```yaml
plugins:
  enabled:
    - reasoning-proxy-router
```

If you edit the active gateway profile, the gateway usually needs a restart before code or plugin-list changes load. Do not restart production gateways casually. Confirm you are editing the right profile first.

## Main router block

```yaml
reasoning_proxy_router:
  enabled: true
  default: medium
  min: none
  max: xhigh
  low_char_limit: 80
  xhigh_high_match_threshold: 4
  pending_intent_enabled: true
  pending_intent_ttl_minutes: 30
  pending_intent_max_entries: 512
  log_decisions: false
  decision_log: false
```

### `enabled`

Turns routing on or off. When disabled, the hook returns `allow` and does not set a reasoning override.

### `default`

The fallback effort when the deterministic classifier does not match a clearer rule. `medium` is a useful default for technical chats because it avoids under-routing ambiguous config or setup questions.

### `min` and `max`

Clamp every decision. Examples:

- `min: low` prevents anything from routing to `none` or `minimal`.
- `max: high` prevents `xhigh` even for security or architecture prompts.

Use clamps when you want a predictable operating envelope.

### `low_char_limit`

Short unmatched messages route to `low`. The documented setup uses `80`, which keeps small follow-ups cheap without dropping technical keyword matches.

### `xhigh_high_match_threshold`

The classifier counts high-signal categories such as implementation, setup, debugging, ops, verification, and logging. If enough groups hit, the request routes to `xhigh`. The documented setup uses `4`.

### `pending_intent_enabled`

Enables terse approval inheritance. This is useful on Telegram, where users often reply with `yes`, `ok`, or `go ahead` after the assistant asks whether to proceed.

### `pending_intent_ttl_minutes`

How long an approval intent stays valid. The documented setup uses `30` minutes.

### `pending_intent_max_entries`

Caps the in-memory pending-intent store. Expired entries are pruned opportunistically before new entries are stored. The documented setup uses `512`.

### `log_decisions`

Writes concise routing decisions to normal Hermes logs. These lines include the session key, which may contain platform/user/chat/thread identifiers. Leave this off unless you are debugging locally.

### `decision_log`

Optional JSONL audit trail. Keep it disabled unless debugging. If enabled, avoid committing the output file. It can expose session keys and routing metadata.

## Reserved semantic classifier settings

The plugin includes reserved config keys for a future semantic classifier:

```yaml
reasoning_proxy_router:
  semantic_classifier_enabled: false
  semantic_classifier_url: http://127.0.0.1:8080/v1/chat/completions
  semantic_classifier_model: gpt-5.4-mini
  semantic_classifier_api_key: ""
  semantic_classifier_timeout_seconds: 3
  semantic_classifier_min_confidence: 0.8
  semantic_classifier_max_chars: 800
```

These keys are no-ops in the current version. Keep them disabled unless you implement the semantic path yourself. The deterministic path is the normal path.

Never commit a real `semantic_classifier_api_key`. Prefer environment-backed secret loading if you extend this path.

## Baseline reasoning settings

The router is separate from global reasoning defaults. Router-local `none` disables reasoning in the provider request, and router-local `xhigh` maps to provider-safe `high` unless Hermes adds native `xhigh` support for the active backend.

```yaml
agent:
  reasoning_effort: low

delegation:
  reasoning_effort: minimal
```

`agent.reasoning_effort` is the main chat baseline. The router can set a session-scoped override for a gateway turn.

`delegation.reasoning_effort` controls spawned workers. The router does not automatically tune subagents. Keep those settings deliberate.

## Runtime flow

1. Gateway receives a message.
2. Hermes fires `pre_gateway_dispatch`.
3. The plugin loads config.
4. The classifier picks an effort and reason.
5. The result is clamped between `min` and `max`.
6. The plugin maps router-local effort to provider-safe reasoning config and calls `_set_session_reasoning_override(session_key, reasoning_config)`.
7. Gateway dispatch continues with `{"action": "allow"}`.

The plugin is fail-open. If it breaks, messages should still go through.

## Public-safe log example

```text
reasoning-proxy-router: session=agent:main:telegram:dm:USER_ID:THREAD_ID effort=medium reason=technical keyword
```

Use placeholders for IDs and omit message text.
