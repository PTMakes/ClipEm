#!/usr/bin/env python3
"""
Writes App/Shared/checksums.json (sha256 of every shipped file, keyed
relative to App/Shared - see setup.py's verify_checksums()) and, optionally,
the legacy clipem-latest.json version manifest for a release zip.
"""

import argparse
import os
import subprocess

from release_common import release_asset_url, sha256_of_file, write_json_atomic

SHIPPED_IGNORED = ("Tools", "clipem-app-capture.exe")
EXCLUDE_NAMES = {"__pycache__", ".DS_Store"}

# Top-level entries under App/Shared that setup/the updater rewrite or
# preserve per install (same set as updater.PRESERVED_RELATIVE_PATHS) -
# hashing them would make setup.py's verify_checksums() fail on every
# configured install.
EXCLUDE_TOP_LEVEL = {"OBS-Config", "run_config.json", "update_config.json"}


def git_listed_files(root_dir):
    """Files git would ship from root_dir (tracked + untracked-but-not-
    ignored) - the same list release.sh zips, so caches or other ignored
    local files never end up in the manifest. None outside a git checkout."""
    try:
        out = subprocess.run(
            ["git", "-C", root_dir, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--full-name", "."],
            check=True, capture_output=True,
        ).stdout
        top = subprocess.run(
            ["git", "-C", root_dir, "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    listed = [os.path.join(top, rel) for rel in out.decode().split("\0") if rel]
    # Built at release time and git-ignored, but shipped (release.sh stages it by hand).
    helper = os.path.join(root_dir, *SHIPPED_IGNORED)
    if os.path.isfile(helper) and helper not in listed:
        listed.append(helper)
    return listed


def iter_files(root_dir, manifest_path):
    listed = git_listed_files(root_dir)
    if listed is not None:
        for path in sorted(listed):
            rel_parts = os.path.relpath(path, root_dir).split(os.sep)
            if rel_parts[0] in EXCLUDE_TOP_LEVEL or path == manifest_path:
                continue
            if os.path.isfile(path) and not os.path.islink(path):
                yield path
        return
    for dirpath, dirnames, filenames in os.walk(root_dir):
        at_top = dirpath == root_dir
        dirnames[:] = [
            d for d in dirnames
            if d not in EXCLUDE_NAMES
            and not (at_top and d in EXCLUDE_TOP_LEVEL)
            and not os.path.islink(os.path.join(dirpath, d))
        ]
        for name in filenames:
            if name in EXCLUDE_NAMES or name.endswith(".pyc") or (at_top and name in EXCLUDE_TOP_LEVEL):
                continue
            path = os.path.join(dirpath, name)
            if path == manifest_path:
                continue
            if not os.path.islink(path):
                yield path


def write_checksums(app_dir):
    manifest_path = os.path.join(app_dir, "checksums.json")
    manifest = {}
    count = 0
    for path in iter_files(app_dir, manifest_path):
        rel_path = os.path.relpath(path, app_dir).replace(os.sep, "/")
        manifest[rel_path] = sha256_of_file(path)
        count += 1
    print(f" - Hashed {count} files.")

    write_json_atomic(manifest_path, manifest, sort_keys=True)
    print(f" - Wrote {len(manifest)} entries to {manifest_path}")


def write_version_manifest(zip_path, version, url, manifest_out):
    """clipem-latest.json - read by pre-1.0.22 installs' updaters only (newer
    ones read the signed install-manifest.json)."""
    manifest = {"version": version, "url": url, "sha256": sha256_of_file(zip_path)}
    write_json_atomic(manifest_out, manifest)
    print(f" - Wrote version manifest to {manifest_out}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate checksums.json for an app dir, and optionally the "
        "update version manifest (clipem-latest.json) for a release zip."
    )
    parser.add_argument("app_dir", help="Path to App/App/Shared to checksum")
    parser.add_argument("--zip", help="Path to the shared-resources release zip")
    parser.add_argument("--version", help="Version string to record in the version manifest")
    parser.add_argument(
        "--manifest-out",
        help="Path to write the version manifest JSON (e.g. clipem-latest.json)",
    )
    parser.add_argument(
        "--skip-checksums",
        action="store_true",
        help="Only write the version manifest; leave checksums.json as-is",
    )
    parser.add_argument(
        "--url",
        help="Public URL of the zip (default: its GitHub release asset URL)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    app_dir = os.path.abspath(args.app_dir)

    if not args.skip_checksums:
        print(f"Generating checksums.json for {app_dir}")
        write_checksums(app_dir)

    manifest_args = [args.zip, args.version, args.manifest_out]
    if any(manifest_args):
        if not all(manifest_args):
            raise SystemExit("--zip, --version, and --manifest-out must be given together")
        url = args.url or release_asset_url(args.version, os.path.basename(args.zip))
        write_version_manifest(args.zip, args.version, url, args.manifest_out)


if __name__ == "__main__":
    main()
