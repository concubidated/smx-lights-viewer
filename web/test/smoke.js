#!/usr/bin/env node
'use strict';
// Smoke test: start the bridge (or a built executable) replaying the fixture
// log, then check the HTTP page, the status endpoint and that light frames
// arrive over the WebSocket.
//
//   node test/smoke.js                      test `node bridge.js`
//   node test/smoke.js dist/smx-overlay-... test a packaged executable

const { spawn } = require('child_process');
const http = require('http');
const path = require('path');
const WebSocket = require('ws');

const ROOT = path.join(__dirname, '..');
const PORT = 18000 + Math.floor(Math.random() * 1000);
const fixture = path.join(__dirname, 'fixture.txt');
const exe = process.argv[2];
const args = ['--log', fixture, '--loop', '--port', String(PORT)];
const child = exe
  ? spawn(path.resolve(exe), args, { cwd: ROOT })
  : spawn(process.execPath, [path.join(ROOT, 'bridge.js'), ...args], { cwd: ROOT });

// Executables run under emulation (Rosetta 2) can take a while on first
// launch while the binary is translated; allow overriding the wait.
const START_MS = Number(process.env.SMOKE_START_MS || 30000);

let out = '';
let exited = null;
child.stdout.on('data', (d) => { out += d; });
child.stderr.on('data', (d) => { out += d; });
child.on('error', (e) => { exited = `spawn error: ${e.message}`; });
child.on('exit', (code, sig) => { exited = `exited with code ${code} signal ${sig}`; });

function fail(msg) {
  console.error(`FAIL: ${msg}${exited ? ` (bridge ${exited})` : ' (bridge still running)'}` +
    `\n--- bridge output ---\n${out}`);
  child.kill();
  process.exit(1);
}

function get(p) {
  return new Promise((res, rej) => {
    http.get({ host: '127.0.0.1', port: PORT, path: p }, (r) => {
      let body = '';
      r.on('data', (d) => { body += d; });
      r.on('end', () => res({ status: r.statusCode, body }));
    }).on('error', rej);
  });
}

async function waitForServer() {
  const t0 = Date.now();
  while (Date.now() - t0 < START_MS) {
    if (exited) break;
    try { return await get('/status'); } catch { await new Promise((r) => setTimeout(r, 200)); }
  }
  throw new Error(`bridge did not start listening within ${START_MS} ms`);
}

(async () => {
  const timer = setTimeout(() => fail('timed out'), START_MS + 10000);
  try {
    await waitForServer();
    const page = await get('/');
    if (page.status !== 200 || !page.body.includes('<canvas')) fail('overlay page not served');
    const js = await get('/overlay.js');
    if (js.status !== 200) fail('overlay.js not served');

    let frames = 0;
    let lit = 0;
    let gotStatus = false;
    await new Promise((resolve, reject) => {
      const ws = new WebSocket(`ws://127.0.0.1:${PORT}/ws`);
      ws.on('message', (data, isBinary) => {
        if (isBinary) {
          if (data.length !== 432 && data.length !== 675) reject(new Error(`frame length ${data.length}`));
          frames++;
          if (data.some((v) => v > 0)) lit++;
        } else if (JSON.parse(data.toString()).type === 'status') {
          gotStatus = true;
        }
      });
      ws.on('error', reject);
      setTimeout(() => { ws.close(); resolve(); }, 2000);
    });
    clearTimeout(timer);
    if (!gotStatus) fail('no status message');
    if (frames < 30) fail(`only ${frames} frames in 2 s (expected ~60)`);
    if (!lit) fail('all frames were black');
    console.log(`OK: ${frames} frames in 2 s (${lit} lit), status + page served`);
    child.kill();
    process.exit(0);
  } catch (e) {
    fail(e.message);
  }
})();
