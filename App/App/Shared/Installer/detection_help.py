"""How the tray widget's checks and ClipEm's OBS script options work, in the
user's words: shown under each option on the setup wizard's Detection &
Warnings and Audio Peak Guard pages (gui.py) and behind the "i" button next
to each in Settings > Scripts (settings_dialog.py). Keep these in line with
tray-widget/stuck_screen.py, disk_space.py and Scripts/stuck_output_check.py."""

STUCK_SCREEN = (
    "Every 10 seconds ClipEm looks at a small copy of OBS's screen capture (in memory - nothing is saved). "
    "If the picture hasn't changed for about 50 seconds while you're using the PC, the capture has probably "
    "frozen, so ClipEm restarts it and lets you know. A screen that's just static is left alone. On Linux it also notices when ClipEm loses access to your screen (for example after "
    "sleep or a restart), opens the screen picker and asks you to pick your screen again."
)

BLACK_SCREEN = (
    "Every 10 seconds ClipEm checks what OBS is recording. If it has been completely black for about "
    "30 seconds, ClipEm shows the screen capture again if it was hidden, points it back at your screen and "
    "restarts it, then lets you know. On Linux it opens the screen picker for you to choose your screen again, "
    "unless the screen is locked."
)

POST_CLIP = (
    "The clip hotkey keeps recording for this long after you press it, to catch what's said right "
    "after. The clip itself is saved as usual (ending at the press) together with a copy that has "
    "the extra seconds (\"-extra.mkv\"). The clip editor opens that copy with its end on the press - "
    "drag the end handle (or use Include extra) to keep the extra seconds.\n\n"
    "The clip sound still plays at the press; ClipEm takes it back out of the copy's Desktop and Mix "
    "tracks (what your mic picked up from your speakers stays).\n\n"
    "The replay buffer is kept this much longer, which uses a little more memory. Applies when OBS restarts."
)

DISK_SPACE = (
    "Every minute ClipEm checks the free space on the drive your clips are saved to. It warns you when "
    "less than 20 GB is left, and again (every 15 minutes while OBS is recording) below 5 GB - OBS stops "
    "recording when the drive is full. It also tells you each time a recording passes another 100 GB."
)

MIC_CHECK = (
    "While OBS is saving clips, ClipEm checks your mic and desktop audio every 10 seconds. A mic taken off "
    "the clip track or turned right down is put back. If the mic goes silent for a minute, or desktop audio "
    "hears nothing while an app plays sound, ClipEm reopens it and switches it to your default device - "
    "then tells you, every 5 minutes, until it's fixed. Only sound levels are measured; nothing is kept."
)

PEAK_GUARD = (
    "OBS watches each audio source's level (mic, desktop audio and so on). When one gets within 1 dB of "
    "the maximum - clipping - its volume is turned down so that peak lands on the ceiling below (never "
    "below -20 dB), then back up 1 dB at a time - unless it keeps clipping, when it stays down. A volume "
    "you change by hand is left alone. Only level numbers are read, never the sound."
)

SCRIPT_DEBUG = (
    "Writes extra detail about what ClipEm's OBS script is doing to OBS's script log (Tools > Scripts > "
    "Script Log in OBS). Only useful when tracking down a problem - leave it off otherwise."
)
