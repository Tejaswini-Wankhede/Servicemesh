import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Dev-time proxy so the browser talks to one origin and CORS never bites.
    proxy: {
      '/api': { target: process.env.VITE_API_BASE_URL || 'http://localhost:8000', changeOrigin: true },
      '/health': { target: process.env.VITE_API_BASE_URL || 'http://localhost:8000', changeOrigin: true }
    }
  },
  build: { outDir: 'dist', sourcemap: false }
})
