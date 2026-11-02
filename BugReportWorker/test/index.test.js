import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { afterEach, beforeEach, test } from "node:test";

import worker from "../src/index.js";

const ZIP = execFileSync("python3", ["-c", `
import io, sys, zipfile
out = io.BytesIO()
with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
    zf.writestr("system-info.txt", "ClipEm version: 1.4.8")
sys.stdout.buffer.write(out.getvalue())
`]);

const realFetch = globalThis.fetch;
let issues;

beforeEach(() => {
  issues = [];
  globalThis.fetch = async (url, init) => {
    issues.push(JSON.parse(init.body));
    return Response.json({ html_url: "https://github.com/PTMakes/ClipEm/issues/1" });
  };
});

afterEach(() => {
  globalThis.fetch = realFetch;
});

function fakeEnv({ alreadyToday = 0 } = {}) {
  const env = {
    GITHUB_REPO: "PTMakes/ClipEm",
    GITHUB_TOKEN: "token",
    stored: [],
    listed: [],
    PER_IP: { limit: async () => ({ success: true }) },
    REPORTS: {
      put: async (key) => env.stored.push(key),
      list: async ({ prefix, limit }) => {
        env.listed.push(prefix);
        return { truncated: alreadyToday > limit };
      },
    },
  };
  return env;
}

function report(logs = ZIP) {
  const form = new FormData();
  form.set("description", "It crashed");
  form.set("version", "1.4.8");
  form.set("platform", "Linux");
  form.set("logs", new File([logs], "report.zip", { type: "application/zip" }));
  return new Request("https://bugs.clipem.app/report", { method: "POST", body: form });
}

test("a report is stored and filed", async () => {
  const env = fakeEnv();
  const res = await worker.fetch(report(), env);
  assert.equal(res.status, 200);
  assert.equal(env.stored.length, 1);
  assert.equal(issues.length, 1);
  const day = new Date().toISOString().slice(0, 10).replace(/-/g, "");
  assert.deepEqual(env.listed, [`reports/${day}-`]);
  assert.match(env.stored[0], new RegExp(`^reports/${day}-[0-9a-f]{8}\\.zip$`));
});

test("logs that aren't a report zip go nowhere", async () => {
  const env = fakeEnv();
  const res = await worker.fetch(report("not a zip"), env);
  assert.equal(res.status, 400);
  assert.equal(env.stored.length, 0);
  assert.equal(issues.length, 0);
});

test("the daily cap stops reports from everyone", async () => {
  const env = fakeEnv({ alreadyToday: 101 });
  const res = await worker.fetch(report(), env);
  assert.equal(res.status, 429);
  assert.match((await res.json()).error, /today/);
  assert.equal(env.stored.length, 0);
  assert.equal(issues.length, 0);
});

function post(body, headers) {
  return new Request("https://bugs.clipem.app/report", { method: "POST", body, headers });
}

function reportWith(fields) {
  const form = new FormData();
  form.set("description", "It crashed");
  form.set("logs", new File([ZIP], "report.zip", { type: "application/zip" }));
  for (const [key, value] of Object.entries(fields)) {
    if (value === null) form.delete(key);
    else form.set(key, value);
  }
  return post(form);
}

async function refused(request, status, message, env = fakeEnv()) {
  const res = await worker.fetch(request, env);
  assert.equal(res.status, status);
  assert.match((await res.json()).error, message);
  assert.equal(env.stored.length, 0);
  assert.equal(issues.length, 0);
}

test("only /report answers", async () => {
  await refused(new Request("https://bugs.clipem.app/other", { method: "POST" }), 404, /Not found/);
});

test("only POST answers", async () => {
  await refused(new Request("https://bugs.clipem.app/report"), 405, /not allowed/);
});

test("too many reports from one address are refused, per address", async () => {
  const env = fakeEnv();
  const keys = [];
  env.PER_IP.limit = async ({ key }) => {
    keys.push(key);
    return { success: false };
  };
  await refused(post(new FormData(), { "CF-Connecting-IP": "203.0.113.9" }), 429, /Too many reports from this/, env);
  assert.deepEqual(keys, ["203.0.113.9"]);
});

test("an oversized upload is refused before it's read", async () => {
  const request = post("x", { "Content-Length": String(21 * 1024 * 1024) });
  await refused(request, 413, /too large/);
  assert.equal(request.bodyUsed, false);
});

test("something that isn't a form is refused", async () => {
  await refused(post("not a form", { "Content-Type": "text/plain" }), 400, /multipart/);
});

for (const [name, description] of [["missing", null], ["blank", "   \n "], ["too long", "x".repeat(5001)]]) {
  test(`a ${name} description is refused`, async () => {
    await refused(reportWith({ description }), 400, /description of 1-5000/);
  });
}

test("the longest description is accepted", async () => {
  const res = await worker.fetch(reportWith({ description: "x".repeat(5000) }), fakeEnv());
  assert.equal(res.status, 200);
});

test("logs are required, as a file", async () => {
  await refused(reportWith({ logs: null }), 400, /logs zip is missing/);
  await refused(reportWith({ logs: "just text" }), 400, /logs zip is missing/);
});

test("logs over the limit are refused", async () => {
  const logs = new File([new Uint8Array(20 * 1024 * 1024 + 1)], "report.zip");
  await refused(reportWith({ logs }), 413, /too large/);
});

test("the logs stay private: only the description, version and platform are public", async () => {
  const env = fakeEnv();
  const stored = [];
  env.REPORTS.put = async (key, zip, options) => stored.push({ key, zip, options });
  const res = await worker.fetch(report(), env);
  const { id, issue_url: issueUrl } = await res.json();
  assert.equal(issueUrl, "https://github.com/PTMakes/ClipEm/issues/1");
  assert.equal(stored[0].key, `reports/${id}.zip`);
  assert.deepEqual(Buffer.from(stored[0].zip), ZIP);
  assert.deepEqual(stored[0].options.customMetadata, { version: "1.4.8", platform: "Linux" });
  const [{ title, body, labels }] = issues;
  assert.equal(title, "[App report] It crashed");
  assert.match(body, /^> It crashed\n/);
  assert.match(body, /\*\*ClipEm version:\*\* 1\.4\.8/);
  assert.match(body, /\*\*Platform:\*\* Linux/);
  assert.ok(body.includes(`private report \`${id}\``));
  assert.ok(!body.includes("system-info"));
  assert.deepEqual(labels, ["bug-report", "from-app"]);
});

test("a report can't ping anyone", async () => {
  await worker.fetch(reportWith({ description: "cc @someone\nand @other" }), fakeEnv());
  const [{ title, body }] = issues;
  assert.ok(!/@[a-z]/.test(title) && !/@[a-z]/.test(body));
  assert.ok(title.includes("@​someone"));
});

test("version and platform can't add Markdown or lines to the issue", async () => {
  await worker.fetch(reportWith({ version: "1.0\n# Heading`*", platform: "<b>[x]</b>" }), fakeEnv());
  const [{ body }] = issues;
  assert.ok(body.includes("**ClipEm version:** 1.0  # Heading"));
  assert.ok(body.includes("**Platform:** b  x  /b"));
});

test("missing version and platform read unknown", async () => {
  await worker.fetch(reportWith({}), fakeEnv());
  const [{ body }] = issues;
  assert.ok(body.includes("**ClipEm version:** unknown") && body.includes("**Platform:** unknown"));
});

test("long fields are cut, and the title comes from the first line with words", async () => {
  const env = fakeEnv();
  const stored = [];
  env.REPORTS.put = async (key, zip, options) => stored.push(options);
  await worker.fetch(reportWith({ description: `\n  \n${"y".repeat(100)}\nsecond line`, version: "1".repeat(200) }), env);
  const [{ title, body }] = issues;
  assert.equal(title, `[App report] ${"y".repeat(79)}…`);
  assert.ok(body.includes("> second line"));
  assert.equal(stored[0].customMetadata.version.length, 100);
});

test("if GitHub refuses, the logs are kept and the id reported", async () => {
  const env = fakeEnv();
  const logged = console.error;
  const errors = [];
  console.error = (message) => errors.push(message);
  globalThis.fetch = async () => new Response("rate limited", { status: 403 });
  try {
    const res = await worker.fetch(report(), env);
    assert.equal(res.status, 502);
    const { id, error } = await res.json();
    assert.match(error, /uploaded/);
    assert.equal(env.stored[0], `reports/${id}.zip`);
    assert.match(errors[0], /GitHub answered 403: rate limited/);
  } finally {
    console.error = logged;
  }
});

test("GitHub is asked with the repo and token configured", async () => {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return Response.json({ html_url: "x" });
  };
  await worker.fetch(report(), fakeEnv());
  assert.equal(calls[0].url, "https://api.github.com/repos/PTMakes/ClipEm/issues");
  assert.equal(calls[0].init.headers.Authorization, "Bearer token");
  assert.equal(calls[0].init.method, "POST");
});
