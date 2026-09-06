"""The trigger port with the ePWM adapter: tools/trigger.sh drives EHRPWM0_A on J28.29. Edge capture lives
in the recorder; epoch, K and edges_seen in TriggerStatus are the recorder's latest status values."""
import subprocess

from .pb import pb


class EpwmTrigger:
    def __init__(self, script):
        self.script = script
        self.settings = pb.TriggerSettings(mode=pb.TriggerSettings.LOCAL, armed=False, period_ns=33_333_333, low_ns=5_000_000)

    def available(self):
        """True when the pwmchip is there (the pin overlay is applied); false on the PC."""
        try:
            return subprocess.run(["sh", self.script, "status"], capture_output=True, timeout=5).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def read(self):
        """The settings as the PWM hardware actually has them, so a change made outside this agent (or made
        before it started) is reported, not the last value it wrote. Falls back to the cache if the read fails."""
        try:
            r = subprocess.run(["sh", self.script, "status"], capture_output=True, text=True, timeout=5)
            if r.returncode != 0:
                return self.settings
            kv = dict(w.split("=", 1) for w in r.stdout.split() if "=" in w)
            period, duty = int(kv["period"]), int(kv["duty_cycle" if "duty_cycle" in kv else "duty"])
        except (OSError, subprocess.TimeoutExpired, KeyError, ValueError):
            return self.settings
        if period <= 0:
            return self.settings
        self.settings = pb.TriggerSettings(mode=pb.TriggerSettings.LOCAL, armed=kv.get("enable") == "1",
                                           period_ns=period, low_ns=duty)
        return self.settings

    def apply(self, s):
        """Arm or disarm; returns the effective settings. Raises ValueError on a rejected setting."""
        mode = s.mode or self.settings.mode
        if mode != pb.TriggerSettings.LOCAL:
            raise ValueError("this node generates the pulse itself (trigger.local); EXTERNAL is not declared")
        period = s.period_ns or self.settings.period_ns
        low = s.low_ns or self.settings.low_ns
        if s.armed:
            fps = round(1e9 / period)
            r = subprocess.run(["sh", self.script, "on", str(fps), "%.3f" % (low / 1e6)], capture_output=True, text=True, timeout=10)
        else:
            r = subprocess.run(["sh", self.script, "off"], capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            raise ValueError((r.stderr or r.stdout).strip() or "trigger.sh failed")
        self.settings = pb.TriggerSettings(mode=mode, armed=s.armed, period_ns=period, low_ns=low)
        return self.settings
