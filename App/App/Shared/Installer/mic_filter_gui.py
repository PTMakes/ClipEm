"""
MicFilterWizard - the popout launched from the audio devices page's "Start
Mic Filter Configuration" button (see gui.py). Split out of gui.py into its
own module purely to keep that file from growing unbounded; it follows the
same controller-dict pattern (see gui.py's module docstring / setup.py's
try_launch_gui) so it has no import-time dependency on setup.py either.
"""

import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

PAD = 16
MIC_FILTER_SENTENCE = "The quick brown fox jumped over the lazy dog"


class MicFilterWizard(tk.Toplevel):
    """Records a few seconds of quiet to find the mic's noise floor, then
    a spoken sentence to find its speaking level, and uses both (via
    controller's compute_mic_filter_values / save_mic_filter_levels - see
    setup.py) to set the OBS Noise Gate and Gain filters on the mic
    source."""

    def __init__(self, parent, controller, device_name, device_id, on_applied=None):
        super().__init__(parent)
        self.controller = controller
        self.device_name = device_name
        self.device_id = device_id
        self.on_applied = on_applied
        # Same 96 DPI pixel scaling as the wizard - see ui_scale.py.
        self.px = parent.px
        self.title("Mic Filter Configuration")
        self.geometry(f"{self.px(560)}x{self.px(380)}")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        self.container = ttk.Frame(self, padding=self.px(PAD))
        self.container.pack(fill="both", expand=True)

        self.noise_floor_db = None
        self.speech_level_db = None

        self._show_intro()

    def _clear(self):
        for widget in self.container.winfo_children():
            widget.destroy()

    def _run_measurement(self, seconds, on_done, noise_floor_db=None):
        """Runs measure_audio_level_db in a background thread and polls
        for its result, so the countdown label keeps updating instead of
        the whole Toplevel freezing for the recording's duration.
        noise_floor_db is only passed for the speech measurement (see
        _start_speech_recording) - it switches measure_audio_level_db to
        voice-activity-gated averaging so the result doesn't drift with
        how fast someone reads the sentence."""
        result = {}

        def worker():
            try:
                result["value"] = self.controller["measure_audio_level_db"](
                    self.device_name, self.device_id, seconds, noise_floor_db
                )
            except Exception as exc:
                result["error"] = str(exc)

        threading.Thread(target=worker, daemon=True).start()

        def poll():
            if "value" not in result and "error" not in result:
                self.after(100, poll)
                return
            if "error" in result:
                messagebox.showerror(
                    "Mic Filter Configuration", f"Could not record audio:\n{result['error']}", parent=self,
                )
                self.destroy()
                return
            on_done(result["value"])

        self.after(100, poll)

    def _start_countdown(self, seconds, label_var):
        start_time = time.time()

        def tick():
            if not self.winfo_exists():
                return
            remaining = max(0.0, seconds - (time.time() - start_time))
            label_var.set(f"{remaining:.0f}")
            if remaining > 0:
                self.after(200, tick)

        tick()

    def _show_intro(self):
        self._clear()
        ttk.Label(self.container, text="Mic Filter Configuration", font=("", 12, "bold")).pack(anchor="w")
        ttk.Label(
            self.container,
            text=(
                "This measures your mic's background noise and speaking "
                "volume, then sets OBS's Noise Gate and Gain filters to "
                "match: first a few seconds of quiet, then a sentence "
                "read aloud."
            ),
            justify="left", wraplength=self.px(520),
        ).pack(anchor="w", pady=(self.px(8), self.px(PAD)))
        bar = ttk.Frame(self.container)
        bar.pack(side="bottom", fill="x")
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="left")
        ttk.Button(bar, text="Begin", command=self._show_noise_floor).pack(side="right")

    def _show_noise_floor(self):
        self._clear()
        seconds = self.controller["noise_floor_sample_seconds"]
        ttk.Label(self.container, text="Measuring noise floor", font=("", 12, "bold")).pack(anchor="w")
        ttk.Label(
            self.container,
            text="Stay quiet for a few seconds - don't talk or move anything near the mic.",
            justify="left", wraplength=self.px(520),
        ).pack(anchor="w", pady=(self.px(8), self.px(PAD)))
        countdown_var = tk.StringVar(value=str(seconds))
        ttk.Label(self.container, textvariable=countdown_var, font=("", 28, "bold")).pack(pady=self.px(PAD))

        self._start_countdown(seconds, countdown_var)

        def on_measured(noise_floor_db):
            self.noise_floor_db = noise_floor_db
            self._show_noise_floor_result()

        self._run_measurement(seconds, on_measured)

    def _show_noise_floor_result(self):
        """A checkpoint after the noise floor measurement, rather than
        jumping straight to the speech prompt - a cough, a chair creak, or
        someone walking in during the "quiet" window would otherwise
        silently poison the noise floor with no way back but restarting
        the whole wizard."""
        self._clear()
        ttk.Label(self.container, text="Noise floor measured", font=("", 12, "bold")).pack(anchor="w")
        ttk.Label(
            self.container, text=f"{self.noise_floor_db:.1f} dBFS", font=("", 22, "bold"),
        ).pack(pady=self.px(PAD))
        ttk.Label(
            self.container,
            text="If there was any noise during that measurement (a cough, a door, etc.), redo it.",
            justify="left", wraplength=self.px(520),
        ).pack(anchor="w", pady=(0, self.px(PAD)))
        bar = ttk.Frame(self.container)
        bar.pack(side="bottom", fill="x")
        ttk.Button(bar, text="Redo", command=self._show_noise_floor).pack(side="left")
        ttk.Button(bar, text="Continue", command=self._show_speech_prompt).pack(side="right")

    def _show_speech_prompt(self):
        self._clear()
        seconds = self.controller["speech_sample_seconds"]
        ttk.Label(self.container, text="Measuring speaking level", font=("", 12, "bold")).pack(anchor="w")
        ttk.Label(
            self.container,
            text="Speak into your mic like normal. Click start and then read "
            "the following sentence out loud in the 5 second window.",
            justify="left", wraplength=self.px(520),
        ).pack(anchor="w", pady=(self.px(8), self.px(4)))
        ttk.Label(
            self.container, text=f"“{MIC_FILTER_SENTENCE}”",
            justify="left", wraplength=self.px(520), font=("", 10, "italic"),
        ).pack(anchor="w", pady=(0, self.px(PAD)))

        self.speech_start_btn = ttk.Button(
            self.container, text="Start Recording",
            command=lambda: self._start_speech_recording(seconds),
        )
        self.speech_start_btn.pack()

    def _start_speech_recording(self, seconds):
        self.speech_start_btn.configure(state="disabled", text="Recording...")
        countdown_var = tk.StringVar(value=str(seconds))
        ttk.Label(self.container, textvariable=countdown_var, font=("", 24, "bold")).pack(pady=(self.px(PAD // 2), 0))

        self._start_countdown(seconds, countdown_var)

        def on_measured(speech_level_db):
            self.speech_level_db = speech_level_db
            self._show_speech_result()

        self._run_measurement(seconds, on_measured, noise_floor_db=self.noise_floor_db)

    def _show_speech_result(self):
        """Same checkpoint idea as _show_noise_floor_result - a stumbled
        word or a mic bump partway through the sentence shouldn't force
        starting the whole wizard over."""
        self._clear()
        ttk.Label(self.container, text="Speaking level measured", font=("", 12, "bold")).pack(anchor="w")
        ttk.Label(
            self.container, text=f"{self.speech_level_db:.1f} dBFS", font=("", 22, "bold"),
        ).pack(pady=self.px(PAD))
        ttk.Label(
            self.container,
            text="If you stumbled over the sentence or weren't speaking at a normal volume, redo it.",
            justify="left", wraplength=self.px(520),
        ).pack(anchor="w", pady=(0, self.px(PAD)))
        bar = ttk.Frame(self.container)
        bar.pack(side="bottom", fill="x")
        ttk.Button(bar, text="Redo", command=self._show_speech_prompt).pack(side="left")
        ttk.Button(bar, text="Continue", command=self._show_results).pack(side="right")

    def _show_results(self):
        self._clear()
        gain_db, open_db, close_db = self.controller["compute_mic_filter_values"](
            self.noise_floor_db, self.speech_level_db
        )
        ttk.Label(self.container, text="Results", font=("", 12, "bold")).pack(anchor="w")
        ttk.Label(
            self.container,
            text=(
                f"Noise floor: {self.noise_floor_db:.1f} dBFS\n"
                f"Speaking level: {self.speech_level_db:.1f} dBFS\n\n"
                f"Gain filter: {gain_db:+.1f} dB\n"
                f"Noise Gate: open {open_db:.1f} dB / close {close_db:.1f} dB"
            ),
            justify="left",
        ).pack(anchor="w", pady=(self.px(8), self.px(PAD)))

        bar = ttk.Frame(self.container)
        bar.pack(side="bottom", fill="x")
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="left")
        ttk.Button(bar, text="Redo", command=self._show_noise_floor).pack(side="left", padx=(self.px(8), 0))

        def apply():
            try:
                self.controller["save_mic_filter_levels"](self.noise_floor_db, self.speech_level_db)
            except Exception as exc:
                messagebox.showerror(
                    "Mic Filter Configuration", f"Could not save filter settings:\n{exc}", parent=self,
                )
                return
            if self.on_applied:
                self.on_applied()
            self.destroy()

        ttk.Button(bar, text="Apply", command=apply).pack(side="right")
