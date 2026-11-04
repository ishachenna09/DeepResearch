import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      // Optional: avoids CORS during dev; axios still targets :8000 directly
      "/api": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
});
