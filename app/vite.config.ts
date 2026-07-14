import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const PRODUCTION_CSP = "default-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'";

// Note: resolution of @learn-ukrainian/activity-kit uses normal package resolution
// from the vendored tarball installed under node_modules (via package.json "exports").
// ACTIVITY_KIT_DIR env can still be used by advanced setups if needed, but not required here.

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [
    react(),
    {
      name: 'production-csp-preview',
      configurePreviewServer(server) {
        server.middlewares.use((req, res, next) => {
          res.setHeader('Content-Security-Policy', PRODUCTION_CSP);
          if (req.url) {
            const url = new URL(req.url, 'http://localhost');
            if (
              url.pathname.startsWith('/teacher/assets/') ||
              url.pathname.startsWith('/teacher/favicon.svg') ||
              url.pathname.startsWith('/teacher/icons.svg')
            ) {
              req.url = req.url.replace('/teacher', '');
            }
          }
          next();
        });
      },
    },
  ],
  // No aliases: rely on package.json "exports" in the installed @learn-ukrainian/activity-kit
  // For production static bundle served under /teacher/ (Caddy), keep '' for flexible base.
  // Dev serves at / ; router and links use /teacher/* paths explicitly.
  base: '',
  server: {
    port: 5173,
    proxy: {
      // Proxy /api to the dev stub so E2E and local dev use same-origin fetches (no CORS, matches prod contract).
      // The stub runs on 8787; browser always talks to vite on 5173.
      '/api': {
        target: 'http://localhost:8787',
        changeOrigin: false,
      },
    },
  },
  preview: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://localhost:8787',
        changeOrigin: false,
      },
    },
  },
})
