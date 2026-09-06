"""The tile.v1 methods (DESIGN_RING_CONTROL.md §8) over the index, the lock map, the supervisor and the
clocks. One Agent per process; http.py is the transport in front of it."""
import glob
import json
import os
import re
import shlex
import subprocess
import threading
import time

from . import beacon, clock, live, supervisor
from .index import Index, index_segment
from .locks import Locks, seg_name
from .pb import ROOT, fault_values, pb
from .trigger import EpwmTrigger

RECONCILE_S = 60
SESSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


class ApiError(Exception):
    def __init__(self, status, code, message, **detail):
        super().__init__(message)
        self.status, self.code, self.message, self.detail = status, code, message, {k: str(v) for k, v in detail.items()}

    def body(self):
        return pb.Error(code=self.code, message=self.message, detail=self.detail)


def bad_request(msg, **d): return ApiError(400, pb.Error.BAD_REQUEST, msg, **d)
def not_found(msg, **d): return ApiError(404, pb.Error.NOT_FOUND, msg, **d)
def conflict(msg, **d): return ApiError(409, pb.Error.CONFLICT, msg, **d)
def not_ready(msg, **d): return ApiError(503, pb.Error.NOT_READY, msg, **d)


class NodeConfig:
    """Node configuration from the command line; the session may override the optional fields."""
    def __init__(self, a):
        self.width, self.height, self.fps = a.width, a.height, a.fps
        self.exposure_lines, self.analogue_gain, self.bitrate_bps = a.exposure, a.gain, a.bitrate
        self.segment_s, self.budget_bytes = a.segment_s, a.budget
        self.proxy = pb.ProxySettings(enabled=False, width=a.proxy_width, height=a.proxy_height,
                                      fps=a.proxy_fps, bitrate_bps=a.proxy_bitrate)
        expected = a.bitrate // 8 * a.segment_s
        if a.budget:      # §6: default 40 %, never above budget - 4 x expected
            cap = a.lock_budget if a.lock_budget else int(a.budget * 0.4)
            self.lock_budget_bytes = min(cap, max(0, a.budget - 4 * expected))
        else:             # no reclaim, so no cap unless one is asked for
            self.lock_budget_bytes = a.lock_budget or (1 << 62)
        self.hw = a.hw
        self.ptp_dev = a.ptp

    def effective(self, start=None, trigger=None):
        """A tile.v1 Config; `start` is the session's StartRecordingRequest with its optional overrides.
        `trigger` is the node's trigger port, needed only to refuse an exposure that would not happen."""
        c = pb.Config(width=self.width, height=self.height, fps=self.fps,
                      sensor=pb.SensorSettings(exposure_lines=self.exposure_lines, analogue_gain=self.analogue_gain),
                      encoder=pb.EncoderSettings(bitrate_bps=self.bitrate_bps),
                      ring=pb.RingSettings(segment_s=self.segment_s, budget_bytes=self.budget_bytes, lock_budget_bytes=self.lock_budget_bytes),
                      proxy=self.proxy)
        if start is not None:
            if start.HasField("fps"): c.fps = start.fps
            if start.HasField("exposure_lines"):
                # The exposure register does nothing while the sensor is slaved: exposure is the XTRIG low
                # width (OPEN.md §2, measured). Refuse rather than accept a setting that will not happen.
                if trigger is not None and trigger.read().armed:
                    raise bad_request("exposure_lines does nothing while the trigger is armed: exposure is the "
                                   "pulse low width, set it with SetTrigger.lowNs")
                c.sensor.exposure_lines = start.exposure_lines
            if start.HasField("analogue_gain"): c.sensor.analogue_gain = start.analogue_gain
            if start.HasField("bitrate_bps"): c.encoder.bitrate_bps = start.bitrate_bps
            if start.HasField("segment_s"): c.ring.segment_s = start.segment_s
        # The proxy cannot run faster than the capture: a session that lowers fps below the proxy rate
        # would otherwise hand the recorder a pair it refuses, and the session would end in ERROR.
        c.proxy.fps = min(c.proxy.fps, c.fps) or c.fps
        return c


class Agent:
    def __init__(self, args, say=print):
        self.say = say
        self.ring = os.path.abspath(args.ring)
        self.cfg = NodeConfig(args)
        for d in ("seg", "lock", "logs"):
            os.makedirs(os.path.join(self.ring, d), exist_ok=True)
        self.seg_dir = os.path.join(self.ring, "seg")
        self.log_dir = os.path.join(self.ring, "logs")
        self.index = Index(os.path.join(self.ring, "index.db"))
        self.locks = Locks(self.ring)
        self.ptp = clock.PtpClock(args.ptp)
        self.offsets = clock.OffsetSeries()
        self.boot_id = clock.boot_id()
        self.t0 = time.monotonic()
        # --recorder takes several words, but argparse stops at the next -option, so a command with its
        # own flags is given as one quoted string and split here.
        cmd = args.recorder or [os.path.join(ROOT, "pipeline", "kpipe.sh")]
        if len(cmd) == 1 and " " in cmd[0]:
            cmd = shlex.split(cmd[0])
        self.recorder_cmd = None if args.no_recorder else cmd
        self.recorder = supervisor.Recorder(self.log_dir, self._recorder_cmd, self._on_event, say)
        self.trigger = EpwmTrigger(args.trigger_sh or os.path.join(ROOT, "tools", "trigger.sh"))
        # The trigger source counts the pulses this board receives and writes the log the beacon is read
        # from (PLAN_K_MATCHING.md §4). It outlives the recorder, which is the point: a recorder starting
        # in mid-train has no idea which pulse it is on, and this is what tells it.
        self.trigger_log = os.path.join(self.ring, "trigger.log")
        self.source = beacon.TriggerSource(self.trigger_log, args.ptp, say=say)
        if not args.no_trigger_log and self.trigger.available():
            self.source.start()
        self.relay = beacon.BeaconRelay(args.beacon_src, self.recorder, say) if args.beacon_src else None
        if self.relay:
            self.relay.start()
        self.capabilities = ["core", "segments", "sessions", "locks", "metrics"]
        if self.recorder_cmd:
            self.capabilities.append("recording")
        if self.trigger.available():
            self.capabilities.append("trigger.local")
        # `live` is declared only when the branch is really there (§8.3a): a recorder that grows one, and
        # a socket for it to come out of. --proxy/--no-proxy decide; the default is on for the stock
        # recorder and off for any other (kpipe-synth on the PC has no second encoder).
        self.proxy_sock = args.proxy_sock or live.default_sock(self.ring)
        self.live = None
        if self.recorder_cmd and (args.proxy if args.proxy is not None else args.recorder is None):
            self.cfg.proxy.enabled = True
            self.capabilities.append("live")
            self.live = live.LiveFanout(self.proxy_sock, self._recorder_command, self.say)
            self.recorder.on_spawn = self._resend_proxy_state
        if self.source.running or os.path.exists(self.trigger_log):
            self.capabilities.append("trigger.log")
        self.node_id = args.node_id or _node_id()
        self.firmware = _firmware()
        self.storage_full = False
        self.rebuilding = True
        self.reconcile_lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.bg = threading.Thread(target=self._background, daemon=True)
        self.bg.start()
        self.say("tile-agent: ring %s, node %s, capabilities %s, budget %d, lock cap %d" %
                 (self.ring, self.node_id, ",".join(self.capabilities), self.cfg.budget_bytes, self.cfg.lock_budget_bytes))

    # ── background: clock samples, reconcile ─────────────────────────────────────────────────────────
    def _background(self):
        self._sample_clock()
        self.reconcile()
        self.rebuilding = False
        self.say("tile-agent: index has %d rows, %d locks" % (self.index.count(), len(self.locks.ids())))
        last = time.monotonic()
        while not self.stop_flag.wait(1.0):
            self._sample_clock()
            if time.monotonic() - last >= RECONCILE_S:
                self.reconcile()
                last = time.monotonic()

    def _sample_clock(self):
        off = self.ptp.sample()
        if off is not None:
            self.offsets.add(clock.mono_ns(), off)
            self.source.ptp_minus_mono = off
            self.recorder.log_event({"ev": "clock_offset", "t_mono": clock.mono_ns(), "ptp_minus_mono": off})

    def offset_at(self, t_mono):
        return self.offsets.at(t_mono)

    def offset_now(self):
        return self.offsets.latest() or 0

    def reconcile(self, only=None, partial=False):
        """One function, three callers (§5): the doorbell (only=segno), the minute timer, and start."""
        with self.reconcile_lock:
            present = set(_list_segnos(self.seg_dir))
            alive = present | self.locks.locked_segnos()
            rows = self.index.all_segnos()
            todo = ({only} & alive) if only is not None else (alive - rows)
            session_of = None
            for s in sorted(todo):
                path = self.locks.path_of(s)
                if path is None:
                    continue
                if only is not None and self.recorder.session:
                    session = self.recorder.session["id"]
                else:
                    session_of = session_of if session_of is not None else self._session_map()
                    session = session_of.get(s, "recovered")
                try:
                    row = index_segment(path, s, session, self.offset_at, partial)
                    # A frame time in this boot's future was written under an earlier boot: the file
                    # outlived a reboot (the ring is on the eMMC, the rootfs is not). It cannot belong to
                    # the running session, and leaving it there would poison every relative-time query,
                    # whose anchor is the newest frame time of the current boot.
                    if row["t_mono_last"] > time.monotonic_ns():
                        row["session"], row["partial"] = "recovered", 1
                    self.index.upsert(row)
                except FileNotFoundError:
                    continue                       # reclaimed while we read it
                except ValueError as e:
                    self.say("reconcile: %s" % e)
            if only is None:
                for s in rows - alive:
                    self.index.delete(s)
                self._prune_logs()

    def _session_map(self):
        """segno -> session id from the segment_closed lines of every log (only when a file has no row)."""
        out = {}
        for path in glob.glob(os.path.join(self.log_dir, "*.jsonl")):
            sid = os.path.basename(path)[:-6]
            try:
                with open(path, "rb") as f:
                    for line in f:
                        if b'"segment_closed"' in line:
                            m = re.search(rb'"segno":(\d+)', line)
                            if m:
                                out[int(m.group(1))] = sid
            except OSError:
                pass
        return out

    def _prune_logs(self):
        """Logs of sessions with no remaining segment go (§6); never the current session's."""
        keep = self.index.sessions()
        current = self.recorder.session["id"] if self.recorder.session else None
        for path in glob.glob(os.path.join(self.log_dir, "*.jsonl")):
            sid = os.path.basename(path)[:-6]
            if sid in keep or sid == current:
                continue
            if time.time() - os.path.getmtime(path) < 3600:   # a fresh session that has not closed a segment yet
                continue
            os.unlink(path)

    def _on_event(self, ev):
        kind = ev.get("ev")
        if kind == "proxy" and self.live:
            # kpipe says its proxy branch is up ("ready"), and then reports the valve every time it moves
            # ("open"/"closed"). Only the first is about readiness: treating a valve report as "not ready"
            # unset the flag the moment the first viewer arrived, and the fan-out then waited out its
            # 30 s deadline and never connected to a socket that was there all along.
            if ev.get("state") == "ready":
                self.live.set_ready(True)
        elif kind == "recorder_exit" and self.live:
            self.live.set_ready(False)
        if kind == "segment_closed":
            self.reconcile(only=int(ev["segno"]), partial=bool(ev.get("partial")))
            self.storage_full = False
        elif kind == "storage_full":
            self.storage_full = True

    def _recorder_command(self, line):
        """One line to the recorder's stdin, for the live-view valve. Silent when nothing is recording:
        an open valve is meaningless without a recorder and the next spawn is told again."""
        return self.recorder.send(line)

    def _resend_proxy_state(self):
        """A respawned recorder starts with the valve closed; tell it again if anyone is still watching."""
        if self.live:
            self.live.reopen_after_respawn()

    def _recorder_cmd(self, session):
        start = pb.from_json(json.dumps(session["start"]), pb.StartRecordingRequest)
        c = self.cfg.effective(start, self.trigger)
        edges = "ptp" if start.edges == pb.StartRecordingRequest.PTP else "grid"
        argv = list(self.recorder_cmd) + ["--ring", self.ring, "--fps", str(c.fps), "--segment-s", str(c.ring.segment_s),
                                          "--budget", str(c.ring.budget_bytes), "--bitrate", str(c.encoder.bitrate_bps),
                                          "--edges", edges]
        # Unset means "the tile knows better": kpipe then uses one trigger period, which is what the
        # sensor's frame length makes the delay (OPEN.md §1). Sending a guess from the client only
        # earns a LATE fault on every frame.
        if start.latency_ns:
            argv += ["--latency-ns", str(start.latency_ns)]
        if c.proxy.enabled:
            p = c.proxy
            argv += ["--proxy-sock", self.proxy_sock, "--proxy-width", str(p.width), "--proxy-height", str(p.height),
                     "--proxy-fps", str(p.fps), "--proxy-bitrate", str(p.bitrate_bps)]
        env = dict(os.environ, FPS=str(c.fps), EXPOSURE=str(c.sensor.exposure_lines), GAIN=str(c.sensor.analogue_gain))
        return argv, env

    def shutdown(self):
        self.stop_flag.set()
        if self.live:
            self.live.shutdown()
        if self.relay:
            self.relay.stop()
        self.source.stop()
        self.recorder.stop()

    # ── helpers ──────────────────────────────────────────────────────────────────────────────────────
    def require(self, capability):
        if capability not in self.capabilities:
            raise ApiError(501, pb.Error.NOT_IMPLEMENTED, "capability %s not declared by this node" % capability)

    def used_bytes(self):
        if os.path.ismount(self.ring):
            v = os.statvfs(self.ring)
            return (v.f_blocks - v.f_bfree) * v.f_frsize
        seen, total = set(), 0
        for d, _, files in os.walk(self.ring):
            for n in files:
                try:
                    st = os.lstat(os.path.join(d, n))
                except OSError:
                    continue
                if (st.st_dev, st.st_ino) not in seen:
                    seen.add((st.st_dev, st.st_ino))
                    total += st.st_blocks * 512
        return total

    def _segment_msg(self, row):
        s = pb.Segment(segno=row["segno"], session=row["session"], frames=row["frames"], bytes=row["bytes"],
                       faults=fault_values(row["faults"]), partial=bool(row["partial"]),
                       reclaimable=os.path.exists(os.path.join(self.seg_dir, seg_name(row["segno"]))),
                       locks=self.locks.holders(row["segno"]))
        off = row["ptp_minus_mono"] if row["ptp_minus_mono"] is not None else self.offset_now()
        s.t_ptp_first_ns, s.t_ptp_last_ns = row["t_mono_first"] + off, row["t_mono_last"] + off
        if row["k_first"] is not None:
            s.first.CopyFrom(pb.KPoint(epoch=row["epoch_first"], k=row["k_first"]))
            s.last.CopyFrom(pb.KPoint(epoch=row["epoch_last"], k=row["k_last"]))
        return s

    def _select(self, sel):
        if self.rebuilding:
            raise not_ready("index rebuilding")
        if sel.WhichOneof("by") is None:
            raise bad_request("selector: exactly one of last_s, ptp, k, segno must be set")
        rows = []
        for r in self.index.select(sel, self.offset_now()):
            if self.locks.path_of(r["segno"]) is None:
                self.index.delete(r["segno"])
                continue
            rows.append(r)
        return rows

    def _lock_msg(self, lid):
        m = self.locks.meta[lid]
        segnos = sorted(self.locks.segnos(lid))
        return pb.Lock(id=lid, reason=m.get("reason", ""), requester=m.get("requester", ""), created_ptp_ns=int(m.get("created_ptp_ns", 0)),
                       segnos=segnos, bytes=self._bytes_anywhere(segnos))

    def _bytes_anywhere(self, segnos):
        total = 0
        for s in segnos:
            r = self.index.get(s)
            if r is not None:
                total += r["bytes"]
            else:
                p = self.locks.path_of(s)
                total += os.path.getsize(p) if p else 0
        return total

    def _recording_status(self):
        rec = self.recorder
        st = rec.status
        m = pb.RecordingStatus(state=getattr(pb.RecordingStatus, rec.state), session=rec.session["id"] if rec.session and rec.state != supervisor.IDLE else "",
                               open_segno=int(st.get("open_segno", 0)), pre_idr_dropped=int(st.get("pre_idr_dropped", 0)),
                               live_clients=self.live.count() if self.live else 0)
        if rec.session and rec.state != supervisor.IDLE:
            m.since_ptp_ns = rec.session["t_mono_start"] + self.offset_now()
        return m

    def _trigger_status(self):
        """From the trigger source when it runs: it counts across recorder restarts, the recorder does not.
        Otherwise the recorder's latest status values, which stand still while recording is stopped."""
        if self.source.running:
            return pb.TriggerStatus(settings=self.trigger.read(), epoch=self.source.epoch, k=max(self.source.k, 0),
                                    edges_seen=self.source.pulses, last_edge_ptp_ns=self.source.t_ptp)
        st = self.recorder.status
        return pb.TriggerStatus(settings=self.trigger.read(), epoch=int(st.get("epoch", 0)), k=int(st.get("k", 0)),
                                edges_seen=int(st.get("edges_total", 0)))

    def _ring_status(self):
        segnos = _list_segnos(self.seg_dir)
        m = pb.RingStatus(used_bytes=self.used_bytes(), locked_bytes=self._bytes_anywhere(sorted(self.locks.locked_segnos())), segments=len(segnos))
        if segnos:
            m.oldest_segno, m.newest_segno = segnos[0], segnos[-1]
            for attr, s in (("oldest_ptp_ns", segnos[0]), ("newest_ptp_ns", segnos[-1])):
                r = self.index.get(s)
                if r is not None:
                    off = r["ptp_minus_mono"] if r["ptp_minus_mono"] is not None else self.offset_now()
                    setattr(m, attr, (r["t_mono_first"] if attr.startswith("oldest") else r["t_mono_last"]) + off)
        return m

    def _storage_status(self):
        dev = _mount_source(self.ring)
        m = pb.StorageStatus(device=os.path.basename(dev) if dev else "")
        v = os.statvfs(self.ring)
        m.fs_free_bytes = v.f_bavail * v.f_frsize
        base = re.sub(r"p\d+$", "", os.path.basename(dev)) if dev else ""
        if base.startswith("mmcblk"):
            lt = _read("/sys/block/%s/device/life_time" % base).split()
            if len(lt) == 2:
                m.life_time_a, m.life_time_b = int(lt[0], 16), int(lt[1], 16)
            pe = _read("/sys/block/%s/device/pre_eol_info" % base).strip()
            if pe:
                m.pre_eol = int(pe, 16)
        return m

    def _time(self):
        now = clock.mono_ns()
        return pb.Time(ptp_ns=now + self.offset_now(), ptp_locked=False, boot_id=self.boot_id, ptp_minus_mono_ns=self.offset_now())

    def _faults(self, storage):
        f = [pb.PTP_UNLOCKED]
        rec = self.recorder
        if rec.state == supervisor.RUNNING and not rec.down and rec.status_t and (clock.mono_ns() - rec.status_t > 2_500_000_000 or rec.status.get("fps_in", 1) == 0):
            f.append(pb.TRIGGER_STALLED)
        if self.storage_full: f.append(pb.STORAGE_FULL)
        if self.rebuilding: f.append(pb.INDEX_REBUILDING)
        if rec.storage_error: f.append(pb.STORAGE_ERROR)
        if storage.pre_eol >= 2: f.append(pb.STORAGE_WEAR_WARNING)
        if rec.down: f.append(pb.RECORDER_DOWN)
        return f

    # ── tile.v1 methods ──────────────────────────────────────────────────────────────────────────────
    def GetNode(self, req):
        return pb.Node(node_id=self.node_id, hw=self.cfg.hw, firmware=self.firmware, api_version=pb.API_VERSION, capabilities=self.capabilities)

    def GetHealth(self, req):
        storage = self._storage_status()
        return pb.Health(recording=self._recording_status(), trigger=self._trigger_status(), ring=self._ring_status(), storage=storage,
                         clocks=self._time(), faults_active=self._faults(storage), uptime_s=int(_read("/proc/uptime").split()[0].split(".")[0] or 0))

    def GetTelemetry(self, req):
        st = self.recorder.status if self.recorder.state == supervisor.RUNNING else {}
        temp = _read("/sys/class/thermal/thermal_zone0/temp").strip()
        return pb.Telemetry(t_ptp_ns=clock.mono_ns() + self.offset_now(), fps_in=int(st.get("fps_in", 0)), fps_out=int(st.get("fps_out", 0)),
                            bytes_written_1s=int(st.get("bytes_1s", 0)), edges_seen_1s=int(st.get("edges_1s", 0)), faults_1s=int(st.get("faults_1s", 0)),
                            k=int(st.get("k", 0)), epoch=int(st.get("epoch", 0)), used_bytes=self.used_bytes(),
                            locked_bytes=self._bytes_anywhere(sorted(self.locks.locked_segnos())),
                            temp_c=int(temp) / 1000.0 if temp.isdigit() else 0.0, load1=os.getloadavg()[0])

    def GetTime(self, req):
        return self._time()

    def GetConfig(self, req):
        start = None
        if self.recorder.session and self.recorder.state != supervisor.IDLE:
            start = pb.from_json(json.dumps(self.recorder.session["start"]), pb.StartRecordingRequest)
        return self.cfg.effective(start, self.trigger)

    def SetSensor(self, req):
        self.require("config.live_sensor")

    def GetTrigger(self, req):
        self.require("trigger.local")
        return self._trigger_status()

    def SetTrigger(self, req):
        self.require("trigger.local")
        try:
            self.trigger.apply(req)
        except ValueError as e:
            raise bad_request(str(e))
        return self._trigger_status()

    def GetRecording(self, req):
        self.require("recording")
        return self._recording_status()

    def StartRecording(self, req):
        self.require("recording")
        with self.recorder.lock:
            st = self.recorder.state
            if st in (supervisor.RUNNING, supervisor.STOPPING):
                raise conflict("recording is %s" % st.lower(), session=self.recorder.session["id"])
            if st == supervisor.ERROR:
                raise not_ready("recorder in error; StopRecording clears it")
            if req.edges == pb.StartRecordingRequest.EDGES_UNSPECIFIED:
                req.edges = pb.StartRecordingRequest.GRID
            t_mono = clock.mono_ns()
            session = {"id": "%s-%d" % (self.boot_id[:8], t_mono // 1_000_000), "boot_id": self.boot_id, "t_mono_start": t_mono,
                       "t_ptp_start": t_mono + self.offset_now(), "start": json.loads(pb.to_json(req)),
                       "config": json.loads(pb.to_json(self.cfg.effective(req, self.trigger)))}
            self.recorder.start(session)
        return self._recording_status()

    def StopRecording(self, req):
        self.require("recording")
        self.recorder.stop()
        return self._recording_status()

    def ListSegments(self, req):
        self.require("segments")
        return pb.SegmentList(segments=[self._segment_msg(r) for r in self._select(req.select)])

    def GetSegment(self, req):
        self.require("segments")
        if self.rebuilding:
            raise not_ready("index rebuilding")
        r = self.index.get(req.segno)
        if r is None or self.locks.path_of(req.segno) is None:
            raise not_found("segment %d" % req.segno, segno=req.segno)
        return self._segment_msg(r)

    def ListSessions(self, req):
        self.require("sessions")
        out = pb.SessionList()
        for path in sorted(glob.glob(os.path.join(self.log_dir, "*.jsonl"))):
            try:
                with open(path) as f:
                    head = json.loads(f.readline())
            except (OSError, ValueError):
                continue
            if head.get("ev") != "session":
                continue
            s = out.sessions.add(id=head["id"], t_ptp_start_ns=int(head.get("t_ptp_start", 0)))
            s.start.CopyFrom(pb.from_json(json.dumps(head.get("start", {})), pb.StartRecordingRequest))
            s.segno_first, s.segno_last = self.index.session_range(head["id"])
        return out

    def ListLocks(self, req):
        self.require("locks")
        return pb.LockList(locks=[self._lock_msg(lid) for lid in self.locks.ids()])

    def GetLock(self, req):
        self.require("locks")
        if not self.locks.has(req.id):
            raise not_found("lock %s" % req.id, lock=req.id)
        return self._lock_msg(req.id)

    def PutLock(self, req):
        self.require("locks")
        if not SESSION_RE.match(req.id):
            raise bad_request("lock id: 1-80 characters of [A-Za-z0-9._-]")
        rows = self._select(req.select)
        with self.locks.lock:
            already = self.locks.locked_segnos()
            new_bytes = sum(r["bytes"] for r in rows if r["segno"] not in already)
            locked = self._bytes_anywhere(sorted(already))
            if locked + new_bytes > self.cfg.lock_budget_bytes:
                raise ApiError(507, pb.Error.INSUFFICIENT_STORAGE, "lock would exceed lock_budget_bytes",
                               needed_bytes=locked + new_bytes, lock_budget_bytes=self.cfg.lock_budget_bytes, locked_bytes=locked)
            meta = {"id": req.id, "reason": req.reason, "requester": req.requester, "created_ptp_ns": clock.mono_ns() + self.offset_now()}
            created, added, missing = self.locks.put(req.id, [r["segno"] for r in rows], meta)
        return pb.PutLockResponse(lock=self._lock_msg(req.id), created=created, added=added, missing=missing)

    def DeleteLock(self, req):
        self.require("locks")
        if not self.locks.has(req.id):
            raise not_found("lock %s" % req.id, lock=req.id)
        for s in self.locks.delete(req.id):
            if self.locks.path_of(s) is None:
                self.index.delete(s)
        return pb.Empty()

    # ── bulk ─────────────────────────────────────────────────────────────────────────────────────────
    def segment_path(self, segno):
        self.require("segments")
        p = self.locks.path_of(segno)
        if p is None:
            raise not_found("segment %d" % segno, segno=segno)
        return p

    def live_stream(self):
        """A subscriber to the proxy substream, for GET /v1/live. 503 while nothing is recording: the
        branch lives inside the recorder and there is no fallback that streams the recording (§8.3a)."""
        self.require("live")
        if self.recorder.state != supervisor.RUNNING:
            raise not_ready("not recording: the live view comes out of the recorder", state=self.recorder.state)
        return self.live

    def trigger_log_path(self):
        self.require("trigger.log")
        if not os.path.exists(self.trigger_log):
            raise not_found("no trigger log yet")
        return self.trigger_log

    def session_log_path(self, sid):
        self.require("sessions")
        p = os.path.join(self.log_dir, sid + ".jsonl")
        if not SESSION_RE.match(sid) or not os.path.exists(p):
            raise not_found("session %s" % sid, session=sid)
        return p

    def metrics(self):
        self.require("metrics")
        t = self.GetTelemetry(None)
        lines = []
        for f in pb.Telemetry.DESCRIPTOR.fields:
            v = getattr(t, f.name)
            lines.append("# TYPE tile_%s gauge\ntile_%s{node=\"%s\"} %s" % (f.name, f.name, self.node_id, repr(float(v)) if isinstance(v, float) else v))
        return "\n".join(lines) + "\n"


def _list_segnos(seg_dir):
    try:
        return sorted(int(n[:12]) for n in os.listdir(seg_dir) if len(n) == 17 and n.endswith(".h265") and n[:12].isdigit())
    except OSError:
        return []


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def _mount_source(path):
    best = ("", "")
    try:
        with open("/proc/mounts") as f:
            for line in f:
                src, mnt = line.split()[:2]
                if (path + "/").startswith(mnt.rstrip("/") + "/") and len(mnt) > len(best[1]):
                    best = (src, mnt)
    except OSError:
        pass
    return os.path.realpath(best[0]) if best[0].startswith("/dev") else best[0]


def _node_id():
    for iface in ("eth0", "end0", "enp1s0"):
        mac = _read("/sys/class/net/%s/address" % iface).strip()
        if mac:
            return mac
    for iface in sorted(os.listdir("/sys/class/net")) if os.path.isdir("/sys/class/net") else []:
        if iface != "lo":
            mac = _read("/sys/class/net/%s/address" % iface).strip()
            if mac and mac != "00:00:00:00:00:00":
                return mac
    return os.uname().nodename


def _firmware():
    try:
        return subprocess.run(["git", "-C", ROOT, "describe", "--always", "--dirty"], capture_output=True, text=True, timeout=5).stdout.strip() or _read(os.path.join(ROOT, "VERSION")).strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return _read(os.path.join(ROOT, "VERSION")).strip() or "unknown"
