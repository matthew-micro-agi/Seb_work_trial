"""The live-view fan-out (DESIGN_RING_CONTROL.md §8.3a).

One reader of the recorder's proxy socket, many `GET /v1/live` clients. The recorder's proxy branch is
off unless someone is watching: the first client makes the agent send `proxy on` down the recorder's
stdin, the last one sends `proxy off`, and in between a closed valve costs the recording nothing.

Each client starts at the next IDR, so what it receives is decodable from its first byte: the parameter
sets ride every keyframe (`h264parse config-interval=1`). A client that stops reading is dropped rather
than allowed to back up into the encoder — the recording never waits on the live view.

The recorder's socket is framed (`pipeline/src/livesock.hpp`): "KAU1", a big-endian length and flags, then
the access unit. So an access unit goes out the moment its last byte arrives, instead of waiting for the
next one to begin — which on a byte stream is a whole proxy frame period. What `/v1/live` serves is plain
Annex-B, as the contract says; only this private link is framed.
"""
import os
import socket
import struct
import threading
import time

BACKLOG_CAP = 2 << 20         # bytes queued for one client before it is dropped
CONNECT_RETRY_S = 0.5
START_WAIT_S = 30.0           # a recorder on the camera path configures the sensor before it binds
READ_CHUNK = 1 << 16
MAGIC = b"KAU1"
HEADER = struct.Struct(">4sII")
MAX_AU = 8 << 20              # a proxy access unit larger than this means the framing is out of step


class Client:
    """One GET /v1/live response. The HTTP thread blocks in `read`; the reader thread calls `put`."""

    def __init__(self):
        self.cond = threading.Condition()
        self.queue = bytearray()
        self.closed = False
        self.started = False       # an IDR has been seen, so bytes are flowing
        self.dropped = False       # fell too far behind

    def put(self, data):
        with self.cond:
            if self.closed:
                return
            if len(self.queue) + len(data) > BACKLOG_CAP:
                self.dropped = self.closed = True
            else:
                self.queue += data
            self.cond.notify()

    def read(self, timeout=1.0):
        """The bytes queued so far, b'' on a timeout, None when the client is finished."""
        with self.cond:
            if not self.queue and not self.closed:
                self.cond.wait(timeout)
            if self.queue:
                out, self.queue = bytes(self.queue), bytearray()
                return out
            return None if self.closed else b""

    def close(self):
        with self.cond:
            self.closed = True
            self.cond.notify()


class LiveFanout:
    """Owns the socket reader thread and the client set. `command(line)` reaches the recorder's stdin."""

    def __init__(self, sock_path, command, say=print):
        self.sock_path = sock_path
        self.command = command            # command(line) -> bool; writes one line to the recorder's stdin
        self.say = say
        self.lock = threading.Lock()      # guards clients, stop_flag
        self.valve_lock = threading.Lock()   # serializes the valve commands; never held with `lock`
        self.valve_open = False
        self.clients = []
        self.thread = None
        self.ready = threading.Event()    # the recorder has said its proxy branch is up (kpipe's `proxy ready`)
        self.stop_flag = threading.Event()   # the current reader run's; a new run gets a new one
        self.stop_flag.set()

    # ── what the HTTP handler uses ───────────────────────────────────────────────────────────────────
    def count(self):
        with self.lock:
            return len(self.clients)

    def _sync_valve(self):
        """Open the valve when someone is watching, close it when nobody is. Called after every change to
        the client set, from any thread: `valve_lock` serializes them and each call reads the truth again,
        so two racing subscribers cannot leave the valve in the state the loser wanted."""
        with self.valve_lock:
            want = self.count() > 0
            if want != self.valve_open:
                self.command("proxy on" if want else "proxy off")
                self.valve_open = want

    def set_ready(self, ready):
        """The recorder's own word on whether its proxy socket exists. A recorder on the camera path
        takes seconds to configure the sensor before it binds, and a viewer that asked in that window
        must wait rather than be told there is nothing there."""
        if ready:
            self.ready.set()
        else:
            self.ready.clear()

    def reopen_after_respawn(self):
        """A fresh recorder starts with the valve closed, whatever this object last told the old one."""
        with self.valve_lock:
            self.valve_open = False
        self._sync_valve()

    def subscribe(self):
        """Register a client and, if it is the first, open the valve and start reading the socket."""
        c = Client()
        with self.lock:
            first = not self.clients
            self.clients.append(c)
            if first:
                # A fresh event per run, so a reader that is still winding down cannot end this one.
                self.stop_flag = threading.Event()
                self.thread = threading.Thread(target=self._read_loop, args=(self.stop_flag,), daemon=True)
                self.thread.start()
        self._sync_valve()
        return c

    def unsubscribe(self, c):
        c.close()
        with self.lock:
            if c in self.clients:
                self.clients.remove(c)
            stop = self.stop_flag if not self.clients else None   # this run's event, not a later one's
        if stop is not None:
            stop.set()
        self._sync_valve()

    def shutdown(self):
        with self.lock:
            self.stop_flag.set()
            for c in self.clients:
                c.close()
            self.clients = []
        self._sync_valve()

    # ── the socket reader ────────────────────────────────────────────────────────────────────────────
    def _connect(self, stop, deadline):
        while not stop.is_set() and time.monotonic() < deadline:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(2.0)
                s.connect(self.sock_path)
                return s
            except OSError:
                stop.wait(CONNECT_RETRY_S)
        return None

    def _read_loop(self, stop):
        """Read the recorder's framed proxy socket and hand each access unit to every client.

        The valve was opened moments ago, so the first access unit is a keyframe; a client that joins
        later waits for the next one. The loop reconnects while there are still clients, because the
        recorder may be restarting (the supervisor respawns it with the same session)."""
        while not stop.is_set() and self.count():
            deadline = time.monotonic() + START_WAIT_S
            while not stop.is_set() and self.count() and not self.ready.wait(0.2) and time.monotonic() < deadline:
                pass
            # At least one attempt, even when the wait above used up the deadline: the socket is
            # often already there and a stale flag must not stop us from finding out.
            s = self._connect(stop, max(time.monotonic() + 2.0, min(deadline, time.monotonic() + 10.0)))
            if s is None:
                if self.count():
                    self.say("live: no proxy socket at %s after %.0f s" % (self.sock_path, START_WAIT_S))
                break
            buf = b""
            try:
                while not stop.is_set() and self.count():
                    try:
                        chunk = s.recv(READ_CHUNK)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    buf += chunk
                    while len(buf) >= HEADER.size:
                        magic, length, flags = HEADER.unpack_from(buf)
                        if magic != MAGIC or length > MAX_AU:
                            raise ValueError("proxy socket out of step: %r %d" % (magic, length))
                        if len(buf) < HEADER.size + length:
                            break
                        self._publish(buf[HEADER.size:HEADER.size + length], bool(flags & 1))
                        buf = buf[HEADER.size + length:]
            except (OSError, ValueError) as e:
                self.say("live: proxy socket: %s" % e)
            finally:
                s.close()
            if not stop.is_set() and self.count():
                self.say("live: proxy socket closed, reconnecting")
                stop.wait(CONNECT_RETRY_S)
        # Whatever ended the loop, no more bytes are coming through it: end the responses and drop the
        # clients, so the next subscriber counts as the first and starts a new run. Unless a later
        # subscriber already started one — then those clients belong to it, not to this thread.
        with self.lock:
            if stop is self.stop_flag:
                for c in self.clients:
                    c.close()
                self.clients = []
        self._sync_valve()

    def _publish(self, data, key):
        with self.lock:
            gone = []
            for c in self.clients:
                if not c.started:
                    if not key:
                        continue                  # nothing sent yet: wait for a decodable start
                    c.started = True
                c.put(data)
                if c.dropped:
                    gone.append(c)
            for c in gone:
                self.say("live: a client fell more than %d bytes behind and was dropped" % BACKLOG_CAP)
                self.clients.remove(c)
            stop = self.stop_flag if gone and not self.clients else None
        if stop is not None:
            stop.set()
        if gone:
            self._sync_valve()


def default_sock(ring):
    """Where the recorder puts its proxy socket. Under /tmp, not the ring: nothing about live view
    touches the eMMC."""
    return os.path.join("/tmp", "tile-live-%s.sock" % (os.path.basename(ring.rstrip("/")) or "ring"))
