'use strict';
// SMX stage lights overlay. Receives 432-byte frames over WebSocket
// ([panel 0..8][rim LED 0..15][r,g,b]) and draws the 3x3 stage like
// smx_lights_player.py. Only redraws when a new frame (or resize) arrives.
//
// URL options:
//   ?transparent=1  no page / stage background (for OBS)
//   ?boards=0       hide the panel boards, LEDs only
//   ?bloom=0        disable the glow pass
//   ?hud=1          show port / fps text
//   ?ws=host:port   bridge address (default: the page's own host)
//   ?gap=8          panel gap in "pygame pixels" (default 8; pygame uses 24)

(function () {
  const q = new URLSearchParams(location.search);
  const flag = (k, d) => (q.has(k) ? !/^(0|false|no|off)$/i.test(q.get(k)) : d);
  const opt = {
    transparent: flag('transparent', false),
    boards: flag('boards', true),
    bloom: flag('bloom', true),
    hud: flag('hud', false),
    ws: q.get('ws') || location.host || 'localhost:8080',
    gap: Number(q.get('gap') ?? 8),
  };
  if (opt.transparent) document.body.classList.add('transparent');

  const PANELS = 9;
  const RIM = 16;
  // 4x4 rim block (row-major) -> position in the panel's 7x7 lattice.
  const RIM_R = [0, 0, 0, 0, 2, 2, 2, 2, 4, 4, 4, 4, 6, 6, 6, 6];
  const RIM_C = [0, 2, 4, 6, 0, 2, 4, 6, 0, 2, 4, 6, 0, 2, 4, 6];
  const IDLE_MS = 2000;

  const canvas = document.getElementById('stage');
  const ctx = canvas.getContext('2d');
  const glow = document.createElement('canvas');
  const gctx = glow.getContext('2d');
  const base = document.createElement('canvas'); // boards + dark LEDs, cached
  const bctx = base.getContext('2d');

  let frame = new Uint8Array(PANELS * RIM * 3);
  let lastFrameAt = 0;
  let dirty = true;
  let L = null; // layout
  let status = {};
  let wsOk = false;
  let shownFps = 0;
  let fpsCount = 0;
  let fpsT = performance.now();

  const shade = (r, g, b, f) =>
    `rgb(${Math.min(255, r * f) | 0},${Math.min(255, g * f) | 0},${Math.min(255, b * f) | 0})`;

  // ------------------------------------------------------------ layout ----
  function layout() {
    const dpr = window.devicePixelRatio || 1;
    const W = Math.max(1, Math.round(canvas.clientWidth * dpr));
    const H = Math.max(1, Math.round(canvas.clientHeight * dpr));
    canvas.width = W;
    canvas.height = H;
    const hudH = opt.hud ? Math.round(Math.max(14, Math.min(W, H) * 0.035) * 1.6) : 0;
    // Same proportions as the pygame player at scale s=1:
    // pitch 15, led r 6, panel = 6*pitch + 2*(r+9), gap 24, margin 16.
    const unit = 6 * 15 + 2 * (6 + 9);
    const total = 3 * unit + 2 * opt.gap + 2 * 16;
    const s = Math.min(W / total, (H - hudH) / total);
    const pitch = 15 * s;
    const r = Math.max(1.5, 6 * s);
    const panel = 6 * pitch + 2 * (r + 9 * s);
    const gap = opt.gap * s;
    const stage = 3 * panel + 2 * gap;
    L = {
      W, H, s, pitch, r, panel, gap, stage, hudH,
      sx: (W - stage) / 2,
      sy: (H - hudH - stage) / 2,
      lead: (panel - 6 * pitch) / 2,
    };
    // Glow layer at half resolution: cheap blur, upscaled when composited.
    glow.width = Math.max(8, Math.round(W / 2));
    glow.height = Math.max(8, Math.round(H / 2));
    buildBase();
    dirty = true;
  }

  function ledPos(p, i) {
    const pr = (p / 3) | 0;
    const pc = p % 3;
    const px = L.sx + pc * (L.panel + L.gap);
    const py = L.sy + pr * (L.panel + L.gap);
    return [px + L.lead + RIM_C[i] * L.pitch, py + L.lead + RIM_R[i] * L.pitch];
  }

  function roundRect(c, x, y, w, h, rad) {
    c.beginPath();
    if (c.roundRect) c.roundRect(x, y, w, h, rad);
    else c.rect(x, y, w, h);
  }

  // Static layer: boards, borders and unlit LED sockets.
  function buildBase() {
    base.width = L.W;
    base.height = L.H;
    bctx.clearRect(0, 0, L.W, L.H);
    if (!opt.transparent) {
      bctx.fillStyle = '#0a0b0f';
      bctx.fillRect(0, 0, L.W, L.H);
    }
    for (let p = 0; p < PANELS; p++) {
      const px = L.sx + (p % 3) * (L.panel + L.gap);
      const py = L.sy + ((p / 3) | 0) * (L.panel + L.gap);
      const rad = Math.max(10 * L.s, L.r);
      if (opt.boards) {
        roundRect(bctx, px, py, L.panel, L.panel, rad);
        bctx.fillStyle = '#13151b';
        bctx.fill();
        bctx.lineWidth = Math.max(1, 2 * L.s);
        bctx.strokeStyle = '#2c303c';
        bctx.stroke();
      }
      // The pygame player also draws the 9 core sockets; the bus never
      // carries core data, so they are always dark sockets here too.
      for (let gr = 0; gr < 7; gr++) {
        for (let gc = 0; gc < 7; gc++) {
          if ((gr + gc) % 2 === 1) continue;
          if (!opt.boards && opt.transparent) continue; // pure LEDs mode
          bctx.beginPath();
          bctx.arc(px + L.lead + gc * L.pitch, py + L.lead + gr * L.pitch, L.r, 0, Math.PI * 2);
          bctx.fillStyle = '#1e2028';
          bctx.fill();
        }
      }
    }
  }

  // -------------------------------------------------------------- draw ----
  function draw(now) {
    // Fade to the idle look when the stream stops.
    const age = now - lastFrameAt;
    const fade = age < IDLE_MS ? 1 : Math.max(0, 1 - (age - IDLE_MS) / 600);

    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = 1;
    ctx.clearRect(0, 0, L.W, L.H);
    ctx.drawImage(base, 0, 0);

    const r = L.r;
    const useGlow = opt.bloom && fade > 0;
    if (useGlow) {
      gctx.globalCompositeOperation = 'source-over';
      gctx.clearRect(0, 0, glow.width, glow.height);
    }
    const gs = glow.width / L.W;

    if (fade > 0) {
      ctx.globalAlpha = fade;
      for (let p = 0; p < PANELS; p++) {
        for (let i = 0; i < RIM; i++) {
          const o = (p * RIM + i) * 3;
          const cr = frame[o], cg = frame[o + 1], cb = frame[o + 2];
          if (Math.max(cr, cg, cb) <= 6) continue;
          const [x, y] = ledPos(p, i);
          // halo, body, hot centre (same recipe as Player.draw)
          ctx.fillStyle = shade(cr, cg, cb, 0.3);
          ctx.beginPath(); ctx.arc(x, y, r + 3 * L.s, 0, Math.PI * 2); ctx.fill();
          ctx.fillStyle = `rgb(${cr},${cg},${cb})`;
          ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
          ctx.fillStyle = shade(cr, cg, cb, 1.18);
          ctx.beginPath(); ctx.arc(x, y, Math.max(1, (2 * r) / 3), 0, Math.PI * 2); ctx.fill();
          if (useGlow) {
            gctx.fillStyle = shade(cr, cg, cb, 0.5);
            gctx.beginPath();
            gctx.arc(x * gs, y * gs, Math.max(1, r * 1.8 * gs), 0, Math.PI * 2);
            gctx.fill();
          }
        }
      }
      if (useGlow) {
        ctx.globalCompositeOperation = 'lighter';
        ctx.filter = `blur(${Math.max(2, r * 1.6)}px)`;
        ctx.drawImage(glow, 0, 0, L.W, L.H);
        ctx.filter = 'none';
        ctx.globalCompositeOperation = 'source-over';
      }
      ctx.globalAlpha = 1;
    }

    if (opt.hud) drawHud();
  }

  function drawHud() {
    const fs = Math.round(L.hudH / 1.6);
    ctx.font = `${fs}px Menlo, Consolas, "DejaVu Sans Mono", monospace`;
    ctx.fillStyle = '#e2e9f6';
    ctx.textBaseline = 'middle';
    let msg;
    if (!wsOk) msg = `bridge not connected (${opt.ws}) - retrying`;
    else if (status.error) msg = `${status.port || 'serial'}: ${status.error}`;
    else {
      msg = `LIVE  ${status.port || ''}  ${status.mode || ''}  ${shownFps.toFixed(0)} fps  ` +
        `frames ${status.frames || 0}`;
    }
    ctx.fillText(msg, Math.max(4, L.sx), L.H - L.hudH / 2);
  }

  function loop(now) {
    if (now - fpsT >= 1000) {
      shownFps = (fpsCount * 1000) / (now - fpsT);
      fpsCount = 0;
      fpsT = now;
      if (opt.hud) dirty = true;
    }
    const age = now - lastFrameAt;
    const fading = age >= IDLE_MS && age < IDLE_MS + 700;
    if (dirty || fading) {
      draw(now);
      dirty = false;
    }
    requestAnimationFrame(loop);
  }

  // ---------------------------------------------------------- network ----
  let retry = 500;
  function connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${opt.ws}/ws`);
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => { wsOk = true; retry = 500; dirty = true; };
    ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') {
        try { status = JSON.parse(ev.data); } catch (e) { /* ignore */ }
        if (opt.hud) dirty = true;
        return;
      }
      const f = new Uint8Array(ev.data);
      if (f.length !== PANELS * RIM * 3) return;
      frame = f;
      lastFrameAt = performance.now();
      fpsCount++;
      dirty = true;
    };
    ws.onclose = () => {
      if (wsOk) dirty = true;
      wsOk = false;
      setTimeout(connect, retry);
      retry = Math.min(5000, retry * 1.5);
    };
    ws.onerror = () => ws.close();
  }

  window.addEventListener('resize', layout);
  layout();
  connect();
  requestAnimationFrame(loop);
})();
