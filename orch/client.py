"""The tile.v1 HTTP client, the node list and the parallel fan-out. Standard library plus the generated
classes; the one place the wire is spoken from the laptop."""
import concurrent.futures
import http.client
import os
import re
import time

from . import pb

JSON = "application/json"
DEFAULT_PORT = 8080


class TileError(Exception):
    """A non-200 reply: .status and .error (a parsed tile.v1 Error)."""

    def __init__(self, status, error):
        super().__init__("%d %s: %s" % (status, pb.Error.Code.Name(error.code), error.message))
        self.status, self.error = status, error


class Client:
    def __init__(self, host, timeout=15):
        self.host = host if ":" in host else "%s:%d" % (host, DEFAULT_PORT)
        self.timeout = timeout

    def __repr__(self):
        return self.host

    def rpc(self, name, req=None):
        req_cls, rep_cls = pb.METHODS[name]
        body = pb.to_json(req if req is not None else req_cls()).encode()
        c = http.client.HTTPConnection(self.host, timeout=self.timeout)
        try:
            c.request("POST", "/tile.v1.Tile/" + name, body=body, headers={"Content-Type": JSON})
            r = c.getresponse()
            data = r.read()
        finally:
            c.close()
        if r.status == 200:
            return pb.from_json(data.decode(), rep_cls)
        try:
            err = pb.from_json(data.decode(), pb.Error)
        except Exception:  # noqa: BLE001 — a body that is not an Error still needs reporting
            err = pb.Error(code=pb.Error.INTERNAL, message=data[:200].decode(errors="replace"))
        raise TileError(r.status, err)

    def open(self, path, rng=None):
        """Start a bulk GET; returns (status, headers dict, response, connection). The caller reads the
        response and closes the connection."""
        c = http.client.HTTPConnection(self.host, timeout=self.timeout)
        c.request("GET", path, headers={"Range": rng} if rng else {})
        r = c.getresponse()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, r, c

    def get(self, path):
        st, hdr, r, c = self.open(path)
        try:
            return st, hdr, r.read()
        finally:
            c.close()

    def time_samples(self, n=5):
        """[(wall_before_ns, wall_after_ns, Time)] for clock.calibrate."""
        out = []
        for _ in range(n):
            t0 = time.time_ns()
            t = self.rpc("GetTime")
            out.append((t0, time.time_ns(), t))
        return out


def node_dir(node_id):
    """The directory name for a node: its id without separators (a MAC becomes 12 hex digits)."""
    return re.sub(r"[^A-Za-z0-9._-]", "", node_id) or "node"


def load_nodes(spec, config=None):
    """--nodes 'h:p,h:p' wins; else orch.yaml (one fixed shape: 'nodes:' then '- host:port' lines, '#'
    comments), searched at `config`, ./orch.yaml, orch/orch.yaml."""
    if spec:
        return [h.strip() for h in spec.split(",") if h.strip()]
    here = os.path.dirname(os.path.abspath(__file__))
    for path in ([config] if config else []) + ["orch.yaml", os.path.join(here, "orch.yaml")]:
        if path and os.path.exists(path):
            hosts = []
            with open(path) as f:
                for line in f:
                    line = line.split("#", 1)[0].strip()
                    if line.startswith("- "):
                        hosts.append(line[2:].strip().strip("'\""))
            if not hosts:
                raise SystemExit("%s: no '- host:port' lines under nodes:" % path)
            return hosts
    raise SystemExit("no nodes: pass --nodes host:port,... or write orch.yaml (see orch/orch.yaml)")


def fanout(hosts, fn, say=print):
    """Run fn(Client) against every host in parallel; print one line per host, failures included.
    Returns the number of failures. fn returns the line to print (or a list of lines)."""
    clients = [Client(h) for h in hosts]
    failures = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(clients))) as ex:
        futures = [ex.submit(fn, c) for c in clients]
        for c, fut in zip(clients, futures):
            try:
                lines = fut.result()
            except TileError as e:
                failures += 1
                say("%-22s %s" % (c.host, e))
                continue
            except OSError as e:
                failures += 1
                say("%-22s unreachable: %s" % (c.host, e))
                continue
            for line in ([lines] if isinstance(lines, str) else lines or []):
                say("%-22s %s" % (c.host, line))
    return failures
