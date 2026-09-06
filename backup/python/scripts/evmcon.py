#!/usr/bin/env python3
"""Run a command on the EVM Linux console over the FT4232H (ttyUSB2, 115200) and print its output.
usage: evmcon.py 'command' [timeout_s]"""
import os, sys, termios, time, select

PORT = os.environ.get("EVM_PORT", "/dev/ttyUSB2")
MARK = "__EVMCON_DONE__"

def open_port():
    fd = os.open(PORT, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    attr = termios.tcgetattr(fd)
    attr[0] = 0; attr[1] = 0
    attr[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    attr[3] = 0
    attr[4] = attr[5] = termios.B115200
    attr[6][termios.VMIN] = 0; attr[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, attr)
    termios.tcflush(fd, termios.TCIOFLUSH)
    return fd

def read_for(fd, seconds):
    out = b""; end = time.time() + seconds
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            try: out += os.read(fd, 65536)
            except BlockingIOError: pass
    return out

def main():
    cmd = sys.argv[1]; timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 8
    fd = open_port()
    os.write(fd, b"\x03"); read_for(fd, 0.5)       # kill whatever may be reading the console
    for _ in range(4):                          # get to a shell prompt, logging in if needed
        os.write(fd, b"\r"); banner = read_for(fd, 1.5)
        if b"login:" in banner:
            os.write(fd, b"root\r"); banner = read_for(fd, 2.0)
        if b"# " in banner:
            break
    else:
        print("[evmcon: no shell prompt]", file=sys.stderr)
    line = (cmd + "; echo " + MARK + "\r").encode()
    for k in range(0, len(line), 8):           # the console drops bytes on long fast writes
        os.write(fd, line[k:k + 8]); time.sleep(0.02)
    out = b""; end = time.time() + timeout
    while time.time() < end:
        out += read_for(fd, 0.2)
        if out.count(MARK.encode()) >= 2:      # echo of the command line, then the marker itself
            break
    text = out.decode(errors="replace")
    i = text.find("\n", text.find(MARK)); j = text.rfind(MARK)
    body = text[i + 1:j] if i >= 0 and j > i else text
    print(body.rstrip())
    if MARK.encode() not in out[-len(MARK)-40:] and out.count(MARK.encode()) < 2:
        print("[evmcon: timeout, partial output above]", file=sys.stderr)
    os.close(fd)

if __name__ == "__main__":
    main()
