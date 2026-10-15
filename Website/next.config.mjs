/** @type {import('next').NextConfig} */
const nextConfig = {
  // Static HTML export (out/) for Cloudflare Pages - no server runtime.
  // Response headers and redirects live in public/_headers and
  // public/_redirects, which Pages applies to the exported files.
  output: "export",
  images: {
    unoptimized: true,
  },
};

export default nextConfig;
