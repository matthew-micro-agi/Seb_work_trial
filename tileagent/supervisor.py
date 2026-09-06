"""The recorder supervisor: one object owns every spawn (DESIGN_RING_CONTROL.md §8.4). It tees the
recorder's stdout to logs/<session>.jsonl, parses the events, restarts a crashed recorder with the same
session id, and turns StopRecording into SIGTERM -> STOPPING -> IDLE without mistaking it for a crash.

`send(line)` is the other direction: one line down the recorder's stdin, the mirror of the JSON events it
writes to stdout. Two verbs today, `proxy on` / `proxy off` for the live-view valve (§8.3a) and `beacon
EPOCH K PTP_NS` for the trigger source's word on a pulse (tileagent/beacon.py). send() is the only writer
and a recorder that is not running just refuses it. Unknown lines are ignored by the recorder, so a verb
can be added without a version step."""
import json
import os
import signal
import subprocess
import sys
import threading
import time

IDLE, RUNNING, STOPPING, ERROR = "IDLE", "RUNNING", "STOPPING", "ERROR"
RESPAWN_DELAY_S = 2.0
MIN_LIFE_S = 10.0          # a respawn that dies sooner ends the session in ERROR
DRAIN_S = 3.0              # SIGTERM to SIGKILL


class Recorder:
    def __init__(self, log_dir, build_cmd, on_event, log=print):
        """build_cmd(session) -> (argv, env); on_event(dict) is called on the reader thread for every event."""
        self.log_dir = log_dir
        self.build_cmd = build_cmd
        self.on_event = on_event
        self.say = log
        self.lock = threading.RLock()
        self.state = IDLE
        self.session = None          # dict: id, start (request json dict), t_mono_start, ...
        self.proc = None
        self.reader = None
        self.logf = None
        self.status = {}             # the recorder's latest status event
        self.status_t = 0            # monotonic ns when it arrived
        self.expected_exit = False
        self.down = False            # crashed, respawn pending
        self.storage_error = False
        self.spawn_t = 0.0
        self.on_spawn = None         # called after each spawn, so a caller can re-send its commands
        os.makedirs(log_dir, exist_ok=True)

    # ── control ──────────────────────────────────────────────────────────────────────────────────────
    def start(self, session):
        with self.lock:
            assert self.state == IDLE
            self.session = session
            self.storage_error = False
            self.down = False
            self.status = {}
            self.logf = open(os.path.join(self.log_dir, session["id"] + ".jsonl"), "a", buffering=1)
            self.log_event({"ev": "session", **session})
            self.state = RUNNING
            self._spawn()

    def stop(self):
        """Blocks until the recorder has drained (at most DRAIN_S, then SIGKILL). Idempotent."""
        with self.lock:
            if self.state == ERROR:
                self.state = IDLE
                self._close_log()
                return
            if self.state == IDLE:
                return
            self.state = STOPPING
            self.expected_exit = True
            proc, reader = self.proc, self.reader
            if proc is not None:
                try:
                    proc.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass
        if reader is not None:
            reader.join(DRAIN_S)
            if reader.is_alive() and proc is not None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                reader.join(DRAIN_S)
        with self.lock:
            if self.state == STOPPING:        # no process was alive (respawn pending): finish here
                self.state = IDLE
                self.expected_exit = False
                self._close_log()

    def send(self, line):
        """One line to the running recorder's stdin. False when there is nobody to send it to."""
        with self.lock:
            proc = self.proc if self.state == RUNNING else None
            if proc is None or proc.stdin is None or proc.stdin.closed:
                return False
            try:
                proc.stdin.write((line + "\n").encode())
                proc.stdin.flush()
                return True
            except (BrokenPipeError, OSError, ValueError):
                return False

    def log_event(self, ev):
        with self.lock:
            if self.logf is not None:
                self.logf.write(json.dumps(ev, separators=(",", ":")) + "\n")

    def pid(self):
        with self.lock:
            return self.proc.pid if self.proc else None

    # ── internals ────────────────────────────────────────────────────────────────────────────────────
    def _spawn(self):
        argv, env = self.build_cmd(self.session)
        self.say("recorder: %s" % " ".join(argv))
        self.proc = subprocess.Popen(argv, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=sys.stderr, start_new_session=True)
        self.spawn_t = time.monotonic()
        self.expected_exit = False
        self.reader = threading.Thread(target=self._read, args=(self.proc,), daemon=True)
        self.reader.start()
        if self.on_spawn:
            try:
                self.on_spawn()
            except Exception as e:  # noqa: BLE001 — a handler bug must not stop the recorder
                self.say("on_spawn: %r" % e)

    def _read(self, proc):
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            with self.lock:
                if self.logf is not None:
                    self.logf.write(line + "\n")
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind = ev.get("ev")
            if kind == "status":
                with self.lock:
                    self.status = ev
                    self.status_t = time.monotonic_ns()
            elif kind == "storage_error":
                self.storage_error = True
            try:
                self.on_event(ev)
            except Exception as e:  # noqa: BLE001 — a handler bug must not kill the reader
                self.say("event handler: %r on %s" % (e, line[:120]))
        rc = proc.wait()
        self._on_exit(proc, rc)

    def _on_exit(self, proc, rc):
        with self.lock:
            if proc is not self.proc:
                return
            self.proc = None
            for pipe in (proc.stdin, proc.stdout):
                try:
                    if pipe is not None:
                        pipe.close()
                except OSError:
                    pass
            lived = time.monotonic() - self.spawn_t
            if self.expected_exit:
                self.log_event({"ev": "recorder_exit", "rc": rc, "expected": True})
                self.state = IDLE
                self.expected_exit = False
                self._close_log()
                return
            self.log_event({"ev": "recorder_exit", "rc": rc, "expected": False, "lived_s": round(lived, 1)})
            self.say("recorder exited rc=%s after %.1f s" % (rc, lived))
            if self.storage_error or lived < MIN_LIFE_S:
                self.state = ERROR
                return
            self.down = True
            threading.Timer(RESPAWN_DELAY_S, self._respawn).start()

    def _respawn(self):
        with self.lock:
            if self.state != RUNNING or self.proc is not None:
                self.down = False
                return
            self.log_event({"ev": "recorder_restart"})
            self.down = False
            self._spawn()

    def _close_log(self):
        if self.logf is not None:
            self.logf.close()
            self.logf = None
