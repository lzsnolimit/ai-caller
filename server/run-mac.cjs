const { spawn, spawnSync } = require('node:child_process');
const { setTimeout: delay } = require('node:timers/promises');
const path = require('node:path');
const blueutil = '/opt/homebrew/bin/blueutil';
const target = process.argv[2];
const port = process.argv[3] || '18765';
if (process.argv.length > 4 || (target !== '-' && !/^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$/i.test(target || '')) || !/^[0-9]{1,5}$/.test(port) || +port < 1 || +port > 65535) process.exit(64);
function power(...args) {
  const r = spawnSync(blueutil, args, {encoding:'utf8',timeout:5000});
  return {status:r.status,value:r.stdout?.trim()};
}
async function main() {
  const initial = power('--power');
  if (initial.status !== 0 || initial.value !== '1') throw Error('system Bluetooth must initially be on');
  let child = null, interrupted = false, killTimer;
  const stop = () => {
    interrupted = true;
    if (child && !killTimer) {
      child.kill('SIGTERM');
      killTimer = setTimeout(() => child?.kill('SIGKILL'), 45000);
    }
  };
  process.on('SIGINT',stop);
  process.on('SIGTERM',stop);
  try {
    const off = power('--power','0');
    await delay(1000);
    const state = power('--power');
    if (off.status !== 0 || state.status !== 0 || state.value !== '0') throw Error('system Bluetooth did not release the controller');
    if (interrupted) return;
    const args = target === '-' ? ['-m','server.mac','--port',port] : ['-m','server.mac.legacy','--address',target,'--port',port];
    // The AI worker uses this same node binary, so it does not depend on PATH.
    child = spawn(process.env.PHONE_MAC_PYTHON || 'python3', args, {cwd:path.dirname(__dirname),stdio:'inherit',env:{...process.env,PHONE_NODE:process.execPath}});
    const code = await new Promise((resolve,reject) => {child.once('error',reject);child.once('exit',resolve);});
    if (code !== 0) process.exitCode = 1;
  } finally {
    clearTimeout(killTimer);
    child = null;
    let restored = false;
    for (let n=0;n<3;n++) {
      power('--power','1');
      await delay(750);
      const state = power('--power');
      console.log(JSON.stringify({stage:'restore-power',...state}));
      if (state.status===0 && state.value==='1') {restored=true;break;}
    }
    if (!restored) process.exitCode=4;
    process.removeListener('SIGINT',stop);
    process.removeListener('SIGTERM',stop);
  }
}
main().catch(() => {console.log(JSON.stringify({stage:'mac-wrapper-failed'}));process.exitCode ||= 1;});
