import createNextIntlPlugin from "next-intl/plugin";

const withNextIntl = createNextIntlPlugin("./src/i18n/request.ts");

/**
 * §17 and §1 N6: nothing secret reaches the browser bundle.
 *
 * `serverExternalPackages` keeps server-only modules out of client compilation,
 * and the absence of any `env` block here is deliberate -- Next inlines
 * everything listed there into the client bundle, so a single well-meaning
 * entry would ship a vendor key to every browser that loads the panel.
 * Server-side configuration is read through `process.env` in server components
 * only, and `src/server/env.ts` is marked `server-only` so an accidental client
 * import fails the build rather than the audit.
 */
const config = withNextIntl({
  reactStrictMode: true,
  poweredByHeader: false,
  typedRoutes: true,
  // The runtime image copies `.next/standalone`, which contains the server and
  // only the dependencies Next traced -- roughly 150 MB against a gigabyte for
  // a full `node_modules`. Without this the Dockerfile's COPY finds nothing and
  // the image fails to build, so the two are a pair.
  output: "standalone",
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          {
            key: "Permissions-Policy",
            // The sandbox test call (§15.1 Flows & Prompts) needs the mic on
            // this origin. Everything else is off.
            value: "camera=(), geolocation=(), microphone=(self)",
          },
        ],
      },
    ];
  },
});

export default config;
