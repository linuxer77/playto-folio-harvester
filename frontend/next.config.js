/** @type {import("next").NextConfig} */
const backendInternalUrl = process.env.BACKEND_INTERNAL_URL || "http://localhost:8080"

const nextConfig = {
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${backendInternalUrl}/api/:path*`,
      },
    ]
  },
}

module.exports = nextConfig
