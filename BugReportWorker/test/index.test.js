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
