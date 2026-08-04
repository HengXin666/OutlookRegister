import { defineConfig, loadEnv } from "vite"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "")
  return {
    plugins: [react(), tailwindcss()],
    server: {
      host: "127.0.0.1",
      port: 5174,
      proxy: {
        "/api": env.OUTLOOK_API_TARGET || "http://127.0.0.1:8765",
      },
    },
  }
})
