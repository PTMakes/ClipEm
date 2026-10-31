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

DISK_SPACE = (
    "Every minute ClipEm checks the free space on the drive your clips are saved to. It warns you when "
    "less than 20 GB is left, and again (every 15 minutes while OBS is recording) below 5 GB - OBS stops "
    "recording when the drive is full. It also tells you each time a recording passes another 100 GB."
)

MIC_CHECK = (
    "While OBS is saving clips, ClipEm checks your mic every 10 seconds. If it has been taken off the "
    "track clips keep, or turned almost all the way down, ClipEm puts it back. If no sound at all has come "
    "from it for 2 minutes (unplugged, or the device changed), ClipEm reopens it, and tells you if that "
    "doesn't help. If it's muted in OBS, ClipEm lets you know. Only the mixer's level numbers are read, "
    "never the sound itself."
)

PEAK_GUARD = (
    "OBS watches the level of each audio source (your mic, desktop audio and so on). When one gets within "
    "1 dB of the maximum - where OBS's meters show clipping and recordings start to distort - its volume is "
    "turned down so that peak would land on the ceiling below, but never below -20 dB. Only level numbers "
    "are read, never the sound itself, and the lowered volume stays until you change it in OBS's Audio Mixer."
)

SCRIPT_DEBUG = (
    "Writes extra detail about what ClipEm's OBS script is doing to OBS's script log (Tools > Scripts > "
    "Script Log in OBS). Only useful when tracking down a problem - leave it off otherwise."
)
