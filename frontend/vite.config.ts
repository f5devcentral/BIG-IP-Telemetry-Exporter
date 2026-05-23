import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRootLogo = path.resolve(__dirname, "..", "F5-logo-F5-rgb.svg");
const publicLogo = path.resolve(__dirname, "public", "F5-logo-F5-rgb.svg");

/** Copy repo-root F5-logo-F5-rgb.svg into public/ for dev server and production build. */
function copyRepoRootFavicon(): Plugin {
  return {
    name: "copy-repo-root-favicon",
    buildStart() {
      if (!fs.existsSync(repoRootLogo)) {
        throw new Error(`Missing favicon source: ${repoRootLogo}`);
      }
      fs.mkdirSync(path.dirname(publicLogo), { recursive: true });
      fs.copyFileSync(repoRootLogo, publicLogo);
    },
  };
}

export default defineConfig({
  plugins: [copyRepoRootFavicon(), react()],
  server: {
    host: true,
    fs: {
      allow: [path.resolve(__dirname, "..")],
    },
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8001",
        changeOrigin: true,
        timeout: 600_000,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
