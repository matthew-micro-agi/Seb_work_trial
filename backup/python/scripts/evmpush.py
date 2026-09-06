#!/usr/bin/env python3
"""Copy a file to the EVM over the serial console (no network needed), verify by md5.
usage: evmpush.py LOCALFILE REMOTEPATH        (EVM_PORT env overrides /dev/ttyUSB2)"""
import base64, hashlib, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evmcon

def main():
    local, remote = sys.argv[1], sys.argv[2]
    data = open(local, "rb").read()
    fd = evmcon.open_port()
    os.write(fd, b"\x03"); evmcon.read_for(fd, 0.5)
    for _ in range(4):
        os.write(fd, b"\r"); banner = evmcon.read_for(fd, 1.5)
        if b"login:" in banner:
            os.write(fd, b"root\r"); banner = evmcon.read_for(fd, 2.0)
        if b"# " in banner:
            break
    else:
        sys.exit("no shell prompt on the console")
    os.write(fd, b"stty -echo; cat > /tmp/push.b64 <<'__EOF__'\r"); evmcon.read_for(fd, 0.3)
    b64 = base64.encodebytes(data)                  # 76-char lines
    for k in range(0, len(b64), 16):                # paced: the console drops bytes on bursts
        os.write(fd, b64[k:k + 16]); time.sleep(0.01)
    os.write(fd, b"__EOF__\r"); evmcon.read_for(fd, 1.0)
    os.write(fd, ("stty echo; base64 -d /tmp/push.b64 > %s && md5sum %s\r" % (remote, remote)).encode())
    out = evmcon.read_for(fd, 4.0).decode(errors="replace")
    os.close(fd)
    want = hashlib.md5(data).hexdigest()
    ok = want in out
    print("%s  %d bytes  md5 %s  %s" % (remote, len(data), want, "VERIFIED" if ok else "MISMATCH"))
    if not ok:
        print(out[-400:]); sys.exit(1)

if __name__ == "__main__":
    main()
