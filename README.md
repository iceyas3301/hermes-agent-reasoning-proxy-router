# Hermes Agent reasoning proxy router

A Hermes Agent gateway plugin that routes each incoming gateway message to a reasoning effort before the model call starts.

The point is simple: short chat should stay fast and cheap, while implementation, debugging, rollout, security, and multi-system work can get more reasoning headroom automatically.

This repository is a public, sanitized reference copy. It does not include tokens, private hostnames, chat IDs, logs, auth files, sessions, or any user-specific secrets.

## What it does

The plugin hooks into Hermes gateway events:

- `pre_gateway_dispatch`: classifies the incoming message and sets a session-scoped reasoning override.
- `post_llm_call`: stores a short-lived pending intent when the assistant asks for approval to proceed.

The gateway then builds the model request with the selected effort.

Effort levels used by the router:

```text
none < minimal < low < medium < high < xhigh
```

Provider request mapping is conservative: router-local `none` disables reasoning, and router-local `xhigh` is sent as provider-safe `high` unless Hermes adds native `xhigh` support for the active backend.

The default classifier is deterministic. It uses local regex and length rules, so normal chat does not pay for a classifier model call. Semantic classifier config keys are reserved for future use and are no-ops in this version.

## Live-style configuration

This is the sanitized shape of the setup this repo documents:

```yaml
agent:
  reasoning_effort: low

delegation:
  reasoning_effort: minimal

plugins:
  enabled:
    - reasoning-proxy-router

reasoning_proxy_router:
  enabled: true
  default: medium
  min: none
  max: xhigh
  low_char_limit: 80
  xhigh_high_match_threshold: 4
  pending_intent_enabled: true
  pending_intent_ttl_minutes: 30
  log_decisions: false
  decision_log: false
```

Why this split:

- The main agent can stay at `low` by default.
- Simple Telegram or gateway messages can route to `none` or `low`.
- Normal technical questions land around `medium`.
- Implementation, setup, debugging, ops, and verification route to `high`.
- Security, auth, credentials, rollback risk, architecture, or multi-system work routes to `xhigh`.
- Delegated workers stay separate through `delegation.reasoning_effort`.

## Routing behavior

Current deterministic rules:

- `thanks`, `ok`, `cool`, `nice`, `lol`: `none`, unless clamped higher.
- Quick factual questions like `what time` or `what is`: `low`.
- Technical feasibility questions involving source, config, plugin, hook, gateway, or Hermes changes: `medium`.
- Implementation, setup, debugging, ops, or verification requests: `high`.
- Security, auth, credentials, rollback risk, architecture, multi-system work, or 4+ high-signal categories: `xhigh`.
- Short unmatched messages under `low_char_limit`: `low`.
- Anything else: configured `default`, usually `medium`.

## Pending intent inheritance

This handles terse approvals.

Example:

1. User asks for a complex implementation.
2. Assistant asks, `Want me to proceed with implementing and testing it?`
3. The plugin stores a pending effort for that session.
4. User replies `yes` or `go ahead`.
5. The next turn inherits the stored effort instead of treating `yes` as a low-effort message.

Rejections like `no`, `cancel`, `stop`, or `not yet` do not consume the pending intent.

## Files

- `plugins/reasoning-proxy-router/__init__.py`: plugin implementation.
- `plugins/reasoning-proxy-router/plugin.yaml`: plugin metadata.
- `examples/reasoning_proxy_router.yaml`: sanitized config block.
- `docs/configuration.md`: full config notes and operational guidance.
- `docs/security.md`: what not to publish.

## Install into a Hermes checkout

Copy the plugin folder into your Hermes Agent checkout:

```bash
cp -R plugins/reasoning-proxy-router ~/.hermes/hermes-agent/plugins/
```

Enable it in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - reasoning-proxy-router
```

Add the `reasoning_proxy_router` block from `examples/reasoning_proxy_router.yaml`.

Restart the Hermes gateway only when you are ready for runtime changes to take effect.

## Verify

From this repository:

```bash
python -m pytest -q
```

To run the original focused Hermes checkout test, if you have the full Hermes Agent source tree available:

```bash
cd ~/.hermes/hermes-agent
./venv/bin/python -m pytest tests/plugins/test_reasoning_proxy_router.py -q -o 'addopts='
```

To verify the plugin is active at runtime, check the gateway or agent log for lines like this, using fake IDs in public docs:

```text
reasoning-proxy-router: session=agent:main:telegram:dm:USER_ID:THREAD_ID effort=medium reason=technical keyword
```

Do not publish real session keys or message text from your logs.

## What this plugin does not do

- It does not choose a different model or provider.
- It does not change `agent.reasoning_effort` permanently.
- It does not rewrite prompts.
- It does not change Telegram delivery or reply threading.
- It does not need Hermes core changes when the gateway hook path is available.
- It should not log full message content to public docs.

## Security note

Keep `.env`, `auth.json`, gateway logs, session files, chat IDs, hostnames, tokens, and account IDs out of public repositories. Use placeholders in docs and examples.

## License

MIT. See `LICENSE`.
