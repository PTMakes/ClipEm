# ClipEm Website

The landing page for [clipem.app](https://clipem.app), plus the small files
older ClipEm installs read from it. It's built with Next.js as a
static export and deployed on Cloudflare Pages.

## What's here

| Path | What it is |
| --- | --- |
| `app/` | The landing page (`page.tsx`), download/install picker (`DownloadSection.tsx`) and metadata (`layout.tsx`). |
| `public/updates/install-manifest.json` + `.sig` | A copy of the latest release's signed manifest (each artifact's URL and sha256). Installs up to 1.3.5 read it here; newer ones read it from the GitHub release. The page also builds its version label and offline links from it. |
| `public/updates/clipem-latest.json` + `.sig` | The oldest update feed. Only installs older than 1.0.22 read it. |
| `public/_headers`, `public/_redirects` | Cloudflare Pages response headers (security headers, `no-cache` on manifests) and the `/ClipEmSetup.exe` and `/linuxinstall.sh` redirects to the latest release's installers. |

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

This needs Node 20.9 or newer (see `.nvmrc`).

```
npm ci
npm run dev        # http://localhost:3000
npm run lint
npm run typecheck
npm run build      # static export -> out/
```

`NEXT_PUBLIC_CLIPEM_EXE_URL` optionally overrides the Windows download link.
It defaults to `https://clipem.app/ClipEmSetup.exe`.

## Deploying (Cloudflare Pages)

- Build command: `npm run build`
- Build output directory: `out`

Deploy only after `Dev/release.sh` has published the GitHub release, so the
new manifests never point at files that aren't public yet.
