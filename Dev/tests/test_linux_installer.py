"""Runs the real ClipEmSetupLinux.sh against a local HTTP server serving a
manifest signed with a throwaway key (CLIPEM_TEST_SIGNING_PUBLIC_KEY), with
tiny stand-ins for the portable Python, OBS, ffmpeg and the shared zip."""

import functools
import hashlib
import http.server
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import zipfile

import pytest
from conftest import INSTALLER_DIR

SCRIPT = os.path.join(INSTALLER_DIR, "Scripts", "ClipEmSetupLinux.sh")

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not all(shutil.which(t) for t in ("bash", "curl", "tar", "sha256sum", "base64")),
    reason="needs Linux with bash, curl, tar, sha256sum and base64",
)

# Stands in for App/Shared/Installer/setup.py: records which interpreter
# ran it (the fake portable python3 sets CLIPEM_FAKE_PORTABLE_PYTHON).
FAKE_SETUP_PY = '''
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))

def record_local_checksums(root_dirs, label):
    with open(os.path.join(HERE, "checksums-recorded"), "w") as f:
        f.write(label)

def record_installed_sha256(component, entry):
    with open(os.path.join(HERE, component + "-sha256-recorded"), "w") as f:
        f.write(entry["sha256"])

if __name__ == "__main__":
    with open(os.path.join(HERE, "setup-ran"), "w") as f:
        f.write(os.environ.get("CLIPEM_FAKE_PORTABLE_PYTHON", "system python"))
'''


def _tar_gz(files):
    """{path: (content, mode)} -> .tar.gz bytes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, (content, mode) in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "www"
    root.mkdir()
    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = functools.partial(QuietHandler, directory=str(root))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield root, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def release(server, signing_key):
    """Publishes the stand-in artifacts plus a signed manifest. Returns the
    manifest URL."""
    root, base = server
    fake_python = f'#!/bin/sh\nCLIPEM_FAKE_PORTABLE_PYTHON=1 exec "{sys.executable}" "$@"\n'
    artifacts = {
        "python.tar.gz": _tar_gz({"python/bin/python3": (fake_python, 0o755)}),
        "obs.tar.gz": _tar_gz({"obs-install/bin/obs": ("#!/bin/sh\n", 0o755)}),
        "ffmpeg.tar.gz": _tar_gz({"ffmpeg/bin/ffmpeg": ("#!/bin/sh\n", 0o755)}),
        "shared.zip": _zip({"Shared/Installer/setup.py": FAKE_SETUP_PY, "Shared/VERSION": "9.9.9"}),
    }
    entries = {}
    for name, data in artifacts.items():
        (root / name).write_bytes(data)
        entries[name] = {"url": f"{base}/{name}", "sha256": hashlib.sha256(data).hexdigest()}
    # Same key order generate_install_manifest.py writes (the installer
    # scans for linux.python with awk before it has a JSON parser).
    manifest = {
        "version": "9.9.9",
        "linux": {
            "obs": entries["obs.tar.gz"],
            "python": entries["python.tar.gz"],
            "shared": entries["shared.zip"],
            "ffmpeg": entries["ffmpeg.tar.gz"],
        },
    }
    manifest_bytes = json.dumps(manifest, indent=2).encode()
    (root / "install-manifest.json").write_bytes(manifest_bytes)
    (root / "install-manifest.json.sig").write_text(signing_key.sign(manifest_bytes))
    return f"{base}/install-manifest.json"


def _run_installer(install_dir, manifest_url, public_key):
    # A system-wide `obs` on PATH, which the installer must ignore.
    system_bin = install_dir.parent / "system-bin"
    system_bin.mkdir(exist_ok=True)
    (system_bin / "obs").write_text("#!/bin/sh\n")
    (system_bin / "obs").chmod(0o755)
    env = dict(
        os.environ,
        PATH=f"{system_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        CLIPEM_MANIFEST_URL=manifest_url,
        CLIPEM_TEST_SIGNING_PUBLIC_KEY=public_key,
    )
    return subprocess.run(
        ["bash", SCRIPT, str(install_dir)], env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=120,
    )


def test_fresh_install_uses_the_portable_python_and_bundled_obs(tmp_path, release, signing_key):
    install_dir = tmp_path / "ClipEm"
    result = _run_installer(install_dir, release, signing_key.public_b64)
    assert result.returncode == 0, result.stdout + result.stderr

    app = install_dir / "App"
    assert (app / "Linux" / "python" / "bin" / "python3").exists()
    assert (app / "Linux" / "obs-install" / "bin" / "obs").exists()  # despite the system obs on PATH
    assert (app / "Linux" / "ffmpeg" / "bin" / "ffmpeg").exists()
    installer = app / "Shared" / "Installer"
    # setup.py ran under the portable Python even though a system python3 exists.
    assert (installer / "setup-ran").read_text() == "1"
    assert (installer / "checksums-recorded").read_text() == "Linux OBS"
    # So the updater can tell a rebuilt OBS (same version) from this one.
    obs_sha256 = hashlib.sha256((tmp_path / "www" / "obs.tar.gz").read_bytes()).hexdigest()
    assert (installer / "obs-sha256-recorded").read_text() == obs_sha256
    assert "release signature OK" in result.stdout


def test_rerun_only_fetches_whats_missing(tmp_path, release, signing_key):
    install_dir = tmp_path / "ClipEm"
    assert _run_installer(install_dir, release, signing_key.public_b64).returncode == 0
    (install_dir / "App" / "Shared" / "Installer" / "checksums-recorded").unlink()

    result = _run_installer(install_dir, release, signing_key.public_b64)

    assert result.returncode == 0, result.stdout + result.stderr
    for artifact in ("portable Python", "OBS", "ffmpeg", "shared ClipEm resources"):
        assert f"Fetching {artifact}" not in result.stdout and f"Fetching ClipEm's {artifact}" not in result.stdout
    assert not (install_dir / "App" / "Shared" / "Installer" / "checksums-recorded").exists()


def test_complete_install_runs_without_internet(tmp_path, server, release, signing_key):
    """The offline zip already has everything: no manifest is needed at all."""
    install_dir = tmp_path / "ClipEm"
    assert _run_installer(install_dir, release, signing_key.public_b64).returncode == 0
    (install_dir / "App" / "Shared" / "Installer" / "setup-ran").unlink()

    result = _run_installer(install_dir, "http://127.0.0.1:9/install-manifest.json", signing_key.public_b64)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Fetching install manifest" not in result.stdout
    assert (install_dir / "App" / "Shared" / "Installer" / "setup-ran").exists()


def test_manifest_signed_by_another_key_is_refused(tmp_path, release, signing_key):
    other_key = "qVRRlix2EQKA14sroLDWymLKDYVH0PYLClc88D5A/zA="  # the real release key
    result = _run_installer(tmp_path / "ClipEm", release, other_key)
    assert result.returncode != 0
    assert "INVALID" in result.stderr
    assert not (tmp_path / "ClipEm" / "App" / "Linux").exists()


def test_tampered_download_is_refused(tmp_path, server, release, signing_key):
    root, _ = server
    (root / "python.tar.gz").write_bytes(b"not what the manifest says")
    result = _run_installer(tmp_path / "ClipEm", release, signing_key.public_b64)
    assert result.returncode != 0
    assert "Checksum mismatch" in result.stderr
    assert not (tmp_path / "ClipEm" / "App" / "Linux" / "python").exists()
