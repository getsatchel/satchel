import { fileURLToPath, URL } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const target = process.env.SATCHEL_API_URL ?? "http://localhost:8080";

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  server: {
    port: 5173,
    proxy: {
      "/v1": target,
      "/auth": target,
      "/mcp": target,
      "/oauth": target,
      "/.well-known": target,
    },
  },
  build: { outDir: "dist", chunkSizeWarningLimit: 2500 },
});
