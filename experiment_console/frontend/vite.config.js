import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    // During development, proxy API calls to the backend
    proxy: {
      '/api': 'http://localhost:6000',
      '/health': 'http://localhost:6000',
    }
  }
})
