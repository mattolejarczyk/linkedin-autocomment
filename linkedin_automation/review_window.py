"""The small always-on-top window for a review run: where you are, Skip, Stop.

A separate window rather than buttons on the page, so nothing is added to
LinkedIn's page and the dashboard does not need to be running.

Skip asks why. It only takes effect once a reason is chosen from the list, or Other
is chosen and a reason typed.
"""

import gc
import tkinter as tk
from tkinter import ttk

from .review_run import OTHER_REASON, SKIP_REASONS, skip_reason_problem


class ReviewWindow:
    def __init__(self, control):
        self.control = control
        self.panel_open = False
        self.root = tk.Tk()
        self.root.title("Review run")
        self.root.attributes("-topmost", True)
        self.root.resizable(False, False)

        self.position = tk.StringVar(self.root)
        self.message = tk.StringVar(self.root)
        self.reason = tk.StringVar(self.root)
        self.note = tk.StringVar(self.root)
        self.problem = tk.StringVar(self.root)

        tk.Label(self.root, textvariable=self.position,
                 font=("Segoe UI", 12, "bold")).pack(padx=12, pady=(10, 2))
        tk.Label(self.root, textvariable=self.message, wraplength=300,
                 justify="center").pack(padx=12, pady=(0, 8))

        self.buttons = tk.Frame(self.root)
        self.buttons.pack(padx=12, pady=(0, 10))
        tk.Button(self.buttons, text="Skip this post", width=14,
                  command=self.open_skip_panel).pack(side="left", padx=4)
        tk.Button(self.buttons, text="Stop", width=8,
                  command=control.request_stop).pack(side="left", padx=4)

        self.panel = tk.Frame(self.root)
        tk.Label(self.panel, text="Why are you skipping this post?").pack(anchor="w")
        self.dropdown = ttk.Combobox(self.panel, textvariable=self.reason, state="readonly",
                                     width=46, values=[label for _, label, _ in SKIP_REASONS])
        self.dropdown.pack(fill="x", pady=(2, 6))
        self.dropdown.bind("<<ComboboxSelected>>", lambda event: self._show_note())

        self.note_box = tk.Frame(self.panel)
        tk.Label(self.note_box, text="Your reason:").pack(anchor="w")
        tk.Entry(self.note_box, textvariable=self.note).pack(fill="x", pady=(2, 6))

        self.panel_end = tk.Frame(self.panel)
        self.panel_end.pack(fill="x")
        tk.Label(self.panel_end, textvariable=self.problem, fg="#b00020").pack(anchor="w")
        tk.Button(self.panel_end, text="Skip", width=10,
                  command=self.confirm_skip).pack(side="left", padx=(0, 4), pady=(4, 0))
        tk.Button(self.panel_end, text="Cancel", width=10,
                  command=self.cancel_skip).pack(side="left", pady=(4, 0))

        # Closing the window means stop. The run closes the browser and exits.
        self.root.protocol("WM_DELETE_WINDOW", control.request_stop)

    def _code_chosen(self):
        for code, label, _ in SKIP_REASONS:
            if label == self.reason.get():
                return code
        return None

    def _show_note(self):
        if self._code_chosen() == OTHER_REASON:
            self.note_box.pack(fill="x", before=self.panel_end)
        else:
            self.note_box.pack_forget()

    def open_skip_panel(self):
        """Ask why. The post is not skipped until a reason is confirmed."""
        self.panel_open = True
        self.panel.pack(fill="x", padx=12, pady=(0, 10))

    def close_skip_panel(self):
        self.panel_open = False
        self.reason.set("")
        self.note.set("")
        self.problem.set("")
        self.note_box.pack_forget()
        self.panel.pack_forget()

    def confirm_skip(self):
        code = self._code_chosen()
        problem = skip_reason_problem(code, self.note.get())
        if problem:
            self.problem.set(problem)
            return
        self.control.request_skip(code, self.note.get() if code == OTHER_REASON else None)
        self.close_skip_panel()

    def cancel_skip(self):
        self.close_skip_panel()

    def refresh(self):
        if self.control.done.is_set():
            self.root.destroy()
            return
        control = self.control
        where = f"{control.position} of {control.total}" if control.total else ""
        self.position.set(" - ".join(p for p in (where, control.author) if p))
        self.message.set(control.message)
        self.root.after(250, self.refresh)


def run_window(control):
    """Show the window until the run ends. Must be called on the main thread."""
    window = ReviewWindow(control)
    window.refresh()
    window.root.mainloop()
    # Collect the Tk objects here. Left for the garbage collector, they could be
    # freed on the worker thread, and Tk aborts the process if that happens.
    del window
    gc.collect()
