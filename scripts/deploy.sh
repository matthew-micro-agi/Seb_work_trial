#!/bin/sh -e
# Push this workspace to the EVM over SSH. No serial console, no re-flash.
#
#   scripts/deploy.sh [host]              repo -> /opt/ti_workspace (tracked + untracked, .gitignore honoured), VERSION stamp
#   scripts/deploy.sh [host] --dtbo       + dts/*.dtbo -> /boot/dtb/ti/   (needs a reboot to apply)
#   scripts/deploy.sh [host] --ko FILE    + FILE -> /lib/modules/<uname -r>/kernel/drivers/media/i2c/, depmod
#   scripts/deploy.sh [host] --reboot     reboot at the end and wait for SSH to come back
#
# host defaults to "evm" (see ~/.ssh/config). The board has no rsync, so files travel as a tar stream;
# the repo is small enough that this takes about a second.
HOST=evm
DTBO=0; KO=""; REBOOT=0
for a in "$@"; do
  case "$a" in
    --dtbo) DTBO=1 ;;
    --reboot) REBOOT=1 ;;
    --ko) KO=next ;;
    *) if [ "$KO" = next ]; then KO=$a; else HOST=$a; fi ;;
  esac
done
cd "$(dirname "$0")/.."
REV=$(git rev-parse --short HEAD 2>/dev/null || echo untracked)

git ls-files -co --exclude-standard | tar czf - -T - | ssh "$HOST" "mkdir -p /opt/ti_workspace && tar xzf - -C /opt/ti_workspace 2>/dev/null; echo $REV > /opt/ti_workspace/VERSION"
echo "repo -> /opt/ti_workspace ($REV)"
# no RTC and no NTP on the bench: the board wakes up in 1970, which ruins every log timestamp
ssh "$HOST" "date -u -s '$(date -u '+%Y-%m-%d %H:%M:%S')' >/dev/null" && echo "board clock set from this PC"

if [ $DTBO = 1 ]; then
  tar czf - dts/*.dtbo | ssh "$HOST" "tar xzf - -C /boot/dtb/ti --strip-components=1 && ls -l /boot/dtb/ti/*camsync* /boot/dtb/ti/*imx296*"
  echo "overlays -> /boot/dtb/ti (reboot to apply; enable in uEnv.txt name_overlays)"
fi

if [ -n "$KO" ] && [ "$KO" != next ]; then
  NAME=$(basename "$KO" | sed 's/-6\.6\..*\.ko$/.ko/')
  ssh "$HOST" "D=/lib/modules/\$(uname -r)/kernel/drivers/media/i2c; mkdir -p \$D; cat > \$D/$NAME && depmod -a && md5sum \$D/$NAME" < "$KO"
  md5sum "$KO"
fi

if [ $REBOOT = 1 ]; then
  ssh "$HOST" reboot || true
  echo -n "rebooting"; sleep 15
  for i in $(seq 1 40); do ssh -o ConnectTimeout=2 "$HOST" true 2>/dev/null && { echo " up"; ssh "$HOST" "uptime; cat /opt/ti_workspace/VERSION"; exit 0; }; echo -n .; sleep 3; done
  echo " no SSH after 2 min"; exit 1
fi
