#!/bin/sh -e
# Trigger PWM on EHRPWM0_A (J28.29), active low, low time = exposure. Run ON THE EVM.
#
#   tools/trigger.sh on [fps] [exposure_ms]     default 30 fps, 5 ms
#   tools/trigger.sh off
#   tools/trigger.sh epoch [fps] [exposure_ms]  off, idle 1.2 s, on: the gap that opens a new epoch (PLAN §4)
#   tools/trigger.sh status
CHIP=$(for c in /sys/class/pwm/pwmchip*; do case "$(readlink -f $c/device)" in *23000000.pwm) echo $c;; esac; done | head -1)
[ -n "$CHIP" ] || { echo "no pwmchip for 23000000.pwm: is k3-j722s-evm-camsync-pins.dtbo in name_overlays?" >&2; exit 1; }
[ -d $CHIP/pwm0 ] || echo 0 > $CHIP/export
P=$CHIP/pwm0
case "${1:-status}" in
  on|epoch)
    FPS=${2:-30}; EXP=${3:-5}
    PERIOD=$((1000000000 / FPS)); DUTY=$(awk -v e=$EXP 'BEGIN{printf "%d", e*1000000}')
    [ $((DUTY + 16700000)) -lt $PERIOD ] || { echo "exposure + 16.7 ms readout must fit in the period ($PERIOD ns)" >&2; exit 1; }
    echo 0 > $P/enable 2>/dev/null || true
    [ "$1" = epoch ] && sleep 1.2
    # order matters: duty must fit the period at every step, and polarity only changes while disabled
    [ "$(cat $P/period)" != 0 ] && echo 0 > $P/duty_cycle
    echo $PERIOD > $P/period; echo inversed > $P/polarity; echo $DUTY > $P/duty_cycle; echo 1 > $P/enable
    echo "trigger on: $FPS Hz, low $EXP ms (J28.29)";;
  off) echo 0 > $P/enable; echo "trigger off";;
  status) echo "enable=$(cat $P/enable) period=$(cat $P/period) duty=$(cat $P/duty_cycle) polarity=$(cat $P/polarity)";;
  *) echo "usage: $0 on|off|epoch|status [fps] [exposure_ms]" >&2; exit 2;;
esac
