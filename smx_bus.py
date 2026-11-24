#!/usr/bin/env python3
"""
Decode the StepManiaX master -> panel data bus, as logged by the ESP32 firmware.

Bus format (observed on the wire, matches SMXManager.cpp in the SMX SDK)
------------------------------------------------------------------------
UART 250 kbaud 8N1.  The master forwards the host's lights commands to the
panels.  Frames are separated by idle time (no BREAK).  A frame holds one or
more packets back to back:

  '2' + 216 bytes   top half of every panel:    LEDs 0-7  (rim rows 0 and 1)
  '3' + 216 bytes   bottom half of every panel: LEDs 8-15 (rim rows 2 and 3)
  'w' + 130 bytes   config write (sent occasionally, ignored here)

216 bytes = 9 panels x 8 LEDs x RGB.  Panels are in reading order (top-left
to bottom-right), LEDs row-major inside the panel's 4x4 rim grid.  The SDK
scales colours by 2/3 before sending (max value 170), so we scale by 1.5 to
get back to 0-255.

'2' and '3' alternate at ~60 Hz, so a complete lights frame arrives at ~30 Hz.
This stage only sends the 16-LED commands; the 3x3 core ('4' packets) is
not present on the bus, so core LEDs stay dark.

Usage
-----
  python3 smx_bus.py captures/raw2.txt            # summary of a log
  python3 smx_bus.py --serial /dev/cu.usbmodem14301   # live summary
"""

from __future__ import annotations

import re
import sys
import time

import numpy as np

PANELS = 9
RIM_LEDS = 16
HALF_BYTES = PANELS * 8 * 3          # 216
# '4' = 3x3 core (9 panels x 9 LEDs x RGB). Never seen on the real bus; only
# in logs exported from lights PNGs (smx_lights_player.py --export-bus-log).
PACKET_LEN = {ord("2"): 1 + HALF_BYTES, ord("3"): 1 + HALF_BYTES, ord("w"): 131,
              ord("4"): 1 + PANELS * 9 * 3}
COLOR_SCALE = 1.5                    # undo the SDK's x0.6666 scaling

_HDR = re.compile(r"#(\d+) t=\+([\d.]+)ms len=(\d+)")
_HEX = re.compile(r"\s+[0-9a-f]{4}:((?: [0-9a-f]{2})+)\s*$")


# ------------------------------------------------------------ parsing ----
def parse_log_lines(lines):
    """Yield (t_seconds, frame_bytes) from the firmware's hex-dump output.

    Timestamps are the running sum of the firmware's 't=+' deltas.  Frames
    whose byte count doesn't match their header (cut-off log) are skipped.
    """
    t = 0.0
    cur = None
    for line in lines:
        m = _HDR.search(line)
        if m:
            dt = float(m[2]) / 1000.0
            if dt < 5:                    # first frame after boot has a huge dt
                t += dt
            cur = (t, bytearray(), int(m[3]))
            continue
        m = _HEX.match(line)
        if m and cur is not None:
            cur[1].extend(bytes.fromhex(m[1].replace(" ", "")))
            if len(cur[1]) >= cur[2]:     # complete: emit right away (live use)
                if len(cur[1]) == cur[2]:
                    yield cur[0], bytes(cur[1])
                cur = None


def split_packets(frame: bytes):
    """Split one bus frame into (cmd_char, payload) packets."""
    out = []
    i = 0
    while i < len(frame):
        n = PACKET_LEN.get(frame[i])
        if n is None or i + n > len(frame):
            break                          # unknown / truncated: drop rest
        out.append((chr(frame[i]), frame[i + 1:i + n]))
        i += n
    return out


# ----------------------------------------------------------- decoding ----
class LightsDecoder:
    """Assemble '2' / '3' halves into full (9, 16, 3) uint8 light frames."""

    def __init__(self):
        self.state = np.zeros((PANELS, RIM_LEDS, 3), np.uint8)
        self._have_top = False

    def feed(self, cmd: str, payload: bytes):
        """Returns a full frame copy when a '3' completes one, else None."""
        if cmd not in "23":
            return None
        half = np.frombuffer(payload, np.uint8).reshape(PANELS, 8, 3)
        half = np.minimum(half.astype(np.uint16) * 3 // 2, 255).astype(np.uint8)
        if cmd == "2":
            self.state[:, 0:8] = half
            self._have_top = True
            return None
        self.state[:, 8:16] = half
        if not self._have_top:
            return None
        self._have_top = False
        return self.state.copy()


def decode_frames(frames):
    """[(t, frame_bytes)] -> (times array, uint8 (N, 9, 16, 3))."""
    dec = LightsDecoder()
    ts, out = [], []
    for t, frame in frames:
        for cmd, payload in split_packets(frame):
            full = dec.feed(cmd, payload)
            if full is not None:
                ts.append(t)
                out.append(full)
    if not out:
        return np.zeros(0), np.zeros((0, PANELS, RIM_LEDS, 3), np.uint8)
    return np.array(ts), np.stack(out)


def load_log(path: str):
    with open(path, errors="replace") as fh:
        return decode_frames(parse_log_lines(fh))


def resample(times, frames, fps: int):
    """Hold-last-value resample of irregular frames onto a fixed fps grid."""
    if len(frames) == 0:
        return frames
    t0 = times[0]
    n = int((times[-1] - t0) * fps) + 1
    grid = t0 + np.arange(n) / fps
    idx = np.clip(np.searchsorted(times, grid, side="right") - 1, 0, len(frames) - 1)
    return frames[idx]


# --------------------------------------------------------------- live ----
def serial_lines(port: str, baud: int = 115200):
    """Yield text lines from the ESP32 logger forever."""
    import serial  # pyserial

    s = serial.Serial(port, baud, timeout=0.2)
    buf = b""
    while True:
        buf += s.read(4096)
        *lines, buf = buf.split(b"\n")
        for ln in lines:
            yield ln.decode(errors="replace")


def _summary(times, frames):
    if len(frames) == 0:
        print("no light frames decoded")
        return
    span = times[-1] - times[0]
    fps = (len(frames) - 1) / span if span > 0 else 0
    lit = (frames.max(axis=(1, 2, 3)) > 0).mean() * 100
    print(f"{len(frames)} light frames over {span:.1f}s ({fps:.1f} fps), "
          f"{lit:.0f}% non-black, max value {frames.max()}")


def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("log", nargs="?", help="firmware log file")
    ap.add_argument("--serial", help="read live from this port instead")
    a = ap.parse_args(argv)
    if a.serial:
        dec = LightsDecoder()
        n, t0 = 0, time.time()
        for _, frame in parse_log_lines(serial_lines(a.serial)):
            for cmd, payload in split_packets(frame):
                if dec.feed(cmd, payload) is not None:
                    n += 1
            if time.time() - t0 >= 1:
                print(f"{n} light frames/s")
                n, t0 = 0, time.time()
    elif a.log:
        _summary(*load_log(a.log))
    else:
        ap.error("pass a log file or --serial PORT")


if __name__ == "__main__":
    sys.exit(main())
