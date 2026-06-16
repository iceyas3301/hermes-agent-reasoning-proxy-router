#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Install and configure the Hermes Agent reasoning proxy router.

Usage:
  ./scripts/install.sh [--profile PROFILE] [--hermes-home PATH] [--config PATH]

Defaults:
  --profile default
  --hermes-home "$HOME/.hermes"

What it does:
  1. Installs the plugin to $HERMES_HOME/plugins/reasoning-proxy-router.
  2. Enables reasoning-proxy-router in the target config's plugins.enabled list.
  3. Adds safe reasoning_proxy_router defaults without overwriting existing values.
  4. Writes backups before replacing an existing plugin or config file.

It does not restart Hermes. Start a new CLI session or restart the gateway when ready.
EOF
}

PROFILE="default"
HERMES_HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
CONFIG_PATH=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)
      PROFILE="${2:?missing value for --profile}"
      shift 2
      ;;
    --hermes-home)
      HERMES_HOME_DIR="${2:?missing value for --hermes-home}"
      shift 2
      ;;
    --config)
      CONFIG_PATH="${2:?missing value for --config}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
PLUGIN_SRC="$REPO_ROOT/plugins/reasoning-proxy-router"
PLUGIN_DST="$HERMES_HOME_DIR/plugins/reasoning-proxy-router"

if [[ ! -f "$PLUGIN_SRC/__init__.py" || ! -f "$PLUGIN_SRC/plugin.yaml" ]]; then
  echo "Plugin source not found at $PLUGIN_SRC" >&2
  exit 1
fi

if [[ -z "$CONFIG_PATH" ]]; then
  if [[ "$PROFILE" == "default" ]]; then
    CONFIG_PATH="$HERMES_HOME_DIR/config.yaml"
  else
    CONFIG_PATH="$HERMES_HOME_DIR/profiles/$PROFILE/config.yaml"
  fi
fi

STAMP="$(date +%Y%m%d_%H%M%S)"
mkdir -p "$(dirname "$PLUGIN_DST")" "$(dirname "$CONFIG_PATH")"

if [[ -e "$PLUGIN_DST" ]]; then
  BACKUP_DST="$PLUGIN_DST.backup.$STAMP"
  cp -a "$PLUGIN_DST" "$BACKUP_DST"
  echo "Backed up existing plugin: $BACKUP_DST"
fi
rm -rf "$PLUGIN_DST"
mkdir -p "$PLUGIN_DST"
cp "$PLUGIN_SRC/__init__.py" "$PLUGIN_SRC/plugin.yaml" "$PLUGIN_DST/"

export INSTALL_CONFIG_PATH="$CONFIG_PATH"
export STAMP
python3 - <<'PY'
from __future__ import annotations

import os
from pathlib import Path

try:
    import yaml
except ImportError as exc:
    raise SystemExit(
        "PyYAML is required to update Hermes config.yaml. Install it in the Hermes environment, then rerun this script."
    ) from exc

config_path = Path(os.environ["INSTALL_CONFIG_PATH"]).expanduser()
config_path.parent.mkdir(parents=True, exist_ok=True)
stamp = os.environ.get("STAMP", "")

if config_path.exists():
    backup = config_path.with_suffix(config_path.suffix + f".backup.{stamp or 'install'}")
    backup.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")
else:
    backup = None

if config_path.exists():
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
else:
    data = {}
if not isinstance(data, dict):
    raise SystemExit(f"Config file is not a YAML mapping: {config_path}")

plugins = data.setdefault("plugins", {})
if not isinstance(plugins, dict):
    plugins = {}
    data["plugins"] = plugins

enabled = plugins.get("enabled")
if enabled is None:
    enabled = []
elif not isinstance(enabled, list):
    enabled = [enabled]
if "reasoning-proxy-router" not in enabled:
    enabled.append("reasoning-proxy-router")
plugins["enabled"] = enabled

disabled = plugins.get("disabled")
if disabled is None:
    disabled = []
elif not isinstance(disabled, list):
    disabled = [disabled]
plugins["disabled"] = [item for item in disabled if item != "reasoning-proxy-router"]

router = data.setdefault("reasoning_proxy_router", {})
if not isinstance(router, dict):
    router = {}
    data["reasoning_proxy_router"] = router

defaults = {
    "enabled": True,
    "default": "medium",
    "min": "none",
    "max": "xhigh",
    "low_char_limit": 80,
    "xhigh_high_match_threshold": 4,
    "pending_intent_enabled": True,
    "pending_intent_ttl_minutes": 30,
    "pending_intent_max_entries": 512,
    "log_decisions": False,
    "decision_log": False,
}
for key, value in defaults.items():
    router.setdefault(key, value)

config_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
print(f"Updated config: {config_path}")
if backup:
    print(f"Backed up config: {backup}")
PY

echo "Installed plugin: $PLUGIN_DST"
echo "Enabled plugin in: $CONFIG_PATH"
echo "Next step: start a new Hermes CLI session or restart the gateway when you are ready."
