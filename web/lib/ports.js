'use strict';
// Serial port discovery: find the ESP32 without the user naming the port.

const ESPRESSIF_VIDS = ['303a'];                 // ESP32-S3 native USB (CDC)
const BRIDGE_VIDS = ['1a86', '10c4', '0403'];    // CH340/CH343, CP210x, FTDI

function vid(p) {
  return (p.vendorId || '').toLowerCase();
}

function isRealPort(p) {
  // Linux lists dozens of legacy /dev/ttyS* without USB info; skip them.
  return Boolean(p.vendorId) || !/\/dev\/ttyS\d+$/.test(p.path);
}

// macOS lists /dev/tty.* (dial-in, may block waiting for carrier); the
// /dev/cu.* callout device is the one to open.
function calloutPath(p) {
  return process.platform === 'darwin' ? p.replace(/^\/dev\/tty\./, '/dev/cu.') : p;
}

async function listPorts() {
  const { SerialPort } = require('serialport');
  return (await SerialPort.list())
    .filter(isRealPort)
    .map((p) => ({ ...p, path: calloutPath(p.path) }));
}

// Returns { path, reason } or null.
async function autoDetect() {
  const ports = await listPorts();
  const esp = ports.find((p) => ESPRESSIF_VIDS.includes(vid(p)));
  if (esp) return { path: esp.path, reason: `Espressif USB (VID ${vid(esp)})` };
  const bridge = ports.find((p) => BRIDGE_VIDS.includes(vid(p)));
  if (bridge) return { path: bridge.path, reason: `USB-serial bridge (VID ${vid(bridge)})` };
  // Fall back to the only USB serial device (ignores COM1, Bluetooth, ...).
  const usb = ports.filter((p) => p.vendorId);
  if (usb.length === 1) return { path: usb[0].path, reason: 'only USB serial port present' };
  return null;
}

function describe(p) {
  const bits = [p.path];
  if (p.vendorId) bits.push(`VID:PID ${p.vendorId}:${p.productId || '????'}`);
  if (p.manufacturer) bits.push(p.manufacturer);
  if (ESPRESSIF_VIDS.includes(vid(p))) bits.push('<- ESP32');
  return bits.join('  ');
}

module.exports = { listPorts, autoDetect, describe };
