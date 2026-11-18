"""What ClipEm's own ffmpeg builds have (build/build-ffmpeg.sh builds only
what it lists), and what an ffmpeg command asks for - so tests can check the
app never asks for something those builds lack. A Discord export once failed
for every 60 fps clip on the bundled ffmpeg (no fps filter) while every test
passed against a full system ffmpeg."""

import os
import re

DEV_DIR = os.path.dirname(os.path.abspath(__file__))
BUILD_SCRIPT = os.path.join(DEV_DIR, "build", "build-ffmpeg.sh")
KINDS = ("filter", "muxer", "encoder", "decoder")
# Built without being listed: configure's mp4 muxer brings the mov one, and
# pcm_s16le's muxer is called s16le.
IMPLIED = {"muxer": {"mov", "s16le"}}


def built():
    """{"filter"|"muxer"|"encoder"|"decoder": names} the build script builds in
    (--enable-<kind>=) or checks the build has (check_has -<kind>s)."""
    with open(BUILD_SCRIPT, encoding="utf-8") as script:
        text = script.read()
    found = {kind: set(IMPLIED.get(kind, ())) for kind in KINDS}
    for kind in KINDS:
        for listed in re.findall(rf"--enable-{kind}=['\"]?([\w,]+)", text):
            found[kind].update(listed.split(","))
        for listed in re.findall(rf"^check_has -{kind}s (.+)$", text, re.MULTILINE):
            found[kind].update(listed.split())
    return found


def filter_names(graph):
    """Every filter a filtergraph uses: split into chains and filters at
    the ; and , outside quotes, brackets and parentheses."""
    names, part, depth, quoted, escaped = [], "", 0, False, False
    for char in graph + ";":
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "'":
            quoted = not quoted
        elif not quoted and char in "([":
            depth += 1
        elif not quoted and char in ")]":
            depth -= 1
        elif not quoted and not depth and char in ",;":
            name = part.lstrip("[]").split("=", 1)[0]
            name = name.split("]")[-1] if "]" in name else name
            if name.strip():
                names.append(name.strip())
            part = ""
            continue
        part += char
    return names


def graphs(cmd):
    return [cmd[i + 1] for i, arg in enumerate(cmd[:-1]) if arg in ("-filter_complex", "-vf", "-af")]


def used(cmd):
    """{"filter"|"muxer"|"encoder": names} one ffmpeg command asks for. The
    muxer: an -f after the last -i (one before it is an input's format), or
    image2 for a picture; encoders: every -c/-c:v/-c:a:N that isn't copy."""
    found = {kind: set() for kind in KINDS}
    for graph in graphs(cmd):
        found["filter"].update(filter_names(graph))
    last_input = max((i for i, arg in enumerate(cmd) if arg == "-i"), default=-1)
    for i, (arg, value) in enumerate(zip(cmd, cmd[1:], strict=False)):
        if arg == "-f" and i > last_input:
            found["muxer"].add(value)
        if re.fullmatch(r"-c(odec)?(:[av](:\d+)?)?", arg) and value != "copy":
            found["encoder"].add(value)
    if not found["muxer"] and os.path.splitext(cmd[-1])[1].lower() in (".jpg", ".jpeg", ".png"):
        found["muxer"].add("image2")
        found["encoder"].add("mjpeg" if cmd[-1].lower().endswith((".jpg", ".jpeg")) else "png")
    return found


def missing(commands):
    """{kind: sorted names} the commands ask for that the builds lack."""
    have, asked = built(), {kind: set() for kind in KINDS}
    for cmd in commands:
        for kind, names in used(cmd).items():
            asked[kind] |= names
    return {kind: sorted(asked[kind] - have[kind]) for kind in KINDS if asked[kind] - have[kind]}, asked
