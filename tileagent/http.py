"""HTTP/1.1 in front of the Agent: POST /tile.v1.Tile/<Method> with the proto3 JSON codec, the bulk GETs
(segment data with Range via sendfile, the live substream, session log, /metrics), 501 for undeclared
capabilities, 415 for a codec this node does not serve. Standard library only."""
import os
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .agent import ApiError
from .pb import pb
from google.protobuf.json_format import ParseError  # noqa: E402

SEG_RE = re.compile(r"^/v1/segments/(\d+)/data$")
LOG_RE = re.compile(r"^/v1/sessions/([^/]+)/log$")
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
JSON = "application/json"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    agent = None
    verbose = False

    def log_message(self, fmt, *args):
        if self.verbose:
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # ── replies ──────────────────────────────────────────────────────────────────────────────────────
    def _send(self, status, body, ctype=JSON, extra=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, e):
        self._send(e.status, pb.to_json(e.body()).encode())

    def _run(self, fn):
        try:
            fn()
        except ApiError as e:
            self._error(e)
        except (ParseError, ValueError) as e:
            self._error(ApiError(400, pb.Error.BAD_REQUEST, str(e)))
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            try:
                self._error(ApiError(500, pb.Error.INTERNAL, repr(e)))
            except OSError:
                pass

    # ── rpc ──────────────────────────────────────────────────────────────────────────────────────────
    def do_POST(self):
        self._run(self._post)

    def _post(self):
        if not self.path.startswith("/tile.v1.Tile/"):
            raise ApiError(404, pb.Error.NOT_FOUND, "no such path")
        name = self.path[len("/tile.v1.Tile/"):]
        if name not in pb.METHODS:
            raise ApiError(404, pb.Error.NOT_FOUND, "no such method %s" % name)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        ctype = (self.headers.get("Content-Type") or JSON).split(";")[0].strip()
        if body and ctype not in (JSON, "application/x-www-form-urlencoded"):     # the second is what `curl -d` sends
            raise ApiError(415, pb.Error.UNSUPPORTED_MEDIA_TYPE, "this node serves application/json only")
        req_cls, _ = pb.METHODS[name]
        req = pb.from_json(body.decode("utf-8"), req_cls)
        reply = getattr(self.agent, name)(req)
        if reply is None:
            raise ApiError(501, pb.Error.NOT_IMPLEMENTED, "%s not implemented" % name)
        self._send(200, pb.to_json(reply).encode())

    # ── bulk ─────────────────────────────────────────────────────────────────────────────────────────
    def do_GET(self):
        self._run(self._get)

    def do_HEAD(self):
        self._run(lambda: self._get(head=True))

    def _get(self, head=False):
        m = SEG_RE.match(self.path)
        if m:
            return self._file(self.agent.segment_path(int(m.group(1))), "application/octet-stream", head)
        m = LOG_RE.match(self.path)
        if m:
            return self._file(self.agent.session_log_path(m.group(1)), "application/x-ndjson", head)
        if self.path == "/metrics":
            return self._send(200, self.agent.metrics().encode(), "text/plain; version=0.0.4")
        if self.path == "/v1/live":
            return self._live(head)
        if self.path == "/v1/trigger/log":
            return self._file(self.agent.trigger_log_path(), "application/x-ndjson", head)
        raise ApiError(404, pb.Error.NOT_FOUND, "no such path")

    def _live(self, head):
        """The proxy substream, chunked, from the next IDR until the client goes away (§8.3a). One
        connection per viewer; the agent reads the recorder's socket once and fans it out."""
        fan = self.agent.live_stream()
        self.send_response(200)
        self.send_header("Content-Type", "video/h264")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if head:
            return
        client = fan.subscribe()
        try:
            while True:
                data = client.read()
                if data is None:
                    break
                if data:
                    self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
                    self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True
        finally:
            fan.unsubscribe(client)

    def _file(self, path, ctype, head):
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            start, end = 0, size - 1
            rng = self.headers.get("Range")
            status = 200
            if rng:
                m = RANGE_RE.match(rng.strip())
                if not m or (m.group(1) == "" and m.group(2) == ""):
                    raise ApiError(400, pb.Error.BAD_REQUEST, "bad Range")
                if m.group(1) == "":
                    start = max(0, size - int(m.group(2)))
                else:
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
            count = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(count))
            self.send_header("Accept-Ranges", "bytes")
            if status == 206:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.end_headers()
            if head:
                return
            self.wfile.flush()
            sent = 0
            while sent < count:
                n = self.connection.sendfile(f, start + sent, count - sent)
                if n <= 0:
                    break
                sent += n


def serve(agent, bind, port, verbose=False):
    Handler.agent = agent
    Handler.verbose = verbose
    srv = ThreadingHTTPServer((bind, port), Handler)
    srv.daemon_threads = True
    return srv
