"use client";

import { useState, useSyncExternalStore } from "react";
import installManifest from "../public/updates/install-manifest.json";
import { RELEASES_URL } from "./site";

const WINDOWS_EXE_URL =
  process.env.NEXT_PUBLIC_CLIPEM_EXE_URL ?? "https://clipem.app/ClipEmSetup.exe";
const LINUX_INSTALL_COMMAND = "curl -fsSL https://clipem.app/linuxinstall.sh | bash";
// Read from the install manifest at build time, so each release bumps it.
const VERSION_LABEL = (
  <p className="version-label">
    Version {installManifest.version} - <a href={RELEASES_URL}>all releases</a>
  </p>
);

type Platform = "windows" | "linux";
type OfflineDownload = { url: string; sha256: string; size: number };
// Listed by release.sh from the release that first built them - absent from
// older manifests, which shows no offline links rather than broken ones.
const OFFLINE: Partial<Record<Platform, OfflineDownload>> =
  (installManifest as { offline?: Partial<Record<Platform, OfflineDownload>> }).offline ?? {};
const OFFLINE_SETUP_STEP: Record<Platform, string> = {
  windows: "unzip it anywhere, then run Setup_Windows.bat",
  linux: "unzip it anywhere, then run ./ClipEmSetupLinux.sh",
};

function OfflineDownloadLink({ platform }: { platform: Platform }) {
  const download = OFFLINE[platform];
  if (!download) return null;
  const megabytes = Math.round(download.size / (1024 * 1024));
  return (
    <div className="offline-download">
      <p>
        <a href={download.url} download>
          Offline installer (.zip, {megabytes} MB)
        </a>{" "}
        - everything included, no internet needed: {OFFLINE_SETUP_STEP[platform]}.
      </p>
      <details>
        <summary>SHA-256</summary>
        <code>{download.sha256}</code>
      </details>
    </div>
  );
}
type DetectedPlatform = Platform | "unsupported";

function detectPlatform(): DetectedPlatform {
  const ua = navigator.userAgent;
  if (/Android|iPhone|iPad|iPod|Macintosh|Mac OS X|CrOS/.test(ua)) return "unsupported";
  if (/Linux/.test(ua)) return "linux";
  return "windows";
}

// The user agent never changes during a visit - nothing to subscribe to.
const subscribeToNothing = () => () => {};

export default function DownloadSection() {
  // The server snapshot ("windows") is what the static HTML is rendered
  // with, so hydration matches; React then re-renders with the detected
  // platform straight away.
  const detected = useSyncExternalStore<DetectedPlatform>(
    subscribeToNothing,
    detectPlatform,
    () => "windows",
  );
  const [chosen, setPlatform] = useState<Platform | null>(null);
  const platform: Platform = chosen ?? (detected === "linux" ? "linux" : "windows");
  const supported = detected !== "unsupported";
  const [copied, setCopied] = useState(false);

  async function copyCommand() {
    try {
      await navigator.clipboard.writeText(LINUX_INSTALL_COMMAND);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard API unavailable or permission denied - the command is
      // still plain selectable text, so this is a soft failure.
    }
  }

  return (
    <div className="download-section">
      {!supported && (
        <p className="unsupported-note">
          ClipEm runs on Windows and Linux PCs - pick the one you&apos;ll install it on.
        </p>
      )}
      <div className="platform-toggle" role="group" aria-label="Choose your platform">
        <button
          type="button"
          aria-pressed={platform === "windows"}
          className={`toggle-tab${platform === "windows" ? " active" : ""}`}
          onClick={() => setPlatform("windows")}
        >
          Windows
        </button>
        <button
          type="button"
          aria-pressed={platform === "linux"}
          className={`toggle-tab${platform === "linux" ? " active" : ""}`}
          onClick={() => setPlatform("linux")}
        >
          Linux
        </button>
      </div>

      {platform === "windows" ? (
        <>
          <a className="download-button" href={WINDOWS_EXE_URL} download>
            Download ClipEmSetup.exe
          </a>
          {VERSION_LABEL}
          <p className="platform-note">
            A small installer that downloads and verifies OBS, a portable
            Python, and ClipEm&apos;s shared resources, then walks you
            through setup.
          </p>
          <OfflineDownloadLink platform="windows" />
        </>
      ) : (
        <>
          <div className="command-box">
            <code>{LINUX_INSTALL_COMMAND}</code>
            <button type="button" className="copy-button" onClick={copyCommand}>
              {copied ? "Copied!" : "Copy"}
            </button>
            <span className="visually-hidden" aria-live="polite">
              {copied ? "Install command copied to clipboard" : ""}
            </span>
          </div>
          {VERSION_LABEL}
          <p className="platform-note">
            Downloads and verifies OBS, a portable Python, and ClipEm&apos;s
            shared resources into ./ClipEm, then walks you through setup.
            No root needed.
          </p>
          <OfflineDownloadLink platform="linux" />
        </>
      )}
    </div>
  );
}
