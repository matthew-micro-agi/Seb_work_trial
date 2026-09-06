"""Trigger source: EHRPWM0 output B via sysfs. On the J722S EVM that is J28 pin 32.

  python3 -m camsync.pwm on [--fps 30] [--pulse-ms 1] [--inverted]
  python3 -m camsync.pwm off

For a sensor whose exposure is the trigger pulse width (IMX296 in trigger mode) --pulse-ms
is the exposure time and --inverted selects an active-low pulse.
"""
import argparse
import glob
import os

PWM_DEVICE = "23000000.pwm"   # epwm0 on J722S / AM62P
CHANNEL = 1                    # output B


def _chip():
    for d in glob.glob("/sys/class/pwm/pwmchip*"):
        if os.path.basename(os.path.realpath(os.path.join(d, "device"))) == PWM_DEVICE:
            return d
    raise FileNotFoundError("no pwmchip for %s; is the camsync overlay loaded?" % PWM_DEVICE)


def _write(path, value):
    with open(path, "w") as f:
        f.write(str(value))


def configure(enable, fps=30.0, pulse_ms=1.0, inverted=False):
    chip = _chip()
    ch = "%s/pwm%d" % (chip, CHANNEL)
    if not os.path.isdir(ch):
        _write(chip + "/export", CHANNEL)
    period_ns = round(1e9 / fps)
    duty_ns = round(pulse_ms * 1e6)
    _write(ch + "/enable", 0)
    _write(ch + "/polarity", "inversed" if inverted else "normal")
    _write(ch + "/duty_cycle", 0)       # duty must never exceed period while changing
    _write(ch + "/period", period_ns)
    _write(ch + "/duty_cycle", duty_ns)
    _write(ch + "/enable", 1 if enable else 0)
    print("%s: period %d ns, pulse %d ns, %s, enable %d"
          % (ch, period_ns, duty_ns, "active-low" if inverted else "active-high", enable))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("state", choices=["on", "off"])
    p.add_argument("--fps", type=float, default=30.0)
    p.add_argument("--pulse-ms", type=float, default=1.0)
    p.add_argument("--inverted", action="store_true")
    a = p.parse_args()
    configure(a.state == "on", a.fps, a.pulse_ms, a.inverted)


if __name__ == "__main__":
    main()
