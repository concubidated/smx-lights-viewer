#!/usr/bin/env node
'use strict';
// SMX stage lights bridge: ESP32 bus logger (serial) -> WebSocket -> browser
// overlay (OBS / Streamlabs "Browser Source").
//
//   node bridge.js                      auto-detect the ESP32 and serve the overlay
//   node bridge.js --serial COM3        explicit port (/dev/ttyACM0, /dev/cu.usbmodem*)
//   node bridge.js --log cap.txt --loop replay a recorded text log
//   node bridge.js --list               list serial ports
//
// The overlay is at http://localhost:8080/  (add ?transparent=1 for OBS).
// WebSocket messages: binary = one light frame, 432 bytes
// ([panel 0..8][rim LED 0..15][r,g,b]); text = JSON status about once a second.

const fs = require('fs');
const http = require('http');
const path = require('path');
const { WebSocketServer } = require('ws');
const { LightsDecoder, formatTextFrame } = require('./lib/smxbus');
const { SerialSource, LogSource } = require('./lib/sources');
const { listPorts, describe } = require('./lib/ports');

const VERSION = require('./package.json').version;
const PUBLIC_DIR = path.join(__dirname, 'public'); // also valid inside a pkg snapshot

const HELP = `smx-overlay ${VERSION} - StepManiaX lights -> OBS browser source

Usage: smx-overlay [options]

  --serial PORT    serial port of the ESP32 logger (default: auto-detect)
  --baud N         serial baud rate (default 115200; ignored by USB CDC)
  --log FILE       replay a recorded text log instead of a serial port
  --loop           with --log: repeat forever
  --rate X         with --log: playback speed (default 1)
  --record FILE    also save the bus in the text log format (works with
                   smx_lights_player.py --bus-log and --log here)
  --port N         HTTP/WebSocket port (default 8080)
  --host ADDR      listen address (default 127.0.0.1; 0.0.0.0 for LAN access)
  --list           list serial ports and exit
  -h, --help       this help
`;

function parseArgs(argv) {
  const a = { port: 8080, host: '127.0.0.1', baud: 115200, rate: 1 };
  const need = (i, name) => {
    if (i >= argv.length) { console.error(`${name} needs a value`); process.exit(2); }
    return argv[i];
  };
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i];
    switch (k) {
      case '--serial': a.serial = need(++i, k); break;
      case '--baud': a.baud = Number(need(++i, k)); break;
      case '--log': a.log = need(++i, k); break;
      case '--loop': a.loop = true; break;
      case '--rate': a.rate = Number(need(++i, k)); break;
      case '--record': a.record = need(++i, k); break;
      case '--port': a.port = Number(need(++i, k)); break;
      case '--host': a.host = need(++i, k); break;
      case '--list': a.list = true; break;
      case '-h': case '--help': a.help = true; break;
      default: console.error(`unknown option ${k}\n\n${HELP}`); process.exit(2);
    }
  }
  return a;
}

// ------------------------------------------------------------- http ----
const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.png': 'image/png', '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon', '.json': 'application/json',
};

function serveStatic(req, res, status) {
  const url = new URL(req.url, 'http://x');
  if (url.pathname === '/status') {
    res.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
    return res.end(JSON.stringify(status()));
  }
  let rel = decodeURIComponent(url.pathname);
  if (rel.endsWith('/')) rel += 'index.html';
  const file = path.join(PUBLIC_DIR, path.normalize(rel).replace(/^([/\\])+/, ''));
  if (!file.startsWith(PUBLIC_DIR)) { res.writeHead(403); return res.end(); }
  fs.readFile(file, (err, data) => {
    if (err) { res.writeHead(404, { 'Content-Type': 'text/plain' }); return res.end('not found'); }
    res.writeHead(200, {
      'Content-Type': MIME[path.extname(file)] || 'application/octet-stream',
      'Cache-Control': 'no-cache',
    });
    res.end(data);
  });
}

// ------------------------------------------------------------- main ----
async function main() {
  const a = parseArgs(process.argv.slice(2));
  if (a.help) { process.stdout.write(HELP); return; }
  if (a.list) {
    const ports = await listPorts();
    if (!ports.length) console.log('no serial ports found');
    for (const p of ports) console.log(describe(p));
    return;
  }

  const state = {
    source: null, port: null, mode: null, error: null,
    frames: 0, fps: 0, busStats: null, clients: 0,
  };
  const status = () => ({ type: 'status', version: VERSION, ...state });

  const server = http.createServer((req, res) => serveStatic(req, res, status));
  const wss = new WebSocketServer({ server, path: '/ws', perMessageDeflate: false });
  wss.on('connection', (ws) => {
    state.clients = wss.clients.size;
    ws.send(JSON.stringify(status()));
    if (last) ws.send(last);
    ws.on('close', () => { state.clients = wss.clients.size; });
  });

  let last = null;
  const broadcast = (msg, isBinary) => {
    for (const c of wss.clients) {
      // Skip clients that are falling behind instead of queueing frames.
      if (c.readyState === 1 && c.bufferedAmount < 64 * 1024) c.send(msg, { binary: isBinary });
    }
  };

  const rec = a.record ? fs.createWriteStream(a.record) : null;
  let recSeq = 0;
  const decoder = new LightsDecoder();

  const cb = {
    onFrame(t, frame, dt) {
      if (rec) rec.write(formatTextFrame(++recSeq, (dt || 0) * 1000, frame));
      for (const full of decoder.frame(frame)) {
        state.frames++;
        last = full;
        broadcast(full, true);
      }
    },
    onStats(s) { state.busStats = s; },
    onStatus(s) {
      Object.assign(state, s);
      broadcast(JSON.stringify(status()), false);
    },
  };

  const src = a.log
    ? new LogSource({ file: a.log, loop: a.loop, rate: a.rate }, cb)
    : new SerialSource({ port: a.serial, baud: a.baud }, cb);

  let fpsN = 0;
  setInterval(() => {
    state.fps = state.frames - fpsN;
    fpsN = state.frames;
    broadcast(JSON.stringify(status()), false);
  }, 1000).unref();

  server.on('error', (e) => {
    console.error(e.code === 'EADDRINUSE'
      ? `port ${a.port} is in use - is another bridge running? (try --port 8081)`
      : e.message);
    process.exit(1);
  });
  server.listen(a.port, a.host, () => {
    const host = a.host === '0.0.0.0' ? 'localhost' : a.host;
    console.log(`smx-overlay ${VERSION}`);
    console.log(`overlay:  http://${host}:${a.port}/?transparent=1   (OBS: Sources > + > Browser)`);
    console.log(`preview:  http://${host}:${a.port}/?hud=1`);
    try {
      src.start();
    } catch (e) {
      console.error(e.message);
      process.exit(1);
    }
  });

  const quit = () => {
    src.close();
    if (rec) rec.end();
    for (const c of wss.clients) c.terminate();
    server.close();
    setTimeout(() => process.exit(0), 100).unref();
  };
  process.on('SIGINT', quit);
  process.on('SIGTERM', quit);
}

main().catch((e) => { console.error(e); process.exit(1); });
