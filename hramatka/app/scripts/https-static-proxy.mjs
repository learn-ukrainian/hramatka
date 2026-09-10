import fs from 'node:fs';
import http from 'node:http';
import https from 'node:https';
import path from 'node:path';

const LOOPBACK = '127.0.0.1';
const SECURITY_HEADERS = Object.freeze({
  'Content-Security-Policy': [
    "default-src 'self'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "object-src 'none'",
  ].join('; '),
  'Permissions-Policy': 'camera=(), microphone=(), geolocation=(), payment=(), usb=()',
  'Referrer-Policy': 'no-referrer',
  'X-Content-Type-Options': 'nosniff',
  'X-Frame-Options': 'DENY',
});
const STATIC_SECURITY_HEADERS = Object.freeze({
  ...SECURITY_HEADERS,
  'Cache-Control': 'no-store',
});
const HOP_BY_HOP = new Set([
  'connection',
  'keep-alive',
  'proxy-authenticate',
  'proxy-authorization',
  'te',
  'trailer',
  'transfer-encoding',
  'upgrade',
]);
const SECURITY_HEADER_NAMES = new Set(
  Object.keys(SECURITY_HEADERS).map((name) => name.toLowerCase()),
);
const FORCED_RESPONSE_HEADER_NAMES = new Set([
  ...SECURITY_HEADER_NAMES,
  'cache-control',
]);
const CONTENT_TYPES = new Map([
  ['.css', 'text/css; charset=utf-8'],
  ['.gif', 'image/gif'],
  ['.html', 'text/html; charset=utf-8'],
  ['.ico', 'image/x-icon'],
  ['.jpeg', 'image/jpeg'],
  ['.jpg', 'image/jpeg'],
  ['.js', 'text/javascript; charset=utf-8'],
  ['.json', 'application/json; charset=utf-8'],
  ['.map', 'application/json; charset=utf-8'],
  ['.png', 'image/png'],
  ['.svg', 'image/svg+xml'],
  ['.txt', 'text/plain; charset=utf-8'],
  ['.webp', 'image/webp'],
  ['.woff', 'font/woff'],
  ['.woff2', 'font/woff2'],
]);

function requiredEnvironment(primary, legacy) {
  const value = process.env[primary] || (legacy ? process.env[legacy] : undefined);
  if (!value) {
    throw new Error(`missing ${primary}`);
  }
  return value;
}

function parsePort(name, fallback) {
  const raw = process.env[name] || fallback;
  const value = Number.parseInt(raw, 10);
  if (!Number.isInteger(value) || value < 1 || value > 65535 || String(value) !== raw) {
    throw new Error(`${name} must be a valid TCP port`);
  }
  return value;
}

function parseListenFd() {
  const raw = process.env.HRAMATKA_PROXY_LISTEN_FD;
  if (raw === undefined) {
    return null;
  }
  const value = Number.parseInt(raw, 10);
  if (!Number.isInteger(value) || value < 3 || String(value) !== raw) {
    throw new Error('HRAMATKA_PROXY_LISTEN_FD must be an inherited file descriptor');
  }
  return value;
}

const frontendRoot = fs.realpathSync(
  requiredEnvironment('HRAMATKA_PROXY_FRONTEND_DIR', 'HRAMATKA_E2E_FRONTEND_DIR'),
);
if (!fs.statSync(frontendRoot).isDirectory()) {
  throw new Error('frontend root must be a directory');
}
const certificatePath = requiredEnvironment('HRAMATKA_PROXY_TLS_CERT', 'HRAMATKA_E2E_TLS_CERT');
const keyPath = requiredEnvironment('HRAMATKA_PROXY_TLS_KEY', 'HRAMATKA_E2E_TLS_KEY');
const httpsPort = parsePort(
  'HRAMATKA_PROXY_PORT',
  process.env.HRAMATKA_E2E_PORT || process.env.HRAMATKA_E2E_HTTPS_PORT || '5174',
);
const apiPort = parsePort('HRAMATKA_PROXY_API_PORT', '8788');
const listenFd = parseListenFd();
if (httpsPort === apiPort) {
  throw new Error('proxy and API ports must be different');
}
const indexPath = fs.realpathSync(path.join(frontendRoot, 'index.html'));
if (!contained(indexPath) || !fs.statSync(indexPath).isFile()) {
  throw new Error('frontend root must contain an index.html file');
}

function contained(candidate) {
  return candidate === frontendRoot || candidate.startsWith(`${frontendRoot}${path.sep}`);
}

function responseHeaders(rawHeaders) {
  const coalesced = new Map();
  for (let index = 0; index < rawHeaders.length; index += 2) {
    const name = rawHeaders[index];
    const value = rawHeaders[index + 1];
    const lower = name.toLowerCase();
    if (
      HOP_BY_HOP.has(lower)
      || lower.startsWith('access-control-')
      || FORCED_RESPONSE_HEADER_NAMES.has(lower)
      || lower === 'server'
      || lower === 'strict-transport-security'
    ) {
      continue;
    }
    const existing = coalesced.get(lower);
    if (existing) {
      existing.values.push(value);
    } else {
      coalesced.set(lower, { name, values: [value] });
    }
  }
  const headers = Object.create(null);
  for (const { name, values } of coalesced.values()) {
    headers[name] = values.length === 1 ? values[0] : values;
  }
  return { ...headers, ...SECURITY_HEADERS, 'Cache-Control': 'no-store' };
}

function requestHeaders(incoming) {
  const headers = Object.create(null);
  for (const [name, value] of Object.entries(incoming)) {
    const lower = name.toLowerCase();
    if (
      value === undefined
      || HOP_BY_HOP.has(lower)
      || lower === 'host'
      || lower.startsWith('x-forwarded-')
    ) {
      continue;
    }
    headers[name] = value;
  }
  headers.host = `${LOOPBACK}:${httpsPort}`;
  headers['x-forwarded-for'] = LOOPBACK;
  headers['x-forwarded-host'] = `${LOOPBACK}:${httpsPort}`;
  headers['x-forwarded-proto'] = 'https';
  return headers;
}

function proxyApi(request, response, url) {
  const upstream = http.request(
    {
      hostname: LOOPBACK,
      port: apiPort,
      method: request.method,
      path: `${url.pathname}${url.search}`,
      headers: requestHeaders(request.headers),
    },
    (upstreamResponse) => {
      let headersWritten = false;
      upstreamResponse.on('error', () => {
        if (headersWritten) {
          response.destroy();
        } else if (!response.headersSent) {
          response.writeHead(502, { ...SECURITY_HEADERS, 'Cache-Control': 'no-store' });
          response.end('Bad Gateway');
        }
      });
      if (!response.headersSent) {
        headersWritten = true;
        response.writeHead(
          upstreamResponse.statusCode || 502,
          responseHeaders(upstreamResponse.rawHeaders),
        );
        upstreamResponse.pipe(response);
      }
    },
  );
  upstream.on('error', () => {
    if (response.headersSent) {
      response.destroy();
    } else {
      response.writeHead(502, { ...SECURITY_HEADERS, 'Cache-Control': 'no-store' });
      response.end('Bad Gateway');
    }
  });
  request.on('aborted', () => upstream.destroy());
  response.on('close', () => {
    if (!response.writableEnded) {
      upstream.destroy();
    }
  });
  request.pipe(upstream);
}

async function safeStaticPath(urlPath) {
  const rawRelative = urlPath.slice('/teacher/'.length);
  let relative;
  try {
    relative = decodeURIComponent(rawRelative);
  } catch {
    return null;
  }
  if (
    relative.includes('\0')
    || relative.includes('\\')
    || relative.startsWith('/')
    || relative.split('/').includes('..')
  ) {
    return null;
  }
  const lexical = path.resolve(frontendRoot, relative || 'index.html');
  if (!contained(lexical)) {
    return null;
  }
  try {
    const resolved = await fs.promises.realpath(lexical);
    if (!contained(resolved) || !(await fs.promises.stat(resolved)).isFile()) {
      return null;
    }
    return resolved;
  } catch {
    // A missing or inaccessible asset may still be an extensionless SPA route.
  }
  if (path.extname(relative)) {
    return null;
  }
  try {
    return contained(indexPath) && (await fs.promises.stat(indexPath)).isFile()
      ? indexPath
      : null;
  } catch {
    return null;
  }
}

async function serveStatic(request, response, url) {
  try {
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      response.writeHead(405, { ...STATIC_SECURITY_HEADERS, Allow: 'GET, HEAD' });
      response.end();
      return;
    }
    const filePath = await safeStaticPath(url.pathname);
    if (!filePath) {
      response.writeHead(404, STATIC_SECURITY_HEADERS);
      response.end('Not Found');
      return;
    }
    response.writeHead(200, {
      ...STATIC_SECURITY_HEADERS,
      'Content-Type': CONTENT_TYPES.get(path.extname(filePath).toLowerCase())
        || 'application/octet-stream',
    });
    if (request.method === 'HEAD') {
      response.end();
      return;
    }
    const stream = fs.createReadStream(filePath);
    stream.on('error', () => response.destroy());
    stream.pipe(response);
  } catch {
    if (response.headersSent) {
      response.destroy();
    } else {
      response.writeHead(500, STATIC_SECURITY_HEADERS);
      response.end('Internal Server Error');
    }
  }
}

const server = https.createServer(
  {
    cert: fs.readFileSync(certificatePath),
    key: fs.readFileSync(keyPath),
  },
  (request, response) => {
    let url;
    try {
      url = new URL(request.url || '/', `https://${LOOPBACK}`);
    } catch {
      response.writeHead(400, STATIC_SECURITY_HEADERS);
      response.end('Bad Request');
      return;
    }
    if (url.pathname.startsWith('/api/')) {
      proxyApi(request, response, url);
      return;
    }
    if (url.pathname === '/teacher') {
      response.writeHead(308, { ...STATIC_SECURITY_HEADERS, Location: '/teacher/' });
      response.end();
      return;
    }
    if (url.pathname.startsWith('/teacher/')) {
      void serveStatic(request, response, url);
      return;
    }
    response.writeHead(404, STATIC_SECURITY_HEADERS);
    response.end('Not Found');
  },
);

if (listenFd === null) {
  server.listen(httpsPort, LOOPBACK);
} else {
  server.listen({ fd: listenFd, exclusive: true });
}
