"""The small always-on-top window for a review run: where you are, Skip, Stop.

A separate window rather than buttons on the page, so nothing is added to
LinkedIn's page and the dashboard does not need to be running.
"""

import tkinter as tk


def run_window(control):
    """Show the window until the run ends. Must be called on the main thread."""
    root = tk.Tk()
    root.title("Review run")
    root.attributes("-topmost", True)
    root.resizable(False, False)

    position = tk.StringVar()
    message = tk.StringVar()
    tk.Label(root, textvariable=position, font=("Segoe UI", 12, "bold")).pack(
        padx=12, pady=(10, 2))
    tk.Label(root, textvariable=message, wraplength=280, justify="center").pack(
        padx=12, pady=(0, 8))

    buttons = tk.Frame(root)
    buttons.pack(padx=12, pady=(0, 10))
    tk.Button(buttons, text="Skip this post", width=14,
              command=control.request_skip).pack(side="left", padx=4)
    tk.Button(buttons, text="Stop", width=8,
              command=control.request_stop).pack(side="left", padx=4)

    # Closing the window means stop. The run closes the browser and exits.
    root.protocol("WM_DELETE_WINDOW", control.request_stop)

    def refresh():
        if control.done.is_set():
            root.destroy()
            return
        where = f"{control.position} of {control.total}" if control.total else ""
        position.set(" - ".join(p for p in (where, control.author) if p))
        message.set(control.message)
        root.after(250, refresh)

    refresh()
    root.mainloop()
