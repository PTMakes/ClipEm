"""The Send Bug Report window: a description, then the report is sent to
ClipEm's bug report relay (bug_report.py) or saved as a zip instead.

Settings' Troubleshooting opens it, and so does every error popup's "Send Bug
Report..." (popups.showerror), with that error to start the description and
kept exactly in the report. The clip editor's page, which has no Tk, runs
this file instead:

  bug_report_dialog.py [--title TEXT] [--message TEXT]

which opens just this window, for the error the page showed, and exits when
it closes."""

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# The bundled Windows Python's ._pth file keeps the script's folder off sys.path.
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import bug_report  # noqa: E402
import dark_theme  # noqa: E402
import file_dialogs  # noqa: E402
import popups  # noqa: E402

TITLE = "Send Bug Report"


def reveal_file(path):
    """Shows path in the system's file manager (its folder, on Linux)."""
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path)])


def _in_background(widget, work, on_done):
    """work() on a worker thread, then on_done(result, error) back on the Tk
    thread - sending mustn't freeze the window."""
    results = queue.Queue()

    def worker():
        try:
            results.put((work(), None))
        except Exception as exc:
            results.put((None, exc))

    def poll():
        try:
            result, error = results.get_nowait()
        except queue.Empty:
            widget.after(100, poll)
            return
        on_done(result, error)

    threading.Thread(target=worker, daemon=True).start()
    widget.after(100, poll)


def starting_description(error):
    """The description a report from an error popup starts with: the error -
    the home folder shortened to ~, as the description is posted publicly."""
    home = os.path.expanduser("~")
    if home and home not in ("~", os.sep):
        error = error.replace(home, "~")
    return f"I got this error:\n{error}\n\nWhat I was doing: "


def open_bug_report(parent, error=None, log=None, wait=False):
    """Opens the Send Bug Report window over parent. error: the error popup
    it's sent from (popups.error_text()), which starts the description. wait: return
    only once the window (and what it opens) has closed - for an error popup
    whose caller may close everything next."""
    log = log or (lambda message: None)
    owner = parent.winfo_toplevel()
    px = popups._px(parent)
    dialog = tk.Toplevel(owner)
    dialog.title(TITLE)
    dialog.resizable(False, False)
    if owner.winfo_viewable():
        dialog.transient(owner)
    done = tk.BooleanVar(owner, False)  # wait=True: set once nothing more is to come
    saving = {"now": False}  # a "Save to File Instead" outlives the window

    def finish():
        saving["now"] = False
        if dialog.winfo_exists():
            dialog.destroy()
        done.set(True)

    def on_destroy(event):
        # However the window goes (closed with its owner, too) - unless it
        # went for a save that's still to come.
        if event.widget is dialog and not saving["now"]:
            done.set(True)

    body = tk.Frame(dialog, padx=px(16), pady=px(12))
    body.pack(fill="both", expand=True)
    tk.Label(body, justify="left", wraplength=px(380),
             text="What happened? What were you doing when it went wrong?").pack(anchor="w")
    description_text = tk.Text(body, width=52, height=8, wrap="word")
    description_text.pack(anchor="w", fill="x", pady=(px(6), 0))
    if error:
        description_text.insert("1.0", starting_description(error))
        description_text.mark_set("insert", "end-1c")
    tk.Label(
        body, fg=dark_theme.MUTED, justify="left", wraplength=px(380),
        text=("Your description is posted publicly as a GitHub issue. ClipEm's and OBS's logs and "
              "a summary of this install (versions, settings and folder paths) are uploaded "
              "privately." + (" So is the error, exactly as shown." if error else "")),
    ).pack(anchor="w", pady=(px(6), 0))

    def set_sending(sending):
        state = "disabled" if sending else "normal"
        for child in dialog_buttons.winfo_children():
            child.config(state=state)
        description_text.config(state=state)
        send_button.config(text="Sending..." if sending else "Send")

    def on_save_instead():
        saving["now"] = True
        dialog.destroy()
        save_to_file()

    def on_send():
        description = description_text.get("1.0", "end").strip()
        if not description:
            popups.showwarning(TITLE, "Describe what happened first.", parent=dialog)
            return
        set_sending(True)
        _in_background(owner, lambda: bug_report.send_bug_report(description, error=error), on_sent)

    def on_sent(result, exc):
        if exc is not None:
            log(f"Sending the bug report failed: {exc}")
            if not dialog.winfo_exists():
                popups.showerror(TITLE, f"Couldn't send the bug report:\n{exc}", parent=owner, report=False)
                finish()
                return
            set_sending(False)
            if popups.askyesno(TITLE, f"Couldn't send the bug report:\n{exc}\n\nSave it to a file instead, to "
                               "attach it to an issue or a message yourself?", parent=dialog):
                on_save_instead()
            return
        report_id, issue_url = result
        log(f"Bug report sent: {report_id} ({issue_url})")
        if dialog.winfo_exists():
            dialog.destroy()
        if issue_url and popups.askyesno(
                TITLE, f"Thanks - the bug report was sent (report ID {report_id}).\n\n"
                "Open the GitHub issue in your browser?", parent=owner):
            webbrowser.open(issue_url)
        elif not issue_url:
            popups.showinfo(TITLE, f"Thanks - the bug report was sent (report ID {report_id}).", parent=owner)
        finish()

    def save_to_file():
        path = file_dialogs.asksaveasfilename(
            parent=owner, title="Save Bug Report", initialdir=bug_report.default_save_dir(),
            initialfile=bug_report.default_file_name(), defaultextension=".zip",
            filetypes=[("Zip files", "*.zip")],
        )
        if not path:
            finish()
            return
        _in_background(owner, lambda: bug_report.create_bug_report(path, error),
                       lambda count, exc: on_saved(path, count, exc))

    def on_saved(path, count, exc):
        if exc is not None:
            log(f"Bug report failed: {exc}")
            popups.showerror("Bug Report", f"Couldn't create the bug report:\n{exc}", parent=owner, report=False)
            finish()
            return
        log(f"Bug report saved: {path} ({count} log files)")
        if popups.askyesno(
                "Bug Report",
                f"Saved the bug report ({count} log files) to:\n{path}\n\n"
                "It holds ClipEm's and OBS's logs and a summary of this install (versions, settings and "
                "folder paths) - the websocket password is left out. Attach it to your bug report.\n\n"
                "Show it in its folder?", parent=owner):
            try:
                reveal_file(path)
            except OSError as exc:
                popups.showerror("Bug Report", f"Couldn't open the folder: {exc}", parent=owner, report=False)
        finish()

    dialog_buttons = tk.Frame(body)
    dialog_buttons.pack(side="bottom", anchor="e", pady=(px(12), 0))
    tk.Button(dialog_buttons, text="Cancel", command=finish, width=10).pack(side="left", padx=px(4))
    tk.Button(dialog_buttons, text="Save to File Instead...", command=on_save_instead).pack(side="left", padx=px(4))
    send_button = tk.Button(dialog_buttons, text="Send", command=on_send, width=10)
    send_button.pack(side="left", padx=px(4))
    dialog.bind("<Escape>", lambda _event: finish())
    dialog.protocol("WM_DELETE_WINDOW", finish)
    dialog.bind("<Destroy>", on_destroy, add=True)
    popups.place(dialog, owner)
    description_text.focus_set()
    dialog.grab_set()
    if wait:
        dialog.wait_variable(done)
    return dialog


def _arg(argv, name):
    if name in argv:
        index = argv.index(name)
        if index + 1 < len(argv):
            return argv[index + 1]
    return None


def main(argv):
    """The window on its own, for the clip editor's page (see the top)."""
    from app_window import style_app_window
    from ui_scale import UiScale, enable_dpi_awareness

    title, message = _arg(argv, "--title"), _arg(argv, "--message")
    enable_dpi_awareness()
    root = tk.Tk(className="ClipEm")
    root.withdraw()
    dark_theme.apply_theme(root)
    style_app_window(root)
    root.px = UiScale(root).px  # popups size themselves with it
    open_bug_report(root, popups.error_text(title, message) if message else None, log=print, wait=True)
    root.destroy()


if __name__ == "__main__":
    main(sys.argv[1:])
