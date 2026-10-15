#!/usr/bin/env python3
"""Pulls the Visual C++ runtime DLLs out of Microsoft's VC_redist.x64.exe,
on Linux, into a zip for app-local deployment next to obs64.exe.

Usage: extract_vcruntime.py <VC_redist.x64.exe> <out.zip>

OBS's portable zip doesn't ship the runtime (its own installer runs
vc_redist, which needs admin rights), so on a machine without it obs64.exe
fails with "MSVCP140.dll / VCRUNTIME140_1.dll was not found". The DLLs
next to the exe are found first - Microsoft's supported "app-local"
deployment - so ClipEm stays portable and installs without elevation.
Only bin\\64bit needs them: every OBS plugin loads into obs64.exe, and the
other OBS executables (browser page, capture helpers) link the CRT
statically.

VC_redist.x64.exe is a WiX Burn bundle: its UX cab holds the bundle
manifest ("0") and its attached cab holds the packages (a0, a1, ...),
which the manifest maps to paths. The runtime DLLs are in
vc_runtimeMinimum_x64.msi, with their files in that package's cab1.cab.
"""

import os
import re
import struct
import subprocess
import sys
import tempfile
import zipfile

from msi_tables import extract_msi, msi_property

MINIMUM_MSI = "vc_runtimeMinimum_x64.msi"
REQUIRED = {"msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"}


def carve_cabs(exe_path, out_dir):
    """Burn appends its containers as plain cabinet files - find each by
    its MSCF header and cut it out at its declared size."""
    data = open(exe_path, "rb").read()
    paths, pos = [], 0
    while (pos := data.find(b"MSCF\0\0\0\0", pos)) >= 0:
        size = struct.unpack("<I", data[pos + 8:pos + 12])[0]
        if 0 < size <= len(data) - pos:
            path = os.path.join(out_dir, f"container{len(paths)}.cab")
            with open(path, "wb") as f:
                f.write(data[pos:pos + size])
            paths.append(path)
            pos += size
        else:
            pos += 4
    return paths


def unpack(cab_path, dest):
    subprocess.run(["7z", "x", "-y", f"-o{dest}", cab_path], check=True, stdout=subprocess.DEVNULL)
    return dest


def main():
    exe_path, out_zip = sys.argv[1:3]
    with tempfile.TemporaryDirectory() as tmp:
        containers = [unpack(cab, cab + ".d") for cab in carve_cabs(exe_path, tmp)]
        ux = next((d for d in containers if os.path.exists(os.path.join(d, "0"))), None)
        if not ux:
            sys.exit(f"No Burn bundle manifest found in {exe_path}")
        manifest = open(os.path.join(ux, "0"), encoding="utf-8-sig").read()

        # Payload SourcePath (name inside the attached container) -> FilePath.
        payloads = {}
        for tag in re.findall(r"<Payload [^>]*>", manifest):
            source = re.search(r'SourcePath="([^"]+)"', tag)
            file_path = re.search(r'FilePath="([^"]+)"', tag)
            if source and file_path:
                payloads[file_path.group(1).replace("\\", "/")] = source.group(1)

        msi_rel = next((p for p in payloads if p.endswith("/" + MINIMUM_MSI)), None)
        if not msi_rel:
            sys.exit(f"{MINIMUM_MSI} isn't in {exe_path}'s bundle manifest - has the redist's layout changed?")
        cab_rel = msi_rel.rsplit("/", 1)[0] + "/cab1.cab"

        def payload_path(rel):
            name = payloads.get(rel)
            found = [os.path.join(d, name) for d in containers if name and os.path.exists(os.path.join(d, name))]
            if not found:
                sys.exit(f"Payload {rel} not found in {exe_path}'s containers")
            return found[0]

        work = os.path.join(tmp, "msi")
        files = extract_msi(payload_path(msi_rel), work, cab_path=payload_path(cab_rel))
        version = msi_property(os.path.join(work, "streams"), "ProductVersion")

        dlls = [(name, src) for parts, name, src in files if name.lower().endswith(".dll")]
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as out:
            for name, src in dlls:
                out.write(src, name)

    missing = sorted(REQUIRED - {name.lower() for name, _ in dlls})
    if missing:
        sys.exit(f"{exe_path} didn't yield {', '.join(missing)}")
    print(f"Bundling VC++ runtime {version}: {', '.join(sorted(name for name, _ in dlls))} -> {out_zip}")


if __name__ == "__main__":
    main()
