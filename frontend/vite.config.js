import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { sentryVitePlugin } from "@sentry/vite-plugin";

// ✅ Full Sentry + Vite config
export default defineConfig({
  plugins: [
    react(),
    // Optional: enable Sentry build integration
    sentryVitePlugin({
      org: "your-org-slug", // found in Sentry settings
      project: "frontend",  // your frontend project name
      // You can remove authToken if you’re not uploading source maps
      authToken: process.env.SENTRY_AUTH_TOKEN,
      telemetry: false,
    }),
  ],
  build: {
    sourcemap: true, // optional but helps Sentry map stack traces
  },
});
