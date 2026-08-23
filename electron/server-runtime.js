const crypto = require("node:crypto");
const net = require("node:net");

const LOOPBACK_HOST = "127.0.0.1";

function createInstanceToken() {
  return crypto.randomBytes(32).toString("hex");
}

function reserveLoopbackPort(host = LOOPBACK_HOST) {
  const server = net.createServer();
  return new Promise((resolve, reject) => {
    const fail = (error) => {
      server.removeListener("listening", ready);
      reject(error);
    };
    const ready = () => {
      server.removeListener("error", fail);
      const address = server.address();
      let released = false;
      resolve({
        host,
        port: address.port,
        release() {
          if (released) return Promise.resolve();
          released = true;
          return new Promise((releaseResolve, releaseReject) => {
            server.close((error) => {
              if (error) releaseReject(error);
              else releaseResolve();
            });
          });
        },
      });
    };
    server.once("error", fail);
    server.once("listening", ready);
    server.listen(0, host);
  });
}

function buildServerArguments({
  host = LOOPBACK_HOST,
  port,
  instanceToken,
  parentPid,
}) {
  return [
    "--host",
    host,
    "--port",
    String(port),
    "--instance-token",
    instanceToken,
    "--parent-pid",
    String(parentPid),
  ];
}

async function serverMatchesInstance(appUrl, instanceToken, options = {}) {
  const fetchImpl = options.fetchImpl || globalThis.fetch;
  const timeoutMs = options.timeoutMs || 750;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetchImpl(`${appUrl}/api/health`, {
      cache: "no-store",
      signal: controller.signal,
    });
    if (!response.ok) return false;
    const payload = await response.json();
    return payload.ok === true && payload.instance_token === instanceToken;
  } catch {
    return false;
  } finally {
    clearTimeout(timeout);
  }
}

module.exports = {
  LOOPBACK_HOST,
  buildServerArguments,
  createInstanceToken,
  reserveLoopbackPort,
  serverMatchesInstance,
};
