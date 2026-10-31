// Whether an upload has the shape of the zip bug_report.create_bug_report()
// makes, judged from its headers alone: nothing is decompressed, so this stays
// cheap, and anything Python's zipfile doesn't write is refused. It keeps
// junk out of the bucket; it can't prove who made the zip.

const LOCAL_SIGNATURE = 0x04034b50;
const CENTRAL_SIGNATURE = 0x02014b50;
const END_SIGNATURE = 0x06054b50;
const LOCAL_SIZE = 30;
const CENTRAL_SIZE = 46;
const END_SIZE = 22;

const INFO_NAME = "system-info.txt";
// One folder deep: every file is a log, named after its file on disk.
const LOG_NAME = /^(clipem-logs|obs-logs|obs-crashes)\/[^/\\]+$/;
// system-info.txt, up to 1000 ClipEm logs, 10 OBS logs and 5 crash reports.
const MAX_ENTRIES = 1016;
// Sizes as declared - a lie here only fools this limit, not the CRC on opening.
const MAX_UNPACKED_BYTES = 1024 * 1024 * 1024;

const METHODS = new Set([0, 8]); // stored, deflated
const ENCRYPTED_FLAG = 1;

const names = new TextDecoder("utf-8", { fatal: true });

export function isReportZip(buffer) {
  try {
    return checkZip(buffer);
  } catch {
    return false; // an offset past the end, or a name that isn't UTF-8
  }
}

function checkZip(buffer) {
  const zip = new DataView(buffer);
  // Python writes no archive comment, so the end record ends the file.
  const end = zip.byteLength - END_SIZE;
  if (end < 0 || zip.getUint32(end, true) !== END_SIGNATURE || zip.getUint16(end + 20, true) !== 0) {
    return false;
  }
  const count = zip.getUint16(end + 10, true);
  const directory = zip.getUint32(end + 16, true);
  if (count < 1 || count > MAX_ENTRIES || directory + zip.getUint32(end + 12, true) !== end) {
    return false;
  }

  let at = directory;
  let nextData = 0; // entries sit back to back, which also rules out overlapping ones
  let unpacked = 0;
  let hasInfo = false;
  for (let i = 0; i < count; i++) {
    if (zip.getUint32(at, true) !== CENTRAL_SIGNATURE) return false;
    const nameLength = zip.getUint16(at + 28, true);
    const name = names.decode(new Uint8Array(buffer, at + CENTRAL_SIZE, nameLength));
    if (name === INFO_NAME) {
      hasInfo = true;
    } else if (!LOG_NAME.test(name) || name.endsWith("/..") || name.endsWith("/.")) {
      return false;
    }
    if (zip.getUint16(at + 8, true) & ENCRYPTED_FLAG || !METHODS.has(zip.getUint16(at + 10, true))) {
      return false;
    }
    unpacked += zip.getUint32(at + 24, true);
    if (unpacked > MAX_UNPACKED_BYTES) return false;

    const local = zip.getUint32(at + 42, true);
    if (local !== nextData || zip.getUint32(local, true) !== LOCAL_SIGNATURE) return false;
    nextData = local + LOCAL_SIZE + zip.getUint16(local + 26, true) + zip.getUint16(local + 28, true)
      + zip.getUint32(at + 20, true);

    at += CENTRAL_SIZE + nameLength + zip.getUint16(at + 30, true) + zip.getUint16(at + 32, true);
  }
  return hasInfo && nextData === directory && at === end;
}
