import { createReadStream, existsSync, statSync } from 'node:fs';
import { extname, join, normalize, resolve } from 'node:path';
import http from 'node:http';

const args = process.argv.slice(2);
const directoryArg = args.find((arg) => !arg.startsWith('-')) ?? 'out';

function parsePort() {
  const argPortIndex = args.findIndex((arg) => arg === '--port' || arg === '-p');

  if (argPortIndex >= 0) {
    const value = Number.parseInt(args[argPortIndex + 1] ?? '', 10);
    if (Number.isFinite(value) && value > 0) return value;
  }

  const envPort = Number.parseInt(process.env.PORT ?? '', 10);
  if (Number.isFinite(envPort) && envPort > 0) return envPort;

  return 3000;
}

const startPort = parsePort();
const rootDir = resolve(process.cwd(), directoryArg);

if (!existsSync(rootDir)) {
  console.error(`Static export directory not found: ${rootDir}`);
  console.error('Run npm run build before npm run start.');
  process.exit(1);
}

const mimeTypes = new Map([
  ['.css', 'text/css; charset=utf-8'],
  ['.gif', 'image/gif'],
  ['.html', 'text/html; charset=utf-8'],
  ['.ico', 'image/x-icon'],
  ['.js', 'text/javascript; charset=utf-8'],
  ['.json', 'application/json; charset=utf-8'],
  ['.jpg', 'image/jpeg'],
  ['.jpeg', 'image/jpeg'],
  ['.md', 'text/markdown; charset=utf-8'],
  ['.mjs', 'text/javascript; charset=utf-8'],
  ['.png', 'image/png'],
  ['.svg', 'image/svg+xml'],
  ['.txt', 'text/plain; charset=utf-8'],
  ['.webp', 'image/webp'],
  ['.woff', 'font/woff'],
  ['.woff2', 'font/woff2'],
  ['.xml', 'application/xml; charset=utf-8'],
]);

function send404(response) {
  response.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
  response.end('Not found');
}

function safeResolve(requestPath) {
  const relativePath = normalize(requestPath).replace(/^([.][.][/\\])+/, '');
  const filePath = resolve(rootDir, `.${relativePath}`);

  if (!filePath.startsWith(rootDir)) return null;
  return filePath;
}

function candidatePaths(requestPath) {
  const explicitPath = safeResolve(requestPath);
  if (!explicitPath) return [];

  const candidates = [explicitPath];

  if (!extname(explicitPath)) {
    candidates.push(join(explicitPath, 'index.html'));
    candidates.push(`${explicitPath}.html`);
    candidates.push(`${explicitPath}.txt`);
  }

  return candidates;
}

function pickFile(requestPath) {
  for (const candidate of candidatePaths(requestPath)) {
    if (!existsSync(candidate)) continue;

    const stats = statSync(candidate);
    if (stats.isDirectory()) {
      const indexPath = join(candidate, 'index.html');
      if (existsSync(indexPath)) return indexPath;
      continue;
    }

    if (stats.isFile()) return candidate;
  }

  return null;
}

const server = http.createServer((request, response) => {
  const url = new URL(request.url ?? '/', 'http://127.0.0.1');
  let requestPath = url.pathname;

  try {
    requestPath = decodeURIComponent(url.pathname);
  } catch {
    send404(response);
    return;
  }

  const filePath = pickFile(requestPath);

  if (!filePath) {
    send404(response);
    return;
  }

  const contentType = mimeTypes.get(extname(filePath)) ?? 'application/octet-stream';
  response.writeHead(200, { 'Content-Type': contentType });
  createReadStream(filePath).pipe(response);
});

function listen(port) {
  server.listen(port, '0.0.0.0');
}

server.on('listening', () => {
  const address = server.address();
  if (!address || typeof address === 'string') return;

  console.log(`Serving ${rootDir}`);
  console.log(`Local:   http://127.0.0.1:${address.port}`);
  console.log(`Network: http://0.0.0.0:${address.port}`);
});

server.on('error', (error) => {
  if (error && typeof error === 'object' && 'code' in error && error.code === 'EADDRINUSE') {
    const address = server.address();
    const nextPort = typeof address === 'object' && address ? address.port + 1 : startPort + 1;
    console.warn(`Port ${nextPort - 1} is busy, retrying on ${nextPort}...`);
    listen(nextPort);
    return;
  }

  throw error;
});

listen(startPort);