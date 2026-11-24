#!/usr/bin/env node
'use strict';
// Package the bridge + overlay into standalone executables with @yao-pkg/pkg.
//
//   node scripts/build.js            current OS / arch
//   node scripts/build.js win|mac|linux|all
//
// serialport's native addon comes from @serialport/bindings-cpp/prebuilds and
// is bundled as an asset (pkg extracts it at runtime). Building each OS on its
// own machine (see .github/workflows/overlay-release.yml) is the reliable
// path; cross-building works for pure JS but is untested for the addon.

const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
// Must be a Node major that pkg-fetch publishes prebuilt base binaries for
// (otherwise pkg compiles Node from source, ~1 h). node20 is no longer built.
const NODE = process.env.PKG_NODE || 'node22';
const TARGETS = {
  win: [['win', 'x64']],
  mac: [['macos', 'x64'], ['macos', 'arm64']],
  linux: [['linux', 'x64']],
};

function hostKey() {
  return { win32: 'win', darwin: 'mac', linux: 'linux' }[process.platform];
}

function hostTargets() {
  const os = { win32: 'win', darwin: 'macos', linux: 'linux' }[process.platform];
  return [[os, process.arch === 'arm64' ? 'arm64' : 'x64']];
}

function build(list) {
  const pkgBin = require.resolve('@yao-pkg/pkg/lib-es5/bin.js');
  fs.mkdirSync(path.join(ROOT, 'dist'), { recursive: true });
  for (const [os, arch] of list) {
    const out = path.join('dist', `smx-overlay-${os}-${arch}${os === 'win' ? '.exe' : ''}`);
    console.log(`\n=== ${NODE}-${os}-${arch} -> ${out}`);
    execFileSync(process.execPath, [
      pkgBin, '.', '--targets', `${NODE}-${os}-${arch}`, '--output', out,
      '--compress', 'GZip', '--no-bytecode', '--public-packages', '*', '--public',
    ], { cwd: ROOT, stdio: 'inherit' });
  }
}

const which = process.argv[2];
if (!which) build(hostTargets());
else if (which === 'all') build([].concat(...Object.values(TARGETS)));
else if (TARGETS[which]) build(which === hostKey() && which !== 'mac' ? hostTargets() : TARGETS[which]);
else {
  console.error(`unknown target ${which} (win | mac | linux | all)`);
  process.exit(2);
}
