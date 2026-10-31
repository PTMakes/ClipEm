// npm test. The zips come from Python's zipfile, which is what the client uses.

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";

import { isReportZip } from "../src/report_zip.js";

// entries: [[name, text, optional compress_type]]
function pythonZip(entries) {
  const script = `
import io, json, sys, zipfile
out = io.BytesIO()
with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
    for name, text, *method in json.loads(sys.argv[1]):
        zf.writestr(name, text, *method)
sys.stdout.buffer.write(out.getvalue())
`;
  const bytes = execFileSync("python3", ["-c", script, JSON.stringify(entries)]);
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
}

const REPORT = [
  ["system-info.txt", "ClipEm version: 1.4.8\n"],
  ["clipem-logs/tray.log", "started\n".repeat(500)],
  ["obs-logs/2026-10-27 13-18-02.txt", "OBS 32.2.2\n"],
  ["obs-crashes/Crash 2026-10-20.txt", "crash\n"],
  ["clipem-logs/gone.log.unreadable.txt", "Couldn't read it\n"],
];

test("the client's report passes", () => {
  assert.equal(isReportZip(pythonZip(REPORT)), true);
});

test("a report of just the install summary passes", () => {
  assert.equal(isReportZip(pythonZip(REPORT.slice(0, 1))), true);
});

test("a non-ASCII log name passes", () => {
  assert.equal(isReportZip(pythonZip([...REPORT, ["obs-logs/journée.txt", "x"]])), true);
});

test("things that aren't a report zip are refused", () => {
  assert.equal(isReportZip(new ArrayBuffer(0)), false);
  assert.equal(isReportZip(new TextEncoder().encode("not a zip at all, but long enough to look").buffer), false);
  assert.equal(isReportZip(pythonZip([["clipem-logs/tray.log", "no summary"]])), false);
  assert.equal(isReportZip(pythonZip([["system-info.txt", "x"], ["setup.exe", "MZ"]])), false);
  assert.equal(isReportZip(pythonZip([["system-info.txt", "x"], ["obs-logs/deep/er.txt", "x"]])), false);
  assert.equal(isReportZip(pythonZip([["system-info.txt", "x"], ["obs-logs/..", "x"]])), false);
  assert.equal(isReportZip(pythonZip([["system-info.txt", "x"], ["obs-logs\\x.txt", "x"]])), false);
});

test("a compression method Python's client doesn't use is refused", () => {
  assert.equal(isReportZip(pythonZip([["system-info.txt", "x", 14]])), false); // LZMA
});

test("data after or before the zip is refused", () => {
  const zip = new Uint8Array(pythonZip(REPORT));
  const longer = new Uint8Array(zip.length + 4);
  longer.set(zip);
  assert.equal(isReportZip(longer.buffer), false);
  longer.set(zip, 4);
  longer.fill(0, 0, 4);
  assert.equal(isReportZip(longer.buffer), false);
});

test("a truncated zip is refused", () => {
  assert.equal(isReportZip(pythonZip(REPORT).slice(0, 100)), false);
});

test("entries that overlap are refused", () => {
  const zip = pythonZip(REPORT);
  const view = new DataView(zip);
  const directory = view.getUint32(zip.byteLength - 22 + 16, true);
  const second = directory + 46 + view.getUint16(directory + 28, true);
  view.setUint32(second + 42, 0, true); // the second entry claims the first one's data
  assert.equal(isReportZip(zip), false);
});

test("an encrypted entry is refused", () => {
  const zip = pythonZip(REPORT);
  const view = new DataView(zip);
  const directory = view.getUint32(zip.byteLength - 22 + 16, true);
  view.setUint16(directory + 8, view.getUint16(directory + 8, true) | 1, true);
  assert.equal(isReportZip(zip), false);
});

test("too much unpacked data is refused", () => {
  const zip = pythonZip(REPORT);
  const view = new DataView(zip);
  const directory = view.getUint32(zip.byteLength - 22 + 16, true);
  view.setUint32(directory + 24, 0xffffffff, true);
  assert.equal(isReportZip(zip), false);
});
