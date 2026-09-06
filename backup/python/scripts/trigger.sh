#!/bin/sh -e
# Usage: scripts/trigger.sh on|off [fps]
# Drives the PWM trigger (J28 pin 32). Before "on", a running binder is told to expect a new
# epoch so the first edge already lands in it.
cd "$(dirname "$0")/.."
[ "$1" = on ] && pkill -HUP -f 'camsync\.binder' || true
python3 -m camsync.pwm "$1" --fps "${2:-30}"
