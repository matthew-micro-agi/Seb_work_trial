#!/bin/sh
# Run on the EVM after booting with the camsync overlay.
echo "== overlay symbols (expect camsync_epwm0_pins_default, camsync_gpio0_pins_default)"
ls /proc/device-tree/__symbols__/ | grep camsync || echo "  camsync overlay NOT applied: check name_overlays in uEnv.txt"
echo "== pad registers (expect f41b8 -> 00010002, f4088/f4094/f40ac -> 00040007, f40a8 -> 00010007)"
mount | grep -q debugfs || mount -t debugfs none /sys/kernel/debug
grep -iE 'f4(1b8|088|094|0a8|0ac) ' /sys/kernel/debug/pinctrl/f4000.pinctrl-pinctrl-single/pins
echo "== pwm chips (expect one -> 23000000.pwm)"
for c in /sys/class/pwm/pwmchip*; do echo "  $c -> $(basename "$(readlink -f "$c/device")")"; done
echo "== gpio chips (expect a chip labelled 600000.gpio)"
gpiodetect 2>/dev/null || ls -l /sys/bus/gpio/devices/
