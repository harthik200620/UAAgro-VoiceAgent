import createNextIntlPlugin from "next-intl/plugin";

const withNextIntl = createNextIntlPlugin("./src/i18n/request.ts");

/**
 * Nothing secret reaches the browser bundle.
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
  // The dev server and `next build` write to the same directory by default,
  // and a build started while the dev server is running leaves it serving a
  // module table it did not write ("__webpack_modules__[moduleId] is not a
  // function" on the next request). Separate directories, so the two never
  // meet; the Dockerfile still copies `.next/standalone`.
  distDir: process.env.NODE_ENV === "development" ? ".next-dev" : ".next",
  experimental: {
    // Knowledge-base uploads (a 48-page PDF) go through a Server Action, and
    // the default 1 MB body limit would refuse them before the API saw them.
    serverActions: { bodySizeLimit: "25mb" },
  },
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
            // Nothing on the panel uses the camera, location or microphone:
            // "Play in Hindi voice" only plays audio, and a test call rings a
            // real phone.
            value: "camera=(), geolocation=(), microphone=()",
          },
        ],
      },
    ];
  },
});

export default config;
