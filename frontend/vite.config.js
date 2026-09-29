import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
export default defineConfig({
  plugins: [vue()],
  build: { target: "es2022" },
  server: {
    host: "127.0.0.1",
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8765",
        changeOrigin: true,
        ws: true,
        configure(proxy) {
          const sameOrigin = (request) =>
            request.setHeader("Origin", "http://127.0.0.1:8765");
          proxy.on("proxyReq", sameOrigin);
          proxy.on("proxyReqWs", sameOrigin);
        },
      },
    },
  },
});
