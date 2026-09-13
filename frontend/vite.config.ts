import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'

// 构建产物直接放进 Python 包里，`vsum ui` 用 FastAPI 静态服务。
// 开发时 `npm run dev` 起在 5173，/api 代理到后端的 7860。
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    outDir: '../src/video_summarizer/web/dist',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:7860', changeOrigin: true },
    },
  },
})
