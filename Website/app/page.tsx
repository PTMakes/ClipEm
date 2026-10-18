import Image from "next/image";
import Link from "next/link";
import DownloadSection from "./DownloadSection";
import { DESCRIPTION, REPO_URL, SITE_URL } from "./site";
import installManifest from "../public/updates/install-manifest.json";

// Only the fields read here - "sources" is absent from manifests published
// before it was added.
type Manifest = {
  version: string;
  linux: { obs: { url: string } };
  sources?: { ffmpeg?: { url: string } };
};
const manifest: Manifest = installManifest;

// linux-obs-install-<version>.tar.gz - ClipEm's Linux build of that OBS tag.
const linuxObsVersion = manifest.linux.obs.url.match(/linux-obs-install-([\d.]+)\.tar\.gz$/)?.[1];
const ffmpegSourceUrl = manifest.sources?.ffmpeg?.url;

const FEATURES = [
  {
    title: "One hotkey, instant clips",
    description:
      "OBS's replay buffer is always running, so pressing the clip hotkey (Alt+~ by default) saves the last 15 seconds to 10 minutes - you choose how far back - with an optional sound to confirm it.",
  },
  {
    title: "Discord-sized copies",
    description:
      "Each clip can also be saved as a smaller copy, cropped to its last few seconds and re-encoded to fit Discord's free upload limit, while the full-quality clip is kept.",
  },
  {
    title: "Tray app",
    description:
      "Save a clip, open the clip editor or your clips folder, start or stop the replay buffer or a recording, show OBS and change settings from the system tray - without opening OBS.",
  },
  {
    title: "Pre-configured OBS",
    description:
      "ClipEm installs its own portable copy of OBS Studio, set up for clipping. Any OBS you already have is left exactly as it is.",
  },
  {
    title: "Windows and Linux",
    description:
      "Works on Windows and on Linux desktops, including Wayland with PipeWire screen capture. It recovers a frozen or black screen capture by itself, and warns before your disk fills up.",
  },
  {
    title: "Update notifications",
    description:
      "ClipEm checks for new releases when it starts and asks before installing anything. Updates keep all of your settings.",
  },
];

const EDITOR_FEATURES = [
  {
    title: "Browse every clip",
    description:
      "All your clips, newest first, in a sidebar you can hide. Hover one to play it with sound and move across it to scrub - no need to open each file.",
  },
  {
    title: "Trim and cut",
    description:
      "Drag the handles to trim the start and end, cut out the boring middle, or keep only one part. Undo and redo cover every edit.",
  },
  {
    title: "Edit each audio track",
    description:
      "With split audio tracks on, your mic, desktop audio and individual apps are separate tracks - mute, solo, shift or change the volume of each, or of just one section.",
  },
  {
    title: "Export for Discord",
    description:
      "One click exports a copy sized to fit Discord's free upload limit. There are also full-quality and custom exports, with your choice of resolution, frame rate and bitrate.",
  },
];

const STEPS = [
  {
    title: "Install",
    description: "Run the Windows installer, or paste one command into a Linux terminal. No admin or root rights needed.",
  },
  {
    title: "Set it up",
    description:
      "A short setup wizard picks your screen, resolution, audio devices, clip length, quality and hotkey.",
  },
  {
    title: "Press the hotkey",
    description: "When something worth keeping happens, press it. The clip is already in your clips folder.",
  },
];

const FAQ = [
  {
    question: "Is ClipEm free?",
    answer: "Yes. ClipEm is a free download for Windows and Linux.",
  },
  {
    question: "Does ClipEm change my existing OBS?",
    answer:
      "No. ClipEm runs its own portable copy of OBS Studio with its own settings, kept inside the ClipEm folder. An OBS you already have installed isn't touched.",
  },
  {
    question: "How far back can a clip go?",
    answer:
      "Anywhere from 15 seconds to 10 minutes - you pick the replay buffer length during setup, and can change it later.",
  },
  {
    question: "Where are my clips saved?",
    answer:
      "In the folder you choose during setup. Re-running setup or updating ClipEm never removes clips.",
  },
  {
    question: "Does it work on Linux with Wayland?",
    answer:
      "Yes - ClipEm's OBS uses PipeWire screen capture. The tray icon works on KDE Plasma, XFCE and MATE; on GNOME it needs the AppIndicator extension.",
  },
  {
    question: "Does ClipEm update itself?",
    answer:
      "It can check for new releases each time it starts. When there is one, it asks whether to update now, at the next restart, or not at all.",
  },
  {
    question: "Do I need a separate video editor?",
    answer:
      "No. ClipEm's clip editor (Open Clip Editor in the tray menu) trims and cuts clips, edits each audio track and exports full-quality or Discord-sized copies. Your original clips are never changed, and you can still open a clip in your own editor with one button.",
  },
  {
    question: "Is there a macOS version?",
    answer: "Not at the moment - ClipEm supports Windows and Linux.",
  },
];

const STRUCTURED_DATA = [
  {
    "@context": "https://schema.org",
    "@type": "SoftwareApplication",
    name: "ClipEm",
    description: DESCRIPTION,
    url: SITE_URL,
    image: `${SITE_URL}/og-image.png`,
    applicationCategory: "MultimediaApplication",
    operatingSystem: "Windows, Linux",
    softwareVersion: manifest.version,
    downloadUrl: `${SITE_URL}/ClipEmSetup.exe`,
    offers: { "@type": "Offer", price: "0", priceCurrency: "USD" },
  },
  {
    "@context": "https://schema.org",
    "@type": "FAQPage",
    mainEntity: FAQ.map(({ question, answer }) => ({
      "@type": "Question",
      name: question,
      acceptedAnswer: { "@type": "Answer", text: answer },
    })),
  },
];

export default function Home() {
  return (
    <>
      <header className="site-header">
        <Link href="/" className="site-logo" aria-label="ClipEm home">
          <Image src="/logo.webp" alt="" width={56} height={56} priority />
        </Link>
      </header>
      <main className="page">
        <script
          type="application/ld+json"
          // Static data, no user input - "<" is escaped anyway so it can never close the tag.
          dangerouslySetInnerHTML={{ __html: JSON.stringify(STRUCTURED_DATA).replace(/</g, "\\u003c") }}
        />
        <section className="hero">
          <p className="eyebrow">Free game clipping for Windows &amp; Linux</p>
          <h1>ClipEm</h1>
          <p className="subtitle">
            Save your best gaming moments the instant they happen with one
            hotkey. ClipEm sets up OBS&apos;s replay buffer for you, and its
            built-in editor trims, cuts and exports your clips - no extra
            software, no hassle.
          </p>
          <DownloadSection />
        </section>

        <section className="features" aria-label="Features">
          {FEATURES.map((feature) => (
            <div className="feature-card" key={feature.title}>
              <h2>{feature.title}</h2>
              <p>{feature.description}</p>
            </div>
          ))}
        </section>

        <section className="section">
          <h2 className="section-title">Built-in clip editor</h2>
          <p className="section-intro">
            Open the editor from the tray to browse, trim and share your clips
            without leaving ClipEm. Edits are saved per clip and never touch the
            original file.
          </p>
          <div className="features features-compact features-two">
            {EDITOR_FEATURES.map((feature) => (
              <div className="feature-card" key={feature.title}>
                <h3>{feature.title}</h3>
                <p>{feature.description}</p>
              </div>
            ))}
          </div>
        </section>

        <section className="section">
          <h2 className="section-title">How it works</h2>
          <ol className="steps">
            {STEPS.map((step, index) => (
              <li className="step" key={step.title}>
                <span className="step-number" aria-hidden="true">{index + 1}</span>
                <h3>{step.title}</h3>
                <p>{step.description}</p>
              </li>
            ))}
          </ol>
        </section>

        <section className="section faq">
          <h2 className="section-title">Questions</h2>
          {FAQ.map(({ question, answer }) => (
            <details className="faq-item" key={question}>
              <summary>{question}</summary>
              <p>{answer}</p>
            </details>
          ))}
        </section>

        <footer className="site-footer">
          <h2>Source code &amp; licenses</h2>
          <p>
            ClipEm installs <a href="https://obsproject.com">OBS Studio</a> (GPLv2)
            and a build of <a href="https://ffmpeg.org">FFmpeg</a> with x264 (GPL).
            Their source code:
          </p>
          <ul>
            <li>
              <a href="https://github.com/obsproject/obs-studio">OBS Studio</a>
              {linuxObsVersion && (
                <>
                  {" "}(Linux build:{" "}
                  <a href={`https://github.com/obsproject/obs-studio/tree/${linuxObsVersion}`}>
                    {linuxObsVersion}
                  </a>
                  )
                </>
              )}
            </li>
            {ffmpegSourceUrl && (
              <li>
                <a href={ffmpegSourceUrl}>FFmpeg + x264 source for this release</a>
              </li>
            )}
          </ul>
          <p>
            Source code: <a href={REPO_URL}>{REPO_URL.replace("https://", "")}</a>
          </p>
        </footer>
      </main>
    </>
  );
}
