# ClipEm Website

The landing page for [clipem.app](https://clipem.app), plus the small files
older ClipEm installs read from it. It's built with Next.js as a
static export and deployed as a Cloudflare Worker that serves only static assets.

## What's here

| Path | What it is |
| --- | --- |
| `app/` | The landing page (`page.tsx`), download/install picker (`DownloadSection.tsx`), metadata (`layout.tsx`) and the site constants they share (`site.ts`). |
| `public/updates/install-manifest.json` + `.sig` | A copy of the latest release's signed manifest (each artifact's URL and sha256). Installs up to 1.3.5 read it here; newer ones read it from the GitHub release. The page also builds its version label and offline links from it. |
| `public/updates/clipem-latest.json` + `.sig` | The oldest update feed. Only installs older than 1.0.22 read it. |
| `public/_headers`, `public/_redirects` | Cloudflare response headers (security headers, `no-cache` on manifests) and the `/ClipEmSetup.exe` and `/linuxinstall.sh` redirects to the latest release's installers. |
| `public/.assetsignore` | Keeps release binaries (`*.zip`, `*.tar.gz`, `*.exe`) from ever being deployed, even if a copy is sitting in `public/`. |
| `public/robots.txt`, `public/sitemap.xml`, `public/og-image.png` | Search engine and link preview files. |
| `wrangler.jsonc` | The Cloudflare Workers deploy config: static assets from `out/`, with Next's `404.html` for unknown routes. |

`Dev/release.sh` in the parent repo writes every file in `public/updates/`.
Don't edit them by hand: the `.sig` files are
Ed25519 signatures over the exact manifest bytes, and clients reject any
manifest whose signature doesn't match.

Release binaries are not committed here. That covers the installers,
the shared zips, the Linux OBS build and the portable Python. They are assets
of the GitHub releases of [PTMakes/ClipEm](https://github.com/PTMakes/ClipEm/releases),
and the manifest points at them. `https://clipem.app/ClipEmSetup.exe`
redirects to the latest release's `ClipEmSetupWindows.exe`, and
`https://clipem.app/linuxinstall.sh` to its `ClipEmSetupLinux.sh`.

## Development

This needs Node 20.9 or newer (`engines` in `package.json`). CI uses the
version in `.nvmrc` (22).

```
npm ci
npm run dev        # http://localhost:3000
npm run lint
npm run typecheck
npm run build      # static export -> out/
```

`NEXT_PUBLIC_CLIPEM_EXE_URL` optionally overrides the Windows download link.
It defaults to `https://clipem.app/ClipEmSetup.exe`.

## Deploying (Cloudflare Workers)

The site is a Worker with static assets only: no Worker script and no
server build. In Cloudflare's build settings:

- Build command: `npm run build`
- Deploy command: `npx wrangler deploy` (the Wrangler version pinned in
  `package-lock.json`, serving `out/` as `wrangler.jsonc` says)

The Worker's name in the Cloudflare dashboard must match `name` in
`wrangler.jsonc` (`clipem`).

Deploy only after `Dev/release.sh` has published the GitHub release, so the
new manifests never point at files that aren't public yet.
