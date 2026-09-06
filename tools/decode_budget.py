#!/usr/bin/env python3
"""How many proxy streams the viewer can decode at once, measured in the decoder the viewer really uses.

  tools/decode_budget.py PROXY.h264 [--counts 1,2,4,6,10,16] [--seconds 12] [--chromium chromium]

Foxglove Studio is Electron, so its Image panel decodes through Chromium's WebCodecs — which is why the
proxy substream is H.264 and the recording is not (`DESIGN_RING_CONTROL.md` §8.3a). This builds a page
that feeds N copies of a recorded proxy stream to N `VideoDecoder`s at their real frame rate, runs it in
headless Chromium, and prints how many frames came back out of each run.

It measures decoding only: Foxglove also paints every frame, so treat the answer as the ceiling. Run it on
the machine that will run Foxglove.
"""
import argparse
import base64
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sei", "python"))
import sei  # noqa: E402

PAGE = """<html><body><script src="stream.js"></script><script>
const raw = Uint8Array.from(atob(DATA), c => c.charCodeAt(0));
const FPS = %(fps)d, SECONDS = %(seconds)g, COUNTS = %(counts)s;

function makeDecoder(stat) {
  const d = new VideoDecoder({
    output: f => { stat.out++; stat.lag += performance.now() - f.timestamp / 1000; f.close(); },
    error: e => { stat.err = String(e); },
  });
  d.configure({ codec: "%(codec)s", codedWidth: %(w)d, codedHeight: %(h)d, optimizeForLatency: true });
  return d;
}

async function run(n) {
  const stats = [], decoders = [];
  for (let i = 0; i < n; i++) { const s = { out: 0, in: 0, lag: 0, err: "" }; stats.push(s); decoders.push(makeDecoder(s)); }
  let idx = 0;
  const t0 = performance.now();
  await new Promise(done => {
    const timer = setInterval(() => {
      const now = performance.now();
      if (now - t0 > SECONDS * 1000) { clearInterval(timer); done(); return; }
      while (!AU[idx][2]) idx = (idx + 1) %% AU.length;    // a decoder can only start on a keyframe
      const [b, e, key] = AU[idx];
      const chunk = new EncodedVideoChunk({ type: key ? "key" : "delta", timestamp: Math.round(now * 1000), data: raw.subarray(b, e) });
      for (let i = 0; i < n; i++) { stats[i].in++; try { decoders[i].decode(chunk); } catch (err) { stats[i].err = String(err); } }
      idx = (idx + 1) %% AU.length;
    }, 1000 / FPS);
  });
  for (const d of decoders) { try { await d.flush(); } catch (e) { /* a decoder in error cannot flush */ } d.close(); }
  const fed = stats[0].in, out = stats.reduce((a, s) => a + s.out, 0), want = fed * n;
  const lag = out ? stats.reduce((a, s) => a + s.lag, 0) / out : 0;
  const errs = stats.filter(s => s.err).length;
  console.log("RESULT " + [n, fed, out, want, (100 * out / want).toFixed(1), lag.toFixed(1), errs,
              errs ? stats.find(s => s.err).err : ""].join("|"));
}

(async () => {
  if (!(await VideoDecoder.isConfigSupported({ codec: "%(codec)s", codedWidth: %(w)d, codedHeight: %(h)d })).supported)
    console.log("RESULT UNSUPPORTED");
  else for (const n of COUNTS) await run(n);
  console.log("RESULT DONE");
})();
</script></body></html>
"""


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("stream", help="an Annex-B H.264 file, e.g. a saved GET /v1/live or test_m4b.sh's proxy.h264")
    p.add_argument("--counts", default="1,2,4,6,10,16")
    p.add_argument("--seconds", type=float, default=12)
    p.add_argument("--fps", type=int, default=15)
    p.add_argument("--width", type=int, default=728)
    p.add_argument("--height", type=int, default=544)
    p.add_argument("--codec", default="avc1.42E01E", help="constrained baseline 3.0, what the Wave5 and x264enc emit here")
    p.add_argument("--chromium", default="chromium")
    a = p.parse_args()

    data = open(a.stream, "rb").read()
    aus = sei.split(data, "h264")
    offsets = [[b, e, 1 if any(sei.is_idr(n, "h264") for n in nals) else 0] for b, e, nals in aus]
    if not any(o[2] for o in offsets):
        sys.exit("%s has no IDR: not a stream a decoder can start on" % a.stream)
    counts = [int(x) for x in a.counts.split(",")]
    print("%s: %d access units, %d keyframes, %d bytes; %dx%d, %d fps, %g s per run"
          % (a.stream, len(offsets), sum(o[2] for o in offsets), len(data), a.width, a.height, a.fps, a.seconds))

    # Chromium gives WebCodecs only to a secure context, and file:// is one; a snap chromium can read the
    # user's home but not /tmp, so the page goes under the home directory.
    d = tempfile.mkdtemp(prefix="decode-budget-", dir=os.path.expanduser("~"))
    try:
        with open(os.path.join(d, "stream.js"), "w") as f:
            f.write("const AU = %s;\nconst DATA = '%s';\n" % (offsets, base64.b64encode(data).decode()))
        with open(os.path.join(d, "page.html"), "w") as f:
            f.write(PAGE % {"fps": a.fps, "seconds": a.seconds, "counts": counts, "codec": a.codec,
                            "w": a.width, "h": a.height})
        out = os.path.join(d, "log.txt")
        with open(out, "wb") as log:
            proc = subprocess.Popen([a.chromium, "--headless=new", "--no-sandbox", "--disable-gpu",
                                     "--enable-logging=stderr", "file://" + os.path.join(d, "page.html")],
                                    stdout=log, stderr=subprocess.STDOUT)
            deadline = a.seconds * len(counts) + 60
            try:
                proc.wait(timeout=deadline)
            except subprocess.TimeoutExpired:
                pass                                   # the page never navigates away; the results are in the log
            finally:
                proc.kill()
                proc.wait()
        lines = re.findall(r'"RESULT ([^"]*)"', open(out, errors="replace").read())
        if "UNSUPPORTED" in lines:
            sys.exit("this Chromium has no H.264 in WebCodecs: %s" % a.codec)
        print("%8s %10s %12s %18s %8s" % ("streams", "fed each", "decoded", "of wanted", "lag ms"))
        for line in lines:
            if line == "DONE" or "|" not in line:
                continue
            n, fed, got, want, pct, lag, errs, msg = line.split("|")
            print("%8s %10s %12s %10s (%5s%%) %8s%s" % (n, fed, got, want, pct, lag, "  " + msg if msg else ""))
        if "DONE" not in lines:
            print("(the sweep did not finish; raise --seconds or look at %s)" % out)
    finally:
        for name in os.listdir(d):
            os.unlink(os.path.join(d, name))
        os.rmdir(d)


if __name__ == "__main__":
    main()
