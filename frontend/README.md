# 拾光笺前端

React + TypeScript + Vite。后端在 `src/video_summarizer/web/api.py`。

```bash
npm install
npm run dev      # http://localhost:5173，/api 代理到 7860（先起 `vsum ui --no-browser`）
npm run build    # 产物写到 ../src/video_summarizer/web/dist/，随 Python 包分发，要提交
```

样式 token 在 `src/index.css` 顶部，视觉稿在 `../docs/mockup/v0.6-ui.html`。
