import base64
import json
import os
import shutil
import subprocess
import sys

import pytest

DEV_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_SHARED_DIR = os.path.join(os.path.dirname(DEV_DIR), "App", "App", "Shared")
INSTALLER_DIR = os.path.join(APP_SHARED_DIR, "Installer")
APP_INSTALLER_DIR = INSTALLER_DIR
TRAY_WIDGET_DIR = os.path.join(APP_SHARED_DIR, "tray-widget")
SCRIPTS_DIR = os.path.join(APP_SHARED_DIR, "Scripts")
sys.path.insert(0, DEV_DIR)
sys.path.insert(0, INSTALLER_DIR)


class SigningKey:
    """A throwaway Ed25519 key made with openssl - what release_common.py
    signs releases with - standing in for the real release key."""

    def __init__(self, directory):
        self.private_pem = os.path.join(directory, "test-signing-key.pem")
        subprocess.run(
            ["openssl", "genpkey", "-algorithm", "ed25519", "-out", self.private_pem],
            check=True, capture_output=True,
        )
        der = subprocess.run(
            ["openssl", "pkey", "-in", self.private_pem, "-pubout", "-outform", "DER"],
            check=True, capture_output=True,
        ).stdout
        self.public_raw = der[-32:]
        self.public_b64 = base64.b64encode(self.public_raw).decode("ascii")
        self._dir = directory

    def sign(self, data: bytes) -> str:
        """Base64 signature, as release_common.sign_file writes to .sig files."""
        msg = os.path.join(self._dir, "msg.bin")
        sig = os.path.join(self._dir, "sig.bin")
        with open(msg, "wb") as f:
            f.write(data)
        subprocess.run(
            ["openssl", "pkeyutl", "-sign", "-inkey", self.private_pem, "-rawin", "-in", msg, "-out", sig],
            check=True, capture_output=True,
        )
        with open(sig, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")


@pytest.fixture(scope="session")
def signing_key(tmp_path_factory):
    if not shutil.which("openssl"):
        pytest.skip("needs openssl")
    try:
        return SigningKey(str(tmp_path_factory.mktemp("signing")))
    except subprocess.CalledProcessError:
        pytest.skip("needs openssl 3.0+ (Ed25519 raw signing)")


@pytest.fixture
def fake_obs_config(tmp_path, monkeypatch):
    """Points obs_config at a minimal basic.ini, obs-websocket config and
    scene collection in tmp_path."""
    import obs_config

    basic_ini = tmp_path / "basic.ini"
    basic_ini.write_text("[General]\nName=DefaultPTM\n\n[Hotkeys]\nReplayBuffer={}\n")
    ws_config = tmp_path / "config.json"
    ws_config.write_text(json.dumps({"server_port": 4455, "server_password": "old-password-123"}))
    scenes = tmp_path / "scenes.json"
    scenes.write_text(json.dumps({"modules": {"scripts-tool": [
        {"path": "/x/stuck_output_check.py", "settings": {"stuck_check_enabled": True}},
    ]}}))
    monkeypatch.setattr(obs_config, "BASIC_INI", str(basic_ini))
    monkeypatch.setattr(obs_config, "OBS_WEBSOCKET_CONFIG_PATH", str(ws_config))
    monkeypatch.setattr(obs_config, "SCENE_COLLECTION_JSON", str(scenes))
    monkeypatch.setattr(obs_config, "PENDING_OBS_SETTINGS_PATH", str(tmp_path / "pending.json"))
    monkeypatch.setattr(obs_config.clipem_common, "RUN_CONFIG_PATH", str(tmp_path / "run_config.json"))
    return tmp_path
