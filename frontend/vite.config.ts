import { sveltekit } from "@sveltejs/kit/vite";
import tailwindcss from "@tailwindcss/vite";
import { SvelteKitPWA } from "@vite-pwa/sveltekit";
import { defineConfig } from "vite";
import { compression } from "vite-plugin-compression2";

export default defineConfig({
    build: { target: "es2022", cssMinify: "lightningcss" },
    plugins: [
        tailwindcss(),
        sveltekit(),
        SvelteKitPWA({
            registerType: "autoUpdate",
            includeAssets: [
                "favicon.ico",
                "apple-touch-icon.png",
                "pwa-192x192.png",
                "pwa-512x512.png",
                "pwa-maskable-192x192.png",
                "pwa-maskable-512x512.png",
            ],
            workbox: { maximumFileSizeToCacheInBytes: 8_000_000 },
            manifest: {
                name: "AniBridge",
                short_name: "AniBridge",
                icons: [
                    {
                        src: "pwa-192x192.png",
                        sizes: "192x192",
                        type: "image/png",
                        purpose: "any",
                    },
                    {
                        src: "pwa-512x512.png",
                        sizes: "512x512",
                        type: "image/png",
                        purpose: "any",
                    },
                    {
                        src: "pwa-maskable-192x192.png",
                        sizes: "192x192",
                        type: "image/png",
                        purpose: "maskable",
                    },
                    {
                        src: "pwa-maskable-512x512.png",
                        sizes: "512x512",
                        type: "image/png",
                        purpose: "maskable",
                    },
                ],
                start_url: "./",
                scope: "./",
                display: "standalone",
                background_color: "#05070d",
                theme_color: "#020618",
                description:
                    "The smart way to keep your anime lists perfectly synchronized.",
            },
        }),
        compression({ algorithms: ["gzip", "brotli"] }),
    ],
    server: {
        proxy: {
            "/api": { target: "http://localhost:4848", changeOrigin: true },
            "/docs": { target: "http://localhost:4848", changeOrigin: true },
            "/healthz": { target: "http://localhost:4848", changeOrigin: true },
            "/livez": { target: "http://localhost:4848", changeOrigin: true },
            "/readyz": { target: "http://localhost:4848", changeOrigin: true },
            "/ws": { target: "http://localhost:4848", changeOrigin: true, ws: true },
        },
    },
});
