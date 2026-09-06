#!/bin/sh -e
# Usage: scripts/install_on_evm.sh <evm-ip>
# Copies the repo to /opt/camsync on the EVM, installs the pin overlay and enables it in uEnv.txt.
IP=${1:?usage: $0 <evm-ip>}
cd "$(dirname "$0")/.."
ssh root@"$IP" mkdir -p /opt/camsync
scp -rq camsync tests scripts dts README.md root@"$IP":/opt/camsync/
ssh root@"$IP" sh -e <<'REMOTE'
cp /opt/camsync/dts/k3-j722s-evm-camsync.dtbo /boot/dtb/ti/
ENV=$(ls /run/media/BOOT-mmcblk*p1/uEnv.txt /boot/uEnv.txt 2>/dev/null | head -1)
[ -n "$ENV" ] || { echo "uEnv.txt not found: add name_overlays=k3-j722s-evm-camsync.dtbo by hand"; exit 1; }
if grep -q camsync "$ENV"; then :
elif grep -q '^name_overlays=' "$ENV"; then
  PREFIX=$(grep '^name_overlays=' "$ENV" | grep -q 'ti/' && echo ti/ || true)
  sed -i "s|^name_overlays=.*|& ${PREFIX}k3-j722s-evm-camsync.dtbo|" "$ENV"
else
  echo 'name_overlays=k3-j722s-evm-camsync.dtbo' >> "$ENV"
fi
echo "$ENV:"; grep name_overlays "$ENV"
REMOTE
echo "installed. now: ssh root@$IP reboot"
