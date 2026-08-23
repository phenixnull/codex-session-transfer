const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const net = require('node:net');
const path = require('node:path');

const {
  buildServerArguments,
  createInstanceToken,
  reserveLoopbackPort,
  serverMatchesInstance,
} = require('../electron/server-runtime');

function listen(server, port, host = '127.0.0.1') {
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, host, () => {
      server.removeListener('error', reject);
      resolve();
    });
  });
}

function close(server) {
  return new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
}

async function main() {
  const electronMain = fs.readFileSync(
    path.join(__dirname, '..', 'electron', 'main.js'),
    'utf8',
  );
  assert.match(electronMain, /reserveLoopbackPort\(HOST\)/);
  assert.match(electronMain, /createInstanceToken\(\)/);
  assert.match(electronMain, /serverMatchesInstance\(/);
  assert.doesNotMatch(electronMain, /CODEX_SESSION_TRANSFER_PORT/);
  assert.doesNotMatch(electronMain, /function\s+isServerReady\b/);

  const firstToken = createInstanceToken();
  const secondToken = createInstanceToken();
  assert.match(firstToken, /^[a-f0-9]{64}$/);
  assert.match(secondToken, /^[a-f0-9]{64}$/);
  assert.notEqual(firstToken, secondToken);
  assert.deepEqual(
    buildServerArguments({
      host: '127.0.0.1',
      port: 43210,
      instanceToken: firstToken,
      parentPid: 321,
    }),
    [
      '--host',
      '127.0.0.1',
      '--port',
      '43210',
      '--instance-token',
      firstToken,
      '--parent-pid',
      '321',
    ],
  );

  const reservation = await reserveLoopbackPort();
  const contender = net.createServer();
  try {
    assert.equal(reservation.host, '127.0.0.1');
    assert.ok(Number.isInteger(reservation.port));
    assert.ok(reservation.port > 0 && reservation.port <= 65535);
    await assert.rejects(
      listen(contender, reservation.port),
      (error) => error && error.code === 'EADDRINUSE',
    );
  } finally {
    contender.close();
    await reservation.release();
  }

  const instanceToken = createInstanceToken();
  const healthServer = http.createServer((request, response) => {
    if (request.url !== '/api/health') {
      response.writeHead(404).end();
      return;
    }
    const body = JSON.stringify({ ok: true, instance_token: instanceToken });
    response.writeHead(200, {
      'Content-Type': 'application/json',
      'Content-Length': Buffer.byteLength(body),
    });
    response.end(body);
  });
  await listen(healthServer, 0);
  const address = healthServer.address();
  const appUrl = `http://127.0.0.1:${address.port}`;
  try {
    assert.equal(await serverMatchesInstance(appUrl, 'wrong-token'), false);
    assert.equal(await serverMatchesInstance(appUrl, instanceToken), true);
  } finally {
    await close(healthServer);
  }

  console.log('server runtime isolation checks passed');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
