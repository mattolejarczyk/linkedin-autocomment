"""The small always-on-top window for a review run: where you are, Skip, Stop.

A separate window rather than buttons on the page, so nothing is added to
LinkedIn's page and the dashboard does not need to be running.

Skip asks why. It only takes effect once a reason is chosen from the list, or Other
is chosen and a reason typed. The same panel takes the human's own words: a direction
on what should be different, or their own version of the comment, which "Use this"
puts in LinkedIn's box for them to read and click Comment.
"""

import gc
import tkinter as tk
from tkinter import ttk

from .review_run import (FEEDBACK_DIRECTION, FEEDBACK_MY_VERSION, OTHER_REASON,
                         SKIP_REASONS, skip_reason_problem)


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
        self.kind = tk.StringVar(self.root, value=FEEDBACK_DIRECTION)
        self.problem = tk.StringVar(self.root)

        tk.Label(self.root, textvariable=self.position,
                 font=("Segoe UI", 12, "bold")).pack(padx=12, pady=(10, 2))
        tk.Label(self.root, textvariable=self.message, wraplength=340,
                 justify="center").pack(padx=12, pady=(0, 8))

        self.buttons = tk.Frame(self.root)
        self.buttons.pack(padx=12, pady=(0, 10))
        tk.Button(self.buttons, text="Skip or give feedback", width=20,
                  command=self.open_skip_panel).pack(side="left", padx=4)
        tk.Button(self.buttons, text="I posted it", width=12,
                  command=control.say_posted).pack(side="left", padx=4)
        tk.Button(self.buttons, text="Stop", width=8,
                  command=control.request_stop).pack(side="left", padx=4)

        self.panel = tk.Frame(self.root)
        tk.Label(self.panel, text="Why are you skipping this post?").pack(anchor="w")
        self.dropdown = ttk.Combobox(self.panel, textvariable=self.reason, state="readonly",
                                     width=52, values=[label for _, label, _ in SKIP_REASONS])
        self.dropdown.pack(fill="x", pady=(2, 8))

        tk.Label(self.panel, text="Your words (optional, needed for Other):").pack(anchor="w")
        self.text = tk.Text(self.panel, width=52, height=7, wrap="word")
        self.text.pack(fill="x", pady=(2, 4))
        tk.Radiobutton(self.panel, text="A direction: what should be different",
                       variable=self.kind, value=FEEDBACK_DIRECTION).pack(anchor="w")
        tk.Radiobutton(self.panel, text="My own version of the comment",
                       variable=self.kind, value=FEEDBACK_MY_VERSION).pack(anchor="w")

        tk.Label(self.panel, textvariable=self.problem, fg="#b00020").pack(anchor="w")
        actions = tk.Frame(self.panel)
        actions.pack(fill="x", pady=(4, 0))
        tk.Button(actions, text="Skip", width=8,
                  command=self.confirm_skip).pack(side="left", padx=(0, 4))
        tk.Button(actions, text="Use this, don't skip", width=18,
                  command=self.use_this).pack(side="left", padx=(0, 4))
        tk.Button(actions, text="Cancel", width=8,
                  command=self.cancel_skip).pack(side="left")

        # Closing the window means stop. The run closes the browser and exits.
        self.root.protocol("WM_DELETE_WINDOW", control.request_stop)

    def _code_chosen(self):
        for code, label, _ in SKIP_REASONS:
            if label == self.reason.get():
                return code
        return None

    def words(self):
        """Exactly what was typed. "end-1c" drops only the newline Tk itself adds."""
        return self.text.get("1.0", "end-1c")

    def set_words(self, words):
        self.text.delete("1.0", "end")
        self.text.insert("1.0", words)

    def open_skip_panel(self):
        """Ask why. The post is not skipped until a reason is confirmed."""
        self.panel_open = True
        self.panel.pack(fill="x", padx=12, pady=(0, 10))

    def close_skip_panel(self):
        self.panel_open = False
        self.reason.set("")
        self.kind.set(FEEDBACK_DIRECTION)
        self.problem.set("")
        self.set_words("")
        self.panel.pack_forget()

    def confirm_skip(self):
        code = self._code_chosen()
        words = self.words()
        problem = skip_reason_problem(code, words)
        if problem:
            self.problem.set(problem)
            return
        self.control.give_feedback(self.kind.get(), words, code)
        self.control.request_skip(code, words if code == OTHER_REASON else None)
        self.close_skip_panel()

    def use_this(self):
        """Put their own version in LinkedIn's box. It does not skip or post."""
        words = self.words()
        if self.kind.get() != FEEDBACK_MY_VERSION:
            self.problem.set('Choose "My own version" to put your words in the box.')
            return
        if not words.strip():
            self.problem.set("Type your version first.")
            return
        self.control.give_feedback(FEEDBACK_MY_VERSION, words, self._code_chosen())
        self.control.request_use_text(words)
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
