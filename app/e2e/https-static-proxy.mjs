/** Test-only HTTPS equivalent of the deployed Caddy same-origin routing. */
import fs from 'node:fs';
import http from 'node:http';
import https from 'node:https';
import path from 'node:path';

const root = path.resolve(process.env.HRAMATKA_E2E_FRONTEND_DIR);
const cert = fs.readFileSync(process.env.HRAMATKA_E2E_TLS_CERT);
const key = fs.readFileSync(process.env.HRAMATKA_E2E_TLS_KEY);
const port = Number(process.env.HRAMATKA_E2E_PORT || '5174');

const contentType = (file) => {
  if (file.endsWith('.js')) return 'text/javascript; charset=utf-8';
  if (file.endsWith('.css')) return 'text/css; charset=utf-8';
  if (file.endsWith('.svg')) return 'image/svg+xml';
  return 'text/html; charset=utf-8';
};

https.createServer({ cert, key }, (request, response) => {
  const pathname = new URL(request.url, 'https://127.0.0.1').pathname;
  if (pathname.startsWith('/api/')) {
    const upstream = http.request({
      host: '127.0.0.1', port: 8788, method: request.method,
      path: request.url, headers: request.headers,
    }, (upstreamResponse) => {
      response.writeHead(upstreamResponse.statusCode || 502, upstreamResponse.headers);
      upstreamResponse.pipe(response);
    });
    upstream.on('error', () => response.writeHead(502).end());
    request.pipe(upstream);
    return;
  }
  if (pathname === '/teacher') {
    response.writeHead(308, { location: '/teacher/' }).end();
    return;
  }
  if (!pathname.startsWith('/teacher/')) {
    response.writeHead(404).end();
    return;
  }
  const relative = pathname.slice('/teacher/'.length);
  const candidate = path.resolve(root, relative || 'index.html');
  const file = candidate.startsWith(`${root}${path.sep}`) && fs.existsSync(candidate)
    && fs.statSync(candidate).isFile() ? candidate : path.join(root, 'index.html');
  response.writeHead(200, { 'content-type': contentType(file) });
  fs.createReadStream(file).pipe(response);
}).listen(port, '127.0.0.1');
