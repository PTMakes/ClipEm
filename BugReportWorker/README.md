# ClipEm bug report relay

A Cloudflare Worker behind the tray's **Send Bug Report** button. It lets users file GitHub issues without a GitHub account:

- The user's description, the ClipEm version and the platform go into a **public** issue on `PTMakes/ClipEm`, labelled `bug-report` and `from-app`.
- The log zip (ClipEm/OBS logs and the install summary, with the websocket password removed) goes to the **private** R2 bucket `clipem-bug-reports` as `reports/<id>.zip`. The issue shows only the report ID.

Each IP may send 2 reports a minute, each zip may be up to 20 MB, and descriptions up to 5000 characters.

## One-time setup

1. Create the bucket with `npx wrangler r2 bucket create clipem-bug-reports`. Then add a lifecycle rule that deletes objects after 90 days: `npx wrangler r2 bucket lifecycle add clipem-bug-reports expire-reports reports/ --expire-days 90`.
2. Create the `bug-report` and `from-app` labels on the GitHub repo.
3. Create a fine-grained GitHub token. Limit it to the `PTMakes/ClipEm` repository, with **Issues: Read and write** as its only permission. Then run `npx wrangler secret put GITHUB_TOKEN`.
4. Run `npx wrangler deploy`. This serves the Worker at `https://bugs.clipem.app/report`, the URL in `bug_report.BUG_REPORT_URL`.

## Reading a report's logs

```
npx wrangler r2 object get clipem-bug-reports/reports/<id>.zip --remote --file <id>.zip
```

You can also download it from the R2 page of the Cloudflare dashboard.

## Local testing

```
npx wrangler dev   # set GITHUB_REPO to a throwaway repo first; put the token in .dev.vars
CLIPEM_BUG_REPORT_URL=http://127.0.0.1:8787/report python App/App/Shared/tray-widget/settings_dialog.py
```
