"""The zip bug_report.create_bug_report() makes must pass the relay's own
check (BugReportWorker/src/report_zip.js) - every kind of report, or the
relay refuses it with "The logs aren't a ClipEm report"."""

import json
import os
import shutil
import subprocess

import bug_report
import pytest
from test_bug_report import install  # noqa: F401 - the fixture

WORKER_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "BugReportWorker")
CHECK = """
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
const { isReportZip } = await import(pathToFileURL(process.argv[1]).href);
const results = process.argv.slice(2).map((file) => {
  const bytes = readFileSync(file);
  return isReportZip(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
});
console.log(JSON.stringify(results));
"""

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="needs node")


def _relay_accepts(*paths):
    out = subprocess.run(
        ["node", "--input-type=module", "-e", CHECK, os.path.join(WORKER_DIR, "src", "report_zip.js"), *paths],
        check=True, capture_output=True, text=True,
    ).stdout
    return json.loads(out)


def test_the_relay_accepts_every_kind_of_report(install, monkeypatch):  # noqa: F811
    plain, with_error, unreadable, empty = (str(install / f"{n}.zip") for n in ("plain", "error", "unreadable", "empty"))
    bug_report.create_bug_report(plain)
    bug_report.create_bug_report(with_error, error="OBS crashed\nTraceback...")
    real_read = bug_report._read_text

    def flaky(path):
        if path.endswith("run.log"):
            raise PermissionError("locked")
        return real_read(path)

    monkeypatch.setattr(bug_report, "_read_text", flaky)
    bug_report.create_bug_report(unreadable)
    for folder in ("logs", "obs-logs", "crashes"):
        shutil.rmtree(install / folder)
        (install / folder).mkdir()
    bug_report.create_bug_report(empty)
    assert _relay_accepts(plain, with_error, unreadable, empty) == [True, True, True, True]


def test_log_names_with_spaces_and_accents_are_accepted(install):  # noqa: F811
    (install / "obs-logs" / "2026-10-27 13-18-02 journée.txt").write_text("x\n")
    path = str(install / "r.zip")
    bug_report.create_bug_report(path)
    assert _relay_accepts(path) == [True]
