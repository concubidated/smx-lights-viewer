#!/usr/bin/env python3
"""
SMX stage LED player / simulator.

Plays a StepManiaX "lights video" PNG back at 30 fps on a 3x3 grid of panels,
25 LEDs per panel (16 rim + 9 core), 225 LEDs total, laid out like the hardware:

    00  01  02  03
      16  17  18
    04  05  06  07
      19  20  21
    08  09  10  11
      22  23  24
    12  13  14  15

    python3 -m pip install pygame-ce pillow numpy
    python3 smx_lights_player.py
    python3 smx_lights_player.py "Golden/Lights video (seq 24x30).png" --loop
    python3 smx_lights_player.py --no-audio --scale 1.4

    # live from the ESP32 bus logger (see smx_bus.py), optionally recording
    python3 smx_lights_player.py --serial /dev/cu.usbmodem14301 --record cap.txt
    # replay a recorded bus log
    python3 smx_lights_player.py --bus-log cap.txt --loop

The stage bus only carries the 16 rim LEDs per panel ('2'/'3' commands), so
core LEDs stay dark in bus playback.

----------------------------------------------------------------------------
File format (reverse engineered from Lights video (seq 24x30).png)
----------------------------------------------------------------------------
The PNG is a frame strip: **every pixel row is one video frame** at 30 fps, so
height / 30 = clip length (3047 rows -> ~101.6 s, the full song).  Only the
left ~501 columns carry data; the rest is black padding.

Each row holds exactly 225 LED colours in two blocks.  Every value is ONE
pixel wide, and each block repeats its values a second time right after:

  OUTER block   x =   0 .. 287   12 cells of 24 px.  A cell's first 12 cols are
                                 12 values of the 12 x 12 stage picture (the
                                 3x3 panels in reading order top-left ..
                                 bottom-right, each panel giving its 4x4 rim
                                 block row-major); cols 12..23 repeat them.
                                 12 cells x 12 = 144 values = 9 x 16.

  INNER block   x = 291 .. 500   9 separate 18 px cells (6 px black gaps): the
                                 first 9 cols of a cell are one 3x3 core of a
                                 9x9 stage picture in the same panel order,
                                 cols 9..17 repeat them.  9 x 9 = 81 values.

The PNGs are lossily compressed, so flat areas and the two copies drift by a
few RGB steps; the decoder averages the two copies of each value.

So the file stores the 4x4 data first, then the 3x3 data, each going from top
left to bottom right row by row over the stage -- a picture of the stage, not
a per-panel chain list.

Verification (Golden/tools/verify_order.py): under this reading the colour
field is smoother inside a panel's 4x4 than across panel boundaries, and that
gap appears only with the row-major reading-order cut (a column-major cut
swaps the axes).  Independently, each panel's 3x3 core colour matches the mean
of its own 4x4 rim better than any mismatched panel's rim (32.8 vs 39.9),
which ties the two blocks to the same panels.

Inside a panel the 16 rim values fill LED numbers 00-15 of the hardware map
above (the 4 rim rows of the 7-row physical lattice) and the 9 core values
fill LED numbers 16-24 (the 3 interleaved rows).

Control keys
------------
  SPACE  play / pause              LEFT / RIGHT   step one frame (SHIFT: 1 s)
  UP / DOWN  seek +-5 s            HOME           restart
  L  loop                          [ / ]          playback speed
  click or drag the progress bar to seek     ESC / Q quit
"""

from __future__ import annotations

import argparse
import math
import os
import sys

import numpy as np
from PIL import Image

FPS = 30
PANELS = 9
PANEL_GRID = 3
LEDS_PER_PANEL = 25
OUTER_LEDS = 16
CORE_LEDS = 9

# 4x4 rim block, row-major -> position in the panel's 7x7 physical lattice
# (rim LEDs sit on even rows; see the hardware map in the docstring).
RIM_R = np.array([0, 0, 0, 0, 2, 2, 2, 2, 4, 4, 4, 4, 6, 6, 6, 6])
RIM_C = np.array([0, 2, 4, 6, 0, 2, 4, 6, 0, 2, 4, 6, 0, 2, 4, 6])
# 3x3 core block, row-major -> odd rows / columns of the same lattice.
CORE_R = np.array([1, 1, 1, 3, 3, 3, 5, 5, 5])
CORE_C = np.array([1, 3, 5, 1, 3, 5, 1, 3, 5])


# =============================================================== decoding ====
def _canonical_strip(img: np.ndarray):
    """Normalize a lights strip; returns (img, sparse).

    Song strips paint both black and white grounds but repeat every value in
    the cell's second half ("twin"); pattern-library strips store each value
    once in 12-px groups.  White-ground library strips paint lit as dark on
    white, so they are inverted here so that later steps see black = off.
    """
    width = img.shape[1]
    if width < 24:
        return img, False
    def _sparse(runs):
        # library packing: short value groups, never one 288-px span; colour
        # files antialias each group into off-grid shards, so key on the
        # group size instead of alignment.
        return bool(runs) and runs[0][0] < 510 and \
            all(r[1] - r[0] <= 48 for r in runs[:16])

    runs = _used_runs(img)
    if _sparse(runs):
        return img, True          # black-ground library packing
    gap_white = img[:, 12:24].max(axis=2).min() > 200
    if gap_white:
        inv = (255 - img).astype(np.uint8)
        if _sparse(_used_runs(inv)):
            return inv, True      # white-ground library packing
    return img, False


def _used_runs(img: np.ndarray, thresh: int = 2):
    """Contiguous x ranges that are not entirely black -> [(x0, x1), ...]."""
    used = img.max(axis=(0, 2)) > thresh
    runs, i, n = [], 0, used.size
    while i < n:
        if not used[i]:
            i += 1
            continue
        j = i
        while j < n and used[j]:
            j += 1
        runs.append((i, j))
        i = j
    return runs


def _half_equal(a: np.ndarray, b: np.ndarray, tol: int = 8, frac: float = 0.9) -> bool:
    """Do two equally sized pixel blocks match on most rows (median per column)?

    The lights PNGs are lossily compressed (DCT ringing drifts single pixels
    by a few RGB steps), so exact equality fails on some rows/files; taking
    the temporal median per column pair rejects the noise.
    """
    med = np.median(np.abs(a.astype(np.int32) - b.astype(np.int32)), axis=0)
    return (med <= tol).mean() >= frac


def _cell_width(strip: np.ndarray, tol: int = 64) -> int:
    """Values per cell: smallest p whose first p columns repeat right after.

    Each cell prints its values twice (cols 0..p-1 == cols p..2p-1 up to
    compression noise), so on the 288-col outer block this returns 12 and on
    an 18-col inner cell it returns 9.
    """
    width = strip.shape[1]
    # scan large to small: blank / flat columns make p=1 "pass" spuriously,
    # while the true cell structure is the biggest period that repeats.
    for p in range(width // 2, 0, -1):
        if width % (2 * p) == 0 and _half_equal(strip[:, :p], strip[:, p:2 * p], tol):
            return p
    return width


def detect_layout(img: np.ndarray, verbose: bool = False) -> dict:
    """
    Work out the cell positions from the image itself.

    Returns {"outer": {"starts": [...], "values": 12},
             "inner": {"starts": [...], "values": 9}}
    where a "start" is the x offset of a cell and "values" the number of
    single-px values per cell (the same values repeat in the cell's second
    half, which is what _cell_width keys on).
    """
    img, sparse = _canonical_strip(img)
    if sparse:
        width = img.shape[1]
        return {"outer": {"starts": [k * 24 for k in range(12)],
                          "values": 12, "dup": False},
                "inner": {"starts": [291 + k * 24 for k in range(9)]
                          if width > 300 else [], "values": 9, "dup": False}}

    runs = _used_runs(img)
    if len(runs) < 2:
        raise SystemExit("could not find two light blocks in the image")

    outer_run, inner_runs = runs[0], runs[1:]
    outer_span = outer_run[1] - outer_run[0]
    strip = img[:, outer_run[0]:outer_run[1]]
    # Rainbow library strips store 144 values adjacency-duplicated (2 px each)
    # across the whole 288-px block instead of a twinned 144+144 layout.
    # Check the canonical twin-12 layout (12 values + exact repeat per 24-px
    # cell) first: smooth songs make neighbouring columns look alike at the
    # loose adj tolerance, so adj must only win when twin-12 doesn't fit.
    twin12 = outer_span % 24 == 0 and all(
        _half_equal(strip[:, k * 24:k * 24 + 12], strip[:, k * 24 + 12:k * 24 + 24])
        for k in range(outer_span // 24))
    adj = not twin12 and outer_span == 288 and \
        not _half_equal(strip[:, :144], strip[:, 144:], tol=64) and \
        _half_equal(strip[:, 0:287:2], strip[:, 1:288:2], tol=64)
    if adj:
        outer_w, outer_n = 144, 1
    else:
        outer_w = _cell_width(strip)
        outer_n = outer_span // (2 * outer_w)
    # dim songs make large periods pass spuriously (24x6 or 36x4 also multiply
    # to 144); the format always stores 144 rim values in 12 cells of 24 px,
    # so re-check the canonical split whenever the scan produced another one.
    if not adj and outer_w != 12 and outer_span % 24 == 0:
        strip = img[:, outer_run[0]:outer_run[1]]
        n = outer_span // 24
        ok = all(_half_equal(strip[:, k * 24:k * 24 + 12],
                             strip[:, k * 24 + 12:k * 24 + 24])
                 for k in range(n))
        if ok:
            outer_w, outer_n = 12, n

    if len(inner_runs) >= 2:
        # one run per inner cell, separated by black gaps; some library
        # strips append a second picture past x=510 which we ignore.
        inner_runs = [r for r in inner_runs if r[0] < 510][:9]
        inner_w = 9 if adj else \
            _cell_width(img[:, inner_runs[0][0]:inner_runs[0][1]])
        inner_starts = [r[0] for r in inner_runs]
    else:
        x0, x1 = inner_runs[0]
        inner_w = _cell_width(img[:, x0:x1])
        inner_starts = [x0 + k * 2 * inner_w
                        for k in range((x1 - x0) // (2 * inner_w))]

    layout = {
        "outer": {"starts": [outer_run[0]], "values": 144, "dup": "adj"}
        if adj else
        {"starts": [outer_run[0] + k * 2 * outer_w for k in range(outer_n)],
         "values": outer_w},
        "inner": {"starts": inner_starts, "values": inner_w,
                  **({"dup": "adj"} if adj else {})},
    }

    total_o = outer_n * outer_w
    total_i = len(inner_starts) * inner_w
    if total_o != PANELS * OUTER_LEDS:
        raise SystemExit(
            f"outer block holds {total_o} values, expected "
            f"{PANELS * OUTER_LEDS} (9 panels x {OUTER_LEDS} rim LEDs)")
    if total_i != PANELS * CORE_LEDS:
        raise SystemExit(
            f"inner block holds {total_i} values, expected "
            f"{PANELS * CORE_LEDS} (9 panels x {CORE_LEDS} core LEDs)")

    if verbose:
        print(f"detected layout: outer {outer_w} cells x {outer_n} groups "
              f"(x={outer_run[0]}..{outer_run[1]}), "
              f"inner {inner_w} cells x {len(inner_starts)} groups "
              f"(x={inner_starts[0]}..{inner_runs[-1][1]})")
    return layout


def _gather_cols(img: np.ndarray, starts, width: int, dup: bool = True) -> np.ndarray:
    """(frames, cells*width, 3) sampling the first copy of every value.

    With dup=True each value column is averaged with its twin in the cell's
    second half, which cancels most of the lossy-compression noise.
    """
    idx = np.array([s + j for s in starts for j in range(width)])
    vals = img[:, idx].astype(np.int32)
    if dup == "adj":
        twin = img[:, idx + 1].astype(np.int32)
        vals = (vals + twin + 1) // 2
    elif dup:
        twin = img[:, idx + width].astype(np.int32)
        vals = (vals + twin + 1) // 2
    return vals.astype(np.uint8)


def decode(path: str, trim: bool = True, layout: dict | None = None,
           verbose: bool = True):
    """
    Lights PNG -> (uint8 (frames, 9, 7, 7, 3), layout dict).

    The third axis is the panel's physical 7x7 lattice (rim on even rows,
    core on odd rows, as in the hardware map).  Unused lattice points are
    black.  The 144 outer values are a stage picture in reading order:
    panels top-left..bottom-right, each panel's 4x4 rim row-major; the 81
    inner values likewise as 3x3 cores.
    """
    img = np.asarray(Image.open(path).convert("RGB"))
    if img.ndim != 3 or img.shape[0] < 2:
        raise SystemExit("expected a frame strip, one pixel row per frame")
    img, _ = _canonical_strip(img)

    layout = layout or detect_layout(img, verbose=verbose)
    frames = img.shape[0]
    o, i = layout["outer"], layout["inner"]

    # The 144 rim values are one 12x12 picture of the stage rows (4 rim rows
    # per panel row), and the 81 core values one 9x9 picture; each panel
    # takes its 4x4 / 3x3 tile out of those pictures.
    rim = _gather_cols(img, o["starts"], o["values"], o.get("dup", True))
    rim = rim.reshape(frames, 12, 12, 3)[:, 0:12:1].reshape(
        frames, PANEL_GRID, 4, PANEL_GRID, 4, 3) \
        .transpose(0, 1, 3, 2, 4, 5)
    if i["starts"]:
        core = _gather_cols(img, i["starts"], i["values"], i.get("dup", True))
        core = core.reshape(frames, PANEL_GRID, 3, PANEL_GRID, 3, 3) \
            .transpose(0, 1, 3, 2, 4, 5)
    else:
        core = np.zeros((frames, PANEL_GRID, PANEL_GRID, 3, 3, 3), np.uint8)

    panels = np.zeros((frames, PANELS, 7, 7, 3), np.uint8)
    for p in range(PANELS):
        pr, pc = divmod(p, PANEL_GRID)
        panels[:, p, RIM_R, RIM_C] = rim[:, pr, pc].reshape(frames, 16, 3)
        panels[:, p, CORE_R, CORE_C] = core[:, pr, pc].reshape(frames, 9, 3)

    if trim:
        lit = np.flatnonzero(panels.max(axis=(1, 2, 3, 4)) > 0)
        if lit.size:
            panels = panels[:lit[-1] + 1]

    return panels, layout


# ================================================================ helpers ====
def _shade(col, f: float):
    return (min(255, int(col[0] * f)), min(255, int(col[1] * f)),
            min(255, int(col[2] * f)))


def _audio_next_to(png: str) -> str | None:
    """First audio file in the same directory as the lights PNG."""
    d = os.path.dirname(png)
    for name in sorted(os.listdir(d)):
        if name.lower().endswith((".ogg", ".mp3", ".wav")):
            return os.path.join(d, name)
    return None


def find_defaults() -> tuple[str | None, str | None]:
    """Locate the lights PNG and song next to this script (or in ./Golden)."""
    here = os.path.dirname(os.path.abspath(__file__))
    png = audio = None
    seen = []
    for root in (os.path.join(here, "Golden"), here, os.getcwd(),
                 os.path.join(os.getcwd(), "Golden")):
        if not os.path.isdir(root) or root in seen:
            continue
        seen.append(root)
        names = sorted(os.listdir(root))
        for name in names:
            if png is None and name.lower().endswith(".png") and "light" in name.lower():
                png = os.path.join(root, name)
        if png and audio is None:
            for name in names:
                if name.lower().endswith((".ogg", ".mp3", ".wav")):
                    audio = os.path.join(root, name)
                    break
    return png, audio


# ============================================================ pygame player ==
class Player:
    PITCH = 15          # lattice step (the 7x7 grid), rim/core alternate on it
    LED_R = 6
    PANEL_GAP = 30
    MARGIN = 26
    HUD_H = 108

    def __init__(self, frames, fps: int, audio_path=None, loop=False,
                 rate: float = 1.0, audio_offset: float = 0.0, scale: float = 1.0,
                 bloom: bool = True):
        try:
            import pygame
        except ImportError:
            sys.exit("pygame is missing:\n"
                     "  python3 -m pip install pygame-ce pillow numpy")
        self.pg = pygame
        pygame.init()

        self.frames = frames
        self.n = len(frames)
        self.fps = fps
        self.dur = self.n / fps
        self.loop = loop
        self.rate = rate
        self.audio_offset = audio_offset
        self.bloom = bloom

        pygame.display.set_caption(f"SMX stage - 3x3 panels x {LEDS_PER_PANEL} LEDs - {fps} fps")

        # the usable display area caps everything; macOS may otherwise
        # restore a window taller than the screen and clip the pad
        disp = pygame.display.get_desktop_sizes()
        self._dw, self._dh = (int(disp[0][0]), int(disp[0][1])) if disp else (1440, 900)

        # always start at a calm, fitted size; RESIZABLE then lets the user
        # grow/shrink live and _layout follows
        s0 = min(scale, (self._dh - 120) / self._window_h_for(1.0),
                 (self._dw - 24) / self._window_w_for(1.0))
        self.screen = pygame.display.set_mode(
            (self._window_w_for(s0), self._window_h_for(s0)),
            flags=pygame.RESIZABLE)
        self.clock = pygame.time.Clock()

        self._layout(*self.screen.get_size())   # derive pad size from window

        # ---- audio ------------------------------------------------------
        self.audio_path = audio_path
        self.audio_on = False
        self._pos_ref = 0.0        # track position when the last play() started
        if audio_path:
            self._init_audio()

        # ---- transport --------------------------------------------------
        self.t = 0.0
        self.playing = True
        self.running = True
        self._resume_after_drag = False
        self.bar = pygame.Rect(0, 0, 1, 1)
        self._start_audio(0.0)

    # -- sizing -----------------------------------------------------------
    def _hud_h(self, font_h: int) -> int:
        # status line + progress bar + gaps
        return int(font_h + 10 + 8 + 16)

    # exact window<->scale formulas; _layout() inverts them so a window of
    # size _window_for(s) reproduces scale s with (near) zero slack.
    # A panel is: 6*PITCH of LED centres, centred in the board, with the LED
    # halo (LED_R+3), a gutter and the border on each side.
    def _window_w_for(self, s: float) -> int:
        panel = 6 * 15 * s + 2 * (6 * s + 3 + 4 + 2)
        return int(3 * panel + 2 * 24 * s + 2 * 16 * s)

    def _window_h_for(self, s: float) -> int:
        return self._window_w_for(s) + self._hud_h(max(12, int(round(15 * s))))

    def _layout(self, W: int, H: int):
        """Fit the whole pad + HUD into a WxH window and rebuild surfaces.

        The window's tighter axis sets the pad scale; any slack left on the
        other axis becomes centring margin, so the grid always sits centred.
        """
        pg = self.pg
        self.W, self.H = W, H
        W = min(W, self._dw - 12)                 # never trust a restored
        H = min(H, self._dh - 96)                 # size beyond the display

        s = min((W - 12) / self._window_w_for(1.0),
                (H - 12) / self._window_h_for(1.0))
        s = max(0.4, min(4.0, s))

        self.PITCH = max(5, int(round(15 * s)))
        self.LED_R = max(3, int(round(6 * s)))
        self.PANEL_GAP = max(8, int(round(24 * s)))
        self.MARGIN = max(8, int(round(16 * s)))

        # board = 6*PITCH of LED centres + halo + gutter + border each side
        self.panel_px = 6 * self.PITCH + 2 * (self.LED_R + 9)
        self.stage = PANEL_GRID * self.panel_px + (PANEL_GRID - 1) * self.PANEL_GAP
        hf = max(11, min(22, int(round(15 * s))))
        hud = self._hud_h(hf)
        self.sx = max(4, (W - self.stage) // 2)
        self.sy = max(4, (H - hud - self.stage) // 2)
        self._hud_h_px = hud

        self.glow = None
        if self.bloom:
            self.glow = pg.Surface((max(8, self.stage // 2), max(8, self.stage // 2)))

        self.font = pg.font.SysFont("menlo,consolas,dejavusansmono", hf)
        self.font_big = pg.font.SysFont("menlo,consolas,dejavusansmono",
                                        hf + 6, bold=True)

    # -- audio clock ------------------------------------------------------
    # The audio device is the most accurate clock available, so while it is
    # playing we follow it and correct the video clock if it drifts.
    def _init_audio(self):
        try:
            self.pg.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
            self.pg.mixer.music.load(self.audio_path)
            self.pg.mixer.music.set_volume(0.9)
            self.audio_on = True
        except Exception as exc:                                   # noqa: BLE001
            print(f"[audio] disabled: {exc}")
            self.audio_on = False

    def _start_audio(self, pos: float, play: bool | None = None):
        self._pos_ref = pos
        if not self.audio_on:
            return
        play = self.playing if play is None else play
        pos = max(0.0, min(pos, max(0.0, self.dur - 0.05)))
        try:
            self.pg.mixer.music.stop()
            self.pg.mixer.music.play(loops=0, start=pos)
            self._pos_ref = pos
            if not play:
                self.pg.mixer.music.pause()
        except Exception as exc:                                   # noqa: BLE001
            print(f"[audio] {exc}")

    def _audio_time(self) -> float | None:
        """Current position of the audio track, or None if not authoritative."""
        if not (self.audio_on and self.pg.mixer.get_init()):
            return None
        ms = self.pg.mixer.music.get_pos()
        if ms is None or ms < 0:
            return None
        return self._pos_ref + ms / 1000.0

    def _advance(self, dt: float):
        """Move the play head; follow the audio clock when it is running."""
        if not self.playing:
            return
        at = self._audio_time() if abs(self.rate - 1.0) < 1e-6 else None
        if at is None:
            self.t += dt * self.rate
            return
        at += self.audio_offset
        if abs(at - self.t) > 0.12:            # real drift: jump, don't lag
            self.t = at
        else:                                  # tiny drift: sneak up on it
            self.t += dt + (at - self.t) * 0.15

    def _seek(self, t: float, play: bool | None = None):
        self.t = max(0.0, min(t, self.dur))
        self._start_audio(self.t, play)

    def _resync(self):
        self._start_audio(self.t)

    def _set_playing(self, playing: bool):
        self.playing = playing
        if not self.audio_on:
            return
        try:
            if playing:
                if not self.pg.mixer.music.get_busy():
                    self._start_audio(self.t, play=True)
                else:
                    self.pg.mixer.music.unpause()
            else:
                self.pg.mixer.music.pause()
        except Exception:                                          # noqa: BLE001
            pass

    # -- input ------------------------------------------------------------
    def _events(self):
        pg = self.pg
        for e in pg.event.get():
            if e.type == pg.QUIT:
                self.running = False
            elif e.type == pg.KEYDOWN:
                k = e.key
                if k in (pg.K_ESCAPE, pg.K_q):
                    self.running = False
                elif k == pg.K_SPACE:
                    self._set_playing(not self.playing)
                elif k == pg.K_LEFT:
                    self._seek(self.t - (1.0 if e.mod & pg.KMOD_SHIFT else 1 / self.fps),
                               play=False)
                elif k == pg.K_RIGHT:
                    self._seek(self.t + (1.0 if e.mod & pg.KMOD_SHIFT else 1 / self.fps),
                               play=False)
                elif k == pg.K_DOWN:
                    self._seek(self.t - 5.0, play=False)
                elif k == pg.K_UP:
                    self._seek(self.t + 5.0, play=False)
                elif k == pg.K_HOME:
                    self._seek(0.0, play=False)
                elif k == pg.K_l:
                    self.loop = not self.loop
                    self._resync()
                elif k == pg.K_LEFTBRACKET:
                    self.rate = max(0.1, round(self.rate - 0.1, 2))
                elif k == pg.K_RIGHTBRACKET:
                    self.rate = min(4.0, round(self.rate + 0.1, 2))
            elif e.type == pg.VIDEORESIZE:
                # the OS resized the window (or restored an old size): make
                # the drawing surface follow and re-derive the whole pad
                self.screen = pg.display.set_mode(e.size, flags=pg.RESIZABLE)
                self._layout(e.size[0], e.size[1])
            elif e.type == pg.MOUSEBUTTONDOWN and e.button == 1:
                if self.bar.inflate(0, 26).collidepoint(e.pos):
                    self._resume_after_drag = self.playing
                    self._set_playing(False)
                    self._drag(e.pos[0])
            elif e.type == pg.MOUSEMOTION and e.buttons[0]:
                # e.buttons is a (left, middle, right) tuple, not a bitmask
                if self.bar.inflate(0, 26).collidepoint(e.pos):
                    self._drag(e.pos[0])
            elif e.type == pg.MOUSEBUTTONUP and e.button == 1:
                if self._resume_after_drag:
                    self._resume_after_drag = False
                    self._set_playing(True)

    def _drag(self, mx: int):
        frac = (mx - self.bar.left) / max(1, self.bar.width)
        self.t = max(0.0, min(1.0, frac)) * self.dur
        self._start_audio(self.t, play=False)

    # -- main loop --------------------------------------------------------
    def run(self):
        pg = self.pg
        while self.running:
            dt = self.clock.tick(60) / 1000.0
            self._events()

            # if the window changed size without an event we saw, stay in step
            if self.screen.get_size() != (self.W, self.H):
                self.screen = pg.display.set_mode(self.screen.get_size(),
                                                  flags=pg.RESIZABLE)
                self._layout(*self.screen.get_size())

            if self.playing:
                self._advance(dt)

                if self.t >= self.dur:
                    if self.loop:
                        self.t %= self.dur
                        self._start_audio(self.t)
                    else:
                        self.t = max(0.0, self.dur - 1.0 / self.fps)
                        self._set_playing(False)
                        if self.audio_on:
                            pg.mixer.music.stop()
                elif self.t < 0:
                    self.t = 0.0

            self.draw()
            pg.display.flip()
        self.close()

    def close(self):
        try:
            if self.audio_on:
                self.pg.mixer.music.stop()
        except Exception:                                          # noqa: BLE001
            pass
        self.pg.quit()

    # -- drawing ----------------------------------------------------------
    def _current_frame(self):
        fi = int(self.t * self.fps)
        fi = max(0, min(self.n - 1, fi))
        return fi, self.frames[fi]

    def draw(self):
        pg = self.pg
        self.screen.fill((10, 11, 15))

        fi, frame = self._current_frame()

        pitch, r = self.PITCH, self.LED_R
        gap, sx, sy = self.PANEL_GAP, self.sx, self.sy

        glow = self.glow
        if glow is not None:
            glow.fill((0, 0, 0))
            gw, gh = glow.get_size()
            gs = gw / float(self.stage)

        for p in range(PANELS):
            prow, pcol = divmod(p, PANEL_GRID)
            px = sx + pcol * (self.panel_px + gap)
            py = sy + prow * (self.panel_px + gap)

            # LED centres are half a pitch in from the board edges so the 7x7
            # grid sits exactly centred; boards leave a visible gutter.
            lead = (self.panel_px - 6 * pitch) // 2
            cx0 = px + lead
            cy0 = py + lead
            board = pg.Rect(px, py, self.panel_px, self.panel_px)
            pg.draw.rect(self.screen, (19, 21, 27),
                         board, border_radius=max(10, r))
            pg.draw.rect(self.screen, (44, 48, 60),
                         board, 2, border_radius=max(10, r))

            for gr in range(7):
                for gc in range(7):
                    if (gr + gc) % 2 == 1:      # lattice holes
                        continue
                    col = frame[p, gr, gc]
                    cx = cx0 + gc * pitch
                    cy = cy0 + gr * pitch
                    pg.draw.circle(self.screen, (30, 32, 40), (cx, cy), r)
                    if int(col.max()) <= 6:
                        continue

                    c = (int(col[0]), int(col[1]), int(col[2]))
                    pg.draw.circle(self.screen, _shade(c, 0.30), (cx, cy), r + 3)
                    pg.draw.circle(self.screen, c, (cx, cy), r)
                    pg.draw.circle(self.screen, _shade(c, 1.18), (cx, cy),
                                   max(2, (2 * r) // 3))

                    if glow is not None:
                        gx = int((cx - sx) * gs)
                        gy = int((cy - sy) * gs)
                        pg.draw.circle(glow, _shade(c, 0.5), (gx, gy),
                                       max(2, int(r * 1.8 * gs)))

        if glow is not None:
            big = pg.transform.smoothscale(glow, (self.stage, self.stage))
            self.screen.blit(big, (sx, sy), special_flags=pg.BLEND_RGB_ADD)

        self._hud(fi)

    def _hud(self, fi: int):
        pg = self.pg
        x = self.sx                                   # same edges as the stage
        w = max(200, self.W - 2 * self.sx)
        y = self.H - self._hud_h_px                   # pin HUD to the bottom

        bits = ["PLAY" if self.playing else "PAUSE",
                f"{self.t:6.2f} / {self.dur:6.2f} s",
                f"frame {fi + 1}/{self.n}"]
        if self.loop:
            bits.append("LOOP")
        if abs(self.rate - 1) > 0.01:
            bits.append(f"x{self.rate:.1f}")
        self.screen.blit(self.font.render("   ".join(bits), True,
                                          (226, 233, 246)), (x, y))

        y += self.font.get_height() + 10
        self.bar = pg.Rect(x, y, w, 8)
        pg.draw.rect(self.screen, (32, 35, 44), self.bar, border_radius=4)
        frac = 0.0 if self.dur <= 0 else max(0.0, min(1.0, self.t / self.dur))
        if frac > 0:
            pg.draw.rect(self.screen, (86, 196, 255),
                         pg.Rect(x, y, max(2, int(w * frac)), 8))
        knob = pg.Rect(0, 0, 4, 18)
        knob.center = (x + int(w * frac), y + 4)
        pg.draw.rect(self.screen, (200, 240, 255), knob, border_radius=2)


# ======================================================= live bus playback ==
def bus_to_panels(frames: np.ndarray) -> np.ndarray:
    """(N, 9, 16, 3) rim colours from smx_bus -> (N, 9, 7, 7, 3) lattice."""
    out = np.zeros((len(frames), PANELS, 7, 7, 3), np.uint8)
    out[:, :, RIM_R, RIM_C] = frames
    return out


class LiveSource:
    """Background thread: ESP32 logger on a serial port -> latest light frame."""

    def __init__(self, port: str, record: str | None = None):
        import threading
        import smx_bus

        self._bus = smx_bus
        self.port = port
        self.record = open(record, "w") if record else None
        self.frame = np.zeros((1, PANELS, 7, 7, 3), np.uint8)[0]
        self.count = 0
        self.error = None
        threading.Thread(target=self._run, daemon=True).start()

    def _lines(self):
        for ln in self._bus.serial_lines(self.port):
            if self.record:
                self.record.write(ln + "\n")
            yield ln

    def _run(self):
        dec = self._bus.LightsDecoder()
        try:
            for _, frame in self._bus.parse_log_lines(self._lines()):
                for cmd, payload in self._bus.split_packets(frame):
                    full = dec.feed(cmd, payload)
                    if full is not None:
                        self.frame = bus_to_panels(full[None])[0]
                        self.count += 1
        except Exception as exc:                                   # noqa: BLE001
            self.error = str(exc)


class LivePlayer(Player):
    """Shows whatever the stage is displaying right now."""

    def __init__(self, source: LiveSource, **kw):
        self.source = source
        self._fps_n, self._fps_t, self._fps = 0, 0.0, 0.0
        blank = np.zeros((1, PANELS, 7, 7, 3), np.uint8)
        super().__init__(blank, FPS, audio_path=None, **kw)
        self.pg.display.set_caption(f"SMX stage - LIVE from {source.port}")

    def _current_frame(self):
        return self.source.count, self.source.frame

    def _hud(self, fi: int):
        import time
        now = time.monotonic()
        if now - self._fps_t >= 1.0:
            self._fps = (fi - self._fps_n) / max(1e-6, now - self._fps_t)
            self._fps_n, self._fps_t = fi, now
        msg = (f"LIVE  {self.source.port}   {self._fps:4.1f} fps   frames {fi}"
               + (f"   REC {self.source.record.name}" if self.source.record else ""))
        if self.source.error:
            msg = f"serial error: {self.source.error}"
        self.screen.blit(self.font.render(msg, True, (226, 233, 246)),
                         (self.sx, self.H - self._hud_h_px))
        self.bar = self.pg.Rect(0, 0, 0, 0)        # no seek bar in live mode


# ================================================================== main =====
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Play an SMX stage LED strip at 30 fps on a 3x3 grid of "
                    "25-LED panels.")
    ap.add_argument("png", nargs="?", help="lights PNG (one pixel row = one frame)")
    ap.add_argument("--audio", default=None, help="audio file to play along")
    ap.add_argument("--no-audio", action="store_true")
    ap.add_argument("--audio-offset", type=float, default=0.0,
                    help="seconds to shift audio relative to video")
    ap.add_argument("--fps", type=int, default=FPS)
    ap.add_argument("--rate", type=float, default=1.0, help="playback speed")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--start", type=float, default=0.0, help="start time, seconds")
    ap.add_argument("--scale", type=float, default=1.0, help="window scale factor")
    ap.add_argument("--no-bloom", action="store_true")
    ap.add_argument("--no-trim", action="store_true",
                    help="keep trailing all-black rows as extra frames")
    ap.add_argument("--serial", metavar="PORT",
                    help="show the stage live from the ESP32 bus logger "
                         "(e.g. /dev/cu.usbmodem14301)")
    ap.add_argument("--record", metavar="FILE",
                    help="with --serial: also save the raw log for later playback")
    ap.add_argument("--bus-log", metavar="FILE",
                    help="play back a recorded ESP32 bus log instead of a PNG")
    a = ap.parse_args(argv)

    if a.serial:
        LivePlayer(LiveSource(a.serial, a.record), scale=a.scale,
                   bloom=not a.no_bloom).run()
        return

    if a.bus_log:
        import smx_bus
        times, raw = smx_bus.load_log(a.bus_log)
        if len(raw) == 0:
            ap.error(f"no light frames found in {a.bus_log}")
        frames = bus_to_panels(smx_bus.resample(times, raw, a.fps))
        print(f"{os.path.basename(a.bus_log)}: {len(raw)} bus frames -> "
              f"{len(frames)} frames = {len(frames) / a.fps:.2f} s at {a.fps} fps")
        audio = None if a.no_audio or not a.audio else os.path.abspath(a.audio)
        pl = Player(frames, a.fps, audio, loop=a.loop, rate=a.rate,
                    audio_offset=a.audio_offset, scale=a.scale,
                    bloom=not a.no_bloom)
        if a.start:
            pl._seek(a.start)
        pl.run()
        return

    if a.png:
        png = os.path.abspath(a.png)
        if not os.path.exists(png):
            ap.error(f"not found: {png}")
        audio = _audio_next_to(png)
    else:
        png, audio = find_defaults()
        if not png:
            ap.error("no lights PNG found - pass its path")

    if a.audio:
        audio = os.path.abspath(a.audio)
    if a.no_audio:
        audio = None

    frames, _ = decode(png, trim=not a.no_trim)
    print(f"{os.path.basename(png)}: {len(frames)} frames = "
          f"{len(frames) / a.fps:.2f} s at {a.fps} fps, "
          f"{PANELS} panels x {LEDS_PER_PANEL} LEDs "
          f"({PANEL_GRID}x{PANEL_GRID} stage)")

    if audio:
        print(f"audio: {os.path.basename(audio)}")

    pl = Player(frames, a.fps, audio, loop=a.loop, rate=a.rate,
                audio_offset=a.audio_offset, scale=a.scale,
                bloom=not a.no_bloom)
    if a.start:
        pl.t = max(0.0, min(a.start, pl.dur))
        pl._start_audio(pl.t)
    pl.run()


if __name__ == "__main__":
    main()
