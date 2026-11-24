'use strict';
// Decoder for the StepManiaX master -> panel bus as logged by the ESP32
// firmware.  JavaScript port of ../../smx_bus.py; see that file for the
// protocol notes.  Two input formats are understood:
//
//   text   (SMX_OUTPUT_BINARY=0)  "#123 t=+16.7ms len=217" + "  0000: aa bb .." lines
//   binary (SMX_OUTPUT_BINARY=1)  A5 5A | type | u16 len | u32 dt_us | data | xor
//
// Both parsers are push-based: feed() them chunks as they arrive and they call
// onFrame(tSeconds, Buffer) for every complete bus frame.

const PANELS = 9;
const RIM_LEDS = 16;
const HALF_BYTES = PANELS * 8 * 3; // 216
const FRAME_BYTES = PANELS * RIM_LEDS * 3; // 432
const PACKET_LEN = { 0x32: 1 + HALF_BYTES, 0x33: 1 + HALF_BYTES, 0x77: 131 }; // '2' '3' 'w'

// ------------------------------------------------------------- text ----
const HDR = /#(\d+) t=\+([\d.]+)ms len=(\d+)/;
const HEX = /^\s+[0-9a-f]{4}:((?: [0-9a-f]{2})+)\s*$/;
const STATS = /^\[stats\]\s+(.*)$/;

class TextParser {
  constructor(onFrame, onStats) {
    this.onFrame = onFrame;
    this.onStats = onStats || (() => {});
    this.t = 0;
    this.cur = null; // { t, chunks: [], have, want }
    this.rest = '';
  }

  feed(chunk) {
    const s = this.rest + (typeof chunk === 'string' ? chunk : chunk.toString('latin1'));
    const lines = s.split('\n');
    this.rest = lines.pop();
    for (const ln of lines) this.line(ln);
  }

  line(line) {
    line = line.replace(/\r$/, '');
    let m = HDR.exec(line);
    if (m) {
      const dt = parseFloat(m[2]) / 1000;
      if (dt < 5) this.t += dt; // first frame after boot has a huge dt
      this.cur = { t: this.t, chunks: [], have: 0, want: parseInt(m[3], 10) };
      return;
    }
    m = HEX.exec(line);
    if (m && this.cur) {
      const b = Buffer.from(m[1].replace(/ /g, ''), 'hex');
      this.cur.chunks.push(b);
      this.cur.have += b.length;
      if (this.cur.have >= this.cur.want) {
        if (this.cur.have === this.cur.want) {
          this.onFrame(this.cur.t, Buffer.concat(this.cur.chunks));
        }
        this.cur = null;
      }
      return;
    }
    m = STATS.exec(line);
    if (m) {
      const o = {};
      for (const kv of m[1].split(/\s+/)) {
        const [k, v] = kv.split('=');
        if (v !== undefined) o[k] = /^\d+$/.test(v) ? Number(v) : v;
      }
      this.onStats(o);
    }
  }
}

// ----------------------------------------------------------- binary ----
const SYNC0 = 0xa5;
const SYNC1 = 0x5a;
const BIN_HDR = 9;
const MAX_LEN = 4096;

class BinaryParser {
  constructor(onFrame, onStats) {
    this.onFrame = onFrame;
    this.onStats = onStats || (() => {});
    this.buf = Buffer.alloc(0);
    this.t = 0;
    this.badChecksums = 0;
  }

  feed(chunk) {
    this.buf = this.buf.length ? Buffer.concat([this.buf, chunk]) : chunk;
    let i = 0;
    const b = this.buf;
    while (i + BIN_HDR <= b.length) {
      if (b[i] !== SYNC0 || b[i + 1] !== SYNC1) { i++; continue; }
      const type = b[i + 2];
      const len = b.readUInt16LE(i + 3);
      if (len > MAX_LEN || !(type === 0x46 || type === 0x66 || type === 0x53)) { i++; continue; }
      const end = i + BIN_HDR + len + 1;
      if (end > b.length) break; // wait for more bytes
      let x = 0;
      for (let k = i + 2; k < end - 1; k++) x ^= b[k];
      if (x !== b[end - 1]) { this.badChecksums++; i++; continue; }
      const dtUs = b.readUInt32LE(i + 5);
      const data = b.subarray(i + BIN_HDR, end - 1);
      if (type === 0x53) {
        if (len >= 28) {
          const mm = data.readUInt32LE(4);
          this.onStats({
            fps: data.readUInt32LE(0), len: `${mm & 0xffff}..${mm >>> 16}`,
            dropped: data.readUInt32LE(8), trunc: data.readUInt32LE(12),
            fifo_ovf: data.readUInt32LE(16), frame_err: data.readUInt32LE(20),
            parity_err: data.readUInt32LE(24),
          });
        }
      } else {
        if (dtUs < 5e6) this.t += dtUs / 1e6;
        if (type === 0x46) this.onFrame(this.t, Buffer.from(data), dtUs); // 'f' = truncated, skip
      }
      i = end;
    }
    this.buf = i ? b.subarray(i) : b;
    if (this.buf.length > 2 * MAX_LEN) this.buf = this.buf.subarray(this.buf.length - MAX_LEN);
  }
}

// Sniffs the first bytes to pick TextParser or BinaryParser, then delegates.
class AutoParser {
  constructor(onFrame, onStats, onMode) {
    this.args = [onFrame, onStats];
    this.onMode = onMode || (() => {});
    this.inner = null;
    this.sniff = Buffer.alloc(0);
  }

  get mode() { return this.inner ? (this.inner instanceof BinaryParser ? 'binary' : 'text') : null; }

  feed(chunk) {
    if (this.inner) return this.inner.feed(chunk);
    this.sniff = Buffer.concat([this.sniff, chunk]);
    const s = this.sniff;
    const bin = s.indexOf(Buffer.from([SYNC0, SYNC1]));
    const txt = HDR.test(s.toString('latin1'));
    if (!(bin >= 0 && s.length >= bin + BIN_HDR) && !txt) {
      if (s.length > 8192) this.sniff = s.subarray(s.length - 4096);
      return;
    }
    const isBin = bin >= 0 && (!txt || s.toString('latin1').search(HDR) > bin) &&
      [0x46, 0x66, 0x53].includes(s[bin + 2]);
    this.inner = isBin ? new BinaryParser(...this.args) : new TextParser(...this.args);
    this.onMode(this.mode);
    this.sniff = null;
    this.inner.feed(s);
  }
}

// ---------------------------------------------------------- packets ----
function* splitPackets(frame) {
  let i = 0;
  while (i < frame.length) {
    const n = PACKET_LEN[frame[i]];
    if (n === undefined || i + n > frame.length) return; // unknown / truncated
    yield [String.fromCharCode(frame[i]), frame.subarray(i + 1, i + n)];
    i += n;
  }
}

// Assembles '2' / '3' halves into full frames: Uint8Array(432) laid out as
// [panel 0..8][rim LED 0..15][r,g,b], colours scaled back from the SDK's x2/3.
class LightsDecoder {
  constructor() {
    this.state = new Uint8Array(FRAME_BYTES);
    this.haveTop = false;
  }

  feed(cmd, payload) {
    if (cmd !== '2' && cmd !== '3') return null;
    const off = cmd === '2' ? 0 : 8;
    for (let p = 0; p < PANELS; p++) {
      for (let k = 0; k < 24; k++) {
        const v = (payload[p * 24 + k] * 3) >> 1;
        this.state[(p * RIM_LEDS + off) * 3 + k] = v > 255 ? 255 : v;
      }
    }
    if (cmd === '2') { this.haveTop = true; return null; }
    if (!this.haveTop) return null;
    this.haveTop = false;
    return this.state.slice();
  }

  // Convenience: one bus frame -> array of completed light frames.
  frame(frameBytes) {
    const out = [];
    for (const [cmd, payload] of splitPackets(frameBytes)) {
      const full = this.feed(cmd, payload);
      if (full) out.push(full);
    }
    return out;
  }
}

// Re-encode a bus frame in the firmware's text format (for --record).
function formatTextFrame(seq, dtMs, frame) {
  let s = `#${seq} t=+${dtMs.toFixed(1)}ms len=${frame.length}\n`;
  for (let off = 0; off < frame.length; off += 16) {
    s += `  ${off.toString(16).padStart(4, '0')}:`;
    for (let i = off; i < frame.length && i < off + 16; i++) {
      s += ' ' + frame[i].toString(16).padStart(2, '0');
    }
    s += '\n';
  }
  return s;
}

module.exports = {
  PANELS, RIM_LEDS, FRAME_BYTES,
  TextParser, BinaryParser, AutoParser, LightsDecoder, splitPackets, formatTextFrame,
};
