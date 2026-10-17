// POST /report (multipart): description, version, platform and a "logs" zip
// from bug_report.send_bug_report(). The zip goes to the private R2 bucket;
// only the description, version and platform go in the public issue, since
// the logs hold usernames and folder paths.

const MAX_DESCRIPTION = 5000;
const MAX_LOGS_BYTES = 20 * 1024 * 1024;
const MAX_SHORT_FIELD = 100;
const TITLE_LENGTH = 80;
const LABELS = ["bug-report", "from-app"];

export default {
  async fetch(request, env) {
    if (new URL(request.url).pathname !== "/report") {
      return json({ error: "Not found." }, 404);
    }
    if (request.method !== "POST") {
      return json({ error: "Method not allowed." }, 405);
    }

    const ip = request.headers.get("CF-Connecting-IP") || "unknown";
    const { success } = await env.PER_IP.limit({ key: ip });
    if (!success) {
      return json({ error: "Too many reports from this network - try again in a minute." }, 429);
    }
    // Refuse oversized uploads before reading them; a little headroom for the text fields.
    if (Number(request.headers.get("Content-Length") || 0) > MAX_LOGS_BYTES + 64 * 1024) {
      return json({ error: "The report is too large." }, 413);
    }

    let form;
    try {
      form = await request.formData();
    } catch {
      return json({ error: "Expected a multipart form." }, 400);
    }
    const description = String(form.get("description") || "").trim();
    const version = shortField(form.get("version"));
    const platform = shortField(form.get("platform"));
    const logs = form.get("logs");
    if (!description || description.length > MAX_DESCRIPTION) {
      return json({ error: `A description of 1-${MAX_DESCRIPTION} characters is required.` }, 400);
    }
    if (!(logs instanceof File)) {
      return json({ error: "The logs zip is missing." }, 400);
    }
    if (logs.size > MAX_LOGS_BYTES) {
      return json({ error: "The report is too large." }, 413);
    }

    const id = reportId();
    await env.REPORTS.put(`reports/${id}.zip`, await logs.arrayBuffer(), {
      httpMetadata: { contentType: "application/zip" },
      customMetadata: { version, platform },
    });

    let issue;
    try {
      issue = await createIssue(env, id, description, version, platform);
    } catch (err) {
      console.error(`Report ${id}: creating the issue failed: ${err}`);
      return json({ id, error: "The logs were uploaded but the GitHub issue couldn't be created." }, 502);
    }
    return json({ id, issue_url: issue.html_url });
  },
};

async function createIssue(env, id, description, version, platform) {
  const firstLine = description.split("\n").find((line) => line.trim()) || "Bug report";
  const title = `[App report] ${noMentions(truncate(firstLine.trim(), TITLE_LENGTH))}`;
  const quoted = noMentions(description)
    .split("\n")
    .map((line) => `> ${line}`)
    .join("\n");
  const body = [
    quoted,
    "",
    `**ClipEm version:** ${version || "unknown"}`,
    `**Platform:** ${platform || "unknown"}`,
    `**Logs:** private report \`${id}\``,
    "",
    "_Sent from the app's Send Bug Report button._",
  ].join("\n");

  const res = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/issues`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "clipem-bug-reports",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ title, body, labels: LABELS }),
  });
  if (!res.ok) {
    throw new Error(`GitHub answered ${res.status}: ${(await res.text()).slice(0, 500)}`);
  }
  return res.json();
}

// Version and platform are one-line labels: no newlines or Markdown.
function shortField(value) {
  return truncate(String(value || "").replace(/[\r\n`*<>[\]]/g, " ").trim(), MAX_SHORT_FIELD);
}

// A zero-width space after "@" so a report can't ping anyone.
function noMentions(text) {
  return text.replace(/@/g, "@​");
}

function truncate(text, length) {
  return text.length > length ? `${text.slice(0, length - 1)}…` : text;
}

function reportId() {
  const date = new Date().toISOString().slice(0, 10).replace(/-/g, "");
  const random = [...crypto.getRandomValues(new Uint8Array(4))]
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
  return `${date}-${random}`;
}

function json(data, status = 200) {
  return Response.json(data, { status });
}
