// defineConfig comes from vitest/config, not vite, so the `test` block is typed.
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'

// import.meta.dirname rather than __dirname: Vite's native config loader, which
// becomes the default in a future major, does not provide the CommonJS global.
const rootDir = import.meta.dirname

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': path.resolve(rootDir, './src') },
  },
  server: {
    port: 5173,
    proxy: {
      // Proxying in development keeps the browser same-origin, so there is no
      // CORS preflight and no divergence from production, where the API sits
      // behind the same gateway host.
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    // Threads rather than the default child processes. Forking a worker per
    // test file fails outright on some Windows setups - "Timeout waiting for
    // worker to respond", with no tests run and an exit code that looks like a
    // configuration error rather than an environment one. These tests are pure
    // functions over in-memory data with nothing to isolate between files, so
    // the extra isolation a process gives buys nothing that would justify a
    // suite that only runs on some machines.
    pool: 'threads',
  },
})
