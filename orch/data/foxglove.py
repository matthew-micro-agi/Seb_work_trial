"""orch bridge and orch mcap (PLAN_ORCH.md §6): tiles' Health/Telemetry as our own protobuf schemas,
per-frame K as JSON, and video as foxglove.CompressedVideo, into a Foxglove WebSocket server or an MCAP
file. The only module that imports the foxglove SDK (a venv on the laptop, TESTING.md).
"""
import json
import os
import threading
import time

import foxglove
from foxglove import Channel, Schema
from foxglove.channels import CompressedVideoChannel
from foxglove.messages import CompressedVideo, Timestamp
from google.protobuf import descriptor_pb2

import sei

from .. import clock, pb
from ..client import Client, TileError, load_nodes, node_dir

UNMATCHED_K = 0xFFFFFFFF
PROXY_FORMAT = "h264"          # DESIGN_RING_CONTROL.md §8.3a; ProxySettings has no codec field
PROXY_CODEC = "h264"           # the AU splitter's dialect for the same stream
RETRY_S = 5.0                  # a tile that is not recording, or is restarting, is tried again this often
LINK_SCHEMA = {"type": "object", "properties": {"reachable": {"type": "boolean"}, "age_s": {"type": "number"}, "error": {"type": "string"}}}
FRAME_SCHEMA = {"type": "object", "properties": {"epoch": {"type": "integer"}, "k": {"type": "integer"}, "frame_ts_ns": {"type": "integer"},
                                                 "matched": {"type": "boolean"}, "faults": {"type": "array", "items": {"type": "string"}}}}


def _descriptor_set():
    fds = descriptor_pb2.FileDescriptorSet()
    fds.file.add().CopyFrom(descriptor_pb2.FileDescriptorProto.FromString(pb.DESCRIPTOR.serialized_pb))
    return fds.SerializeToString()


def proto_channel(topic, cls):
    return Channel(topic, schema=Schema(name=cls.DESCRIPTOR.full_name, encoding="protobuf", data=_descriptor_set()), message_encoding="protobuf")


def json_channel(topic, name, schema):
    return Channel(topic, schema=Schema(name=name, encoding="jsonschema", data=json.dumps(schema).encode()), message_encoding="json")


def frame_json(rec):
    epoch, k, ts, _, flags = rec
    return {"epoch": epoch, "k": -1 if k == UNMATCHED_K else k, "frame_ts_ns": ts, "matched": k != UNMATCHED_K,
            "faults": [pb.Fault.Name(i + 1) for i in range(16) if flags & (1 << i) and (i + 1) in pb.Fault.values()]}


class TileChannels:
    def __init__(self, tid, video=True):
        base = "/tile/%s/" % tid
        self.tid = tid
        self.health = proto_channel(base + "health", pb.Health)
        self.telemetry = proto_channel(base + "telemetry", pb.Telemetry)
        self.link = json_channel(base + "link", "orch.Link", LINK_SCHEMA)
        self.video = CompressedVideoChannel(base + "video") if video else None
        self.frame = json_channel(base + "frame", "orch.Frame", FRAME_SCHEMA) if video else None

    def publish_au(self, data, rec, fmt, t_ns):
        self.video.log(CompressedVideo(timestamp=Timestamp(sec=t_ns // clock.NS, nsec=t_ns % clock.NS), frame_id=self.tid, data=data, format=fmt), log_time=t_ns)
        if rec:
            self.frame.log(frame_json(rec), log_time=t_ns)


# ── bridge ───────────────────────────────────────────────────────────────────────────────────────────
def sample_loop(client, ch, stop, say):
    """GetHealth + GetTelemetry once a second; /link says whether the tile answered and how old the truth is."""
    last_ok = time.time()
    while not stop.is_set():
        now = time.time_ns()
        try:
            h, t = client.rpc("GetHealth"), client.rpc("GetTelemetry")
            ch.health.log(h.SerializeToString(), log_time=now)
            ch.telemetry.log(t.SerializeToString(), log_time=now)
            last_ok = time.time()
            ch.link.log({"reachable": True, "age_s": 0.0, "error": ""}, log_time=now)
        except (OSError, TileError) as e:
            ch.link.log({"reachable": False, "age_s": round(time.time() - last_ok, 1), "error": str(e)}, log_time=now)
        stop.wait(1.0)


def live_loop(client, ch, stop, say):
    """GET /v1/live: the tile's proxy substream, split into access units, each published as
    foxglove.CompressedVideo with the SEI's record on /frame beside it.

    The stream begins at an IDR and carries its parameter sets on every keyframe, so a reconnect needs
    no state: whatever arrives first is decodable. 503 is the normal answer from a tile that is not
    recording (the branch lives inside the recorder), so it is retried quietly rather than treated as
    a failure; anything else is said once and then also retried, because a tile comes back.

    The log time is this laptop's wall clock, not the frame's PTP time: the tiles free-run from boot
    (OPEN.md §1c) and Foxglove's live view plots against now. The frame's own time is on /frame."""
    said = None
    while not stop.is_set():
        try:
            st, _, r, conn = client.open("/v1/live")
            if st != 200:
                if said != st:
                    say("live: %d%s; retrying every %.0f s" % (st, " (tile is not recording)" if st == 503 else "", RETRY_S))
                    said = st
                conn.close()
                stop.wait(RETRY_S)
                continue
            said = None
            n0 = 0
            buf = b""
            try:
                while not stop.is_set():
                    chunk = r.read1(65536)   # read1, not read: a full 64 KB is 0.7 s of proxy at 1 Mbps
                    if not chunk:
                        break
                    buf += chunk
                    aus = sei.split(buf, PROXY_CODEC)
                    for begin, end, nals in aus[:-1]:
                        ch.publish_au(buf[begin:end], sei.record_of(nals, PROXY_CODEC), PROXY_FORMAT, time.time_ns())
                        n0 += 1
                    buf = buf[aus[-1][0]:] if aus else buf
            finally:
                conn.close()
            say("live: stream ended after %d access units; reconnecting" % n0)
        except (OSError, TileError) as e:
            say("live: %s; retrying in %.0f s" % (e, RETRY_S))
        stop.wait(RETRY_S)


def replay_loop(client, ch, stop, say):
    """No `live` on this tile: the newest segment's access units at their frame rate, as h265, repeated."""
    lst = client.rpc("ListSegments", pb.ListSegmentsRequest(select=pb.Selector(last_s=1e9))).segments
    if not lst:
        say("replay: no segments on this tile")
        return
    seg = lst[-1]
    st, _, data = client.get("/v1/segments/%d/data" % seg.segno)
    if st != 200:
        say("replay: GET segment %d: %d" % (seg.segno, st))
        return
    aus = [(data[b:e], sei.record_of(n)) for b, e, n in sei.split(data)]
    say("replay: segment %d, %d AUs, %d frames listed, h265, looping" % (seg.segno, len(aus), seg.frames))
    while not stop.is_set():
        for i, (au, rec) in enumerate(aus):
            if stop.is_set():
                return
            ch.publish_au(au, rec, "h265", time.time_ns())
            nxt = aus[i + 1][1] if i + 1 < len(aus) else None
            dt = (nxt[2] - rec[2]) / clock.NS if rec and nxt else 1 / 30
            stop.wait(min(max(dt, 0.001), 1.0))


def bridge(args):
    say = lambda m: print("bridge: " + m, flush=True)  # noqa: E731
    hosts = load_nodes(args.nodes, args.config)
    server = foxglove.start_server(name="orch bridge", host=args.host, port=args.port)
    stop = threading.Event()
    threads, tids = [], set()
    for host in hosts:
        c = Client(host)
        try:
            node = c.rpc("GetNode")
        except (OSError, TileError) as e:
            say("%s: %s; skipped" % (host, e))
            continue
        live = "live" in node.capabilities
        tid = node_dir(node.node_id)
        if tid in tids:                 # two agents on one PC share its MAC; on tiles the id is unique
            tid += "-" + c.host.rsplit(":", 1)[1]
        tids.add(tid)
        ch = TileChannels(tid, video=live or args.replay_segment)
        threads.append(threading.Thread(target=sample_loop, args=(c, ch, stop, say), daemon=True))
        if live and args.replay_segment:
            say("%s declares live; --replay-segment ignored for it" % host)
        if live:
            threads.append(threading.Thread(target=live_loop, args=(c, ch, stop, say), daemon=True))
        elif args.replay_segment:
            threads.append(threading.Thread(target=replay_loop, args=(c, ch, stop, say), daemon=True))
        say("%s -> /tile/%s/{health,telemetry,link%s}" % (host, ch.tid, ",video,frame" if ch.video else ""))
    for t in threads:
        t.start()
    say("ws://%s:%d  (Foxglove Studio: Open connection > Foxglove WebSocket); Ctrl-C stops" % (args.host, args.port))
    try:
        while not stop.is_set():
            stop.wait(1.0)
    except KeyboardInterrupt:
        pass
    stop.set()
    for t in threads:
        t.join(2.0)
    server.stop()
    return 0


# ── mcap ─────────────────────────────────────────────────────────────────────────────────────────────
def mcap(args):
    """A fetched <out>/<node> directory (manifest.json, seg/, logs/) into one MCAP: CompressedVideo per AU
    at its wall time, /frame per AU, the session logs and the manifest as attachments."""
    d = args.dir.rstrip("/")
    with open(os.path.join(d, "manifest.json")) as f:
        m = json.load(f)
    tid = node_dir(m["node"]["nodeId"])
    offset = m["time"]["offset_ns"]
    rows = {int(s["segno"]): s for s in m["segments"]["segments"]}
    out = args.output or os.path.join(d, "%s.mcap" % tid)
    writer = foxglove.open_mcap(out, allow_overwrite=True)
    ch = TileChannels(tid)
    n_au, first_t, last_t = 0, None, None
    for name in sorted(os.listdir(os.path.join(d, "seg"))):
        if not name.endswith(".h265"):
            continue
        row = rows.get(int(name[:12]))
        with open(os.path.join(d, "seg", name), "rb") as f:
            data = f.read()
        aus = sei.split(data)
        recs = [sei.record_of(n) for _, _, n in aus]
        t0 = int(row["tPtpFirstNs"]) if row else 0
        base = next((r[2] for r in recs if r), 0)
        for (b, e, _), rec in zip(aus, recs):
            t_wall = (t0 + ((rec[2] - base) if rec else 0)) - offset
            ch.publish_au(data[b:e], rec, "h265", t_wall)
            n_au += 1
            first_t = t_wall if first_t is None else first_t
            last_t = t_wall
    logs = os.path.join(d, "logs")
    n_logs = 0
    for name in sorted(os.listdir(logs)) if os.path.isdir(logs) else []:
        with open(os.path.join(logs, name), "rb") as f:
            writer.attach(log_time=last_t or 0, create_time=last_t or 0, name=name, media_type="application/x-ndjson", data=f.read())
        n_logs += 1
    with open(os.path.join(d, "manifest.json"), "rb") as f:
        writer.attach(log_time=last_t or 0, create_time=last_t or 0, name="manifest.json", media_type="application/json", data=f.read())
    writer.close()
    print("%s: %d access units on /tile/%s/video and /frame, %s .. %s, %d session logs attached" % (
        out, n_au, tid, clock.fmt_wall(first_t or 0), clock.fmt_wall(last_t or 0), n_logs))
    return 0
