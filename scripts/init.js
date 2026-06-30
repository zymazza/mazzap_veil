#!/usr/bin/env node
'use strict';

const { spawn } = require('child_process');
const path = require('path');

const ROOT = path.dirname(__dirname);
const env = {
  ...process.env,
  TWIN_DATA_DIR: process.env.TWIN_DATA_DIR || path.join(ROOT, 'twins', 'init', 'data'),
  VEIL_INIT: '1',
};
const host = env.HOST || '127.0.0.1';
const port = env.PORT || '4173';
// Setup UI: the shell at /init.html (public/init.{html,css} + init-shell.js).
const url = `http://${host}:${port}/init.html`;

function openBrowser(target) {
  const platform = process.platform;
  const cmd = platform === 'darwin' ? 'open'
    : platform === 'win32' ? 'cmd'
      : 'xdg-open';
  const args = platform === 'win32' ? ['/c', 'start', '', target] : [target];
  const opener = spawn(cmd, args, { stdio: 'ignore', detached: true });
  opener.on('error', () => {});
  opener.unref();
}

const server = spawn(process.execPath, [path.join(ROOT, 'server.js')], {
  cwd: ROOT,
  env,
  stdio: ['inherit', 'pipe', 'pipe'],
});

let opened = false;
function maybeOpen(chunk) {
  process.stdout.write(chunk);
  if (!opened && String(chunk).includes(`http://${host}:${port}`)) {
    opened = true;
    openBrowser(url);
    console.log(`  setup: ${url}`);
    console.log(`  data:  ${env.TWIN_DATA_DIR}\n`);
  }
}

server.stdout.on('data', maybeOpen);
server.stderr.on('data', (chunk) => process.stderr.write(chunk));
server.on('exit', (code, signal) => {
  process.exitCode = code || (signal ? 1 : 0);
});

['SIGINT', 'SIGTERM'].forEach((sig) => {
  process.on(sig, () => {
    server.kill(sig);
  });
});
