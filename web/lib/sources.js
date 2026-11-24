'use strict';
// Frame sources. Both emit through callbacks:
//   onFrame(tSeconds, busFrameBuffer, dtSeconds)  onStats(obj)  onStatus(obj)

const fs = require('fs');
const { AutoParser, TextParser } = require('./smxbus');
const { autoDetect } = require('./ports');

// ----------------------------------------------------------- serial ----
class SerialSource {
  constructor(opts, cb) {
    this.wanted = opts.port || null;   // null = auto-detect every attempt
    this.baud = opts.baud || 115200;
    this.cb = cb;
    this.port = null;
    this.path = null;
    this.mode = null;
    this.closed = false;
    this.retryMs = 1000;
  }

  start() { this._open(); }

  async _open() {
    if (this.closed) return;
    let path = this.wanted;
    try {
      if (!path) {
        const found = await autoDetect();
        if (!found) throw new Error('no ESP32 serial port found (pass --serial PORT, see --list)');
        path = found.path;
        if (path !== this.path) console.log(`[serial] auto-selected ${path} (${found.reason})`);
      }
      const { SerialPort } = require('serialport');
      const sp = new SerialPort({ path, baudRate: this.baud, autoOpen: false });
      await new Promise((res, rej) => sp.open((e) => (e ? rej(e) : res())));
      this.port = sp;
      this.path = path;
      this.retryMs = 1000;
      let lastT = 0;
      const parser = new AutoParser(
        (t, f) => { const dt = t - lastT; lastT = t; this.cb.onFrame(t, f, dt); },
        this.cb.onStats,
        (m) => { this.mode = m; console.log(`[serial] ${path}: ${m} format`); this._status(null); },
      );
      sp.on('data', (d) => parser.feed(d));
      sp.on('close', () => this._lost(path, 'port closed'));
      sp.on('error', (e) => this._lost(path, e.message));
      console.log(`[serial] opened ${path}`);
      this._status(null);
    } catch (e) {
      this._lost(path, e.message);
    }
  }

  _lost(path, msg) {
    if (this.port) {
      const p = this.port;
      this.port = null;
      p.removeAllListeners();
      if (p.isOpen) p.close(() => {});
    }
    this.mode = null;
    if (this.closed) return;
    if (msg !== this._lastErr) console.log(`[serial] ${path || '(auto)'}: ${msg} - retrying`);
    this._lastErr = msg;
    this._status(msg);
    setTimeout(() => this._open(), this.retryMs);
    this.retryMs = Math.min(5000, this.retryMs * 1.5);
  }

  _status(error) {
    this.cb.onStatus({ source: 'serial', port: this.path || this.wanted, mode: this.mode, error });
  }

  close() {
    this.closed = true;
    if (this.port && this.port.isOpen) this.port.close(() => {});
  }
}

// --------------------------------------------------------- log file ----
// Replays a recorded text log in real time using its own timestamps.
class LogSource {
  constructor(opts, cb) {
    this.file = opts.file;
    this.loop = !!opts.loop;
    this.rate = opts.rate || 1;
    this.cb = cb;
    this.timer = null;
  }

  start() {
    const frames = [];
    const parser = new TextParser((t, f) => frames.push([t, f]), () => {});
    parser.feed(fs.readFileSync(this.file));
    parser.feed('\n'); // flush a final line without a trailing newline
    if (!frames.length) throw new Error(`no frames found in ${this.file}`);
    this.frames = frames;
    console.log(`[log] ${this.file}: ${frames.length} bus frames, ` +
      `${(frames[frames.length - 1][0] - frames[0][0]).toFixed(1)} s${this.loop ? ' (loop)' : ''}`);
    this.cb.onStatus({ source: 'log', port: this.file, mode: 'text', error: null });
    this._run();
  }

  _run() {
    const fr = this.frames;
    const t0 = fr[0][0];
    const wall0 = performance.now();
    let i = 0;
    const tick = () => {
      const now = (performance.now() - wall0) / 1000 * this.rate;
      while (i < fr.length && fr[i][0] - t0 <= now) {
        const dt = i ? fr[i][0] - fr[i - 1][0] : 0;
        this.cb.onFrame(fr[i][0], fr[i][1], dt);
        i++;
      }
      if (i >= fr.length) {
        if (this.loop) { this.timer = setTimeout(() => this._run(), 200); return; }
        console.log('[log] replay finished');
        this.cb.onStatus({ source: 'log', port: this.file, mode: 'text', error: 'replay finished' });
        return;
      }
      this.timer = setTimeout(tick, 4);
    };
    tick();
  }

  close() { clearTimeout(this.timer); }
}

module.exports = { SerialSource, LogSource };
