#!/usr/bin/env python3
import re
from pathlib import Path

env_path = Path("/workspace/snowball/.env")

try:
    import tkinter as tk
    from tkinter import messagebox
except Exception as e:
    Path("/tmp/ytkey_gui_error.txt").write_text(f"tkinter failed: {e}\n")
    raise

# ensure line exists
text = env_path.read_text() if env_path.exists() else ""
if "YOUTUBE_API_KEY=" not in text:
    env_path.write_text(text.rstrip() + "\nYOUTUBE_API_KEY=\n")

root = tk.Tk()
root.title("Snowball YouTube API key")
root.geometry("560x200")
root.attributes("-topmost", True)
tk.Label(
    root,
    text="Paste your YouTube API key below.\nIt is saved only to .env on this computer — not to chat.",
    justify="left",
).pack(anchor="w", padx=12, pady=10)
entry = tk.Entry(root, width=68, show="*")
entry.pack(padx=12, fill="x")
entry.focus_set()
note = tk.Label(root, text="", fg="#0a0")
note.pack(pady=6)

def save(_event=None):
    key = entry.get().strip().strip('"').strip("'")
    if len(key) < 20:
        messagebox.showerror("Snowball", "Paste the full API key first.")
        return
    t = env_path.read_text() if env_path.exists() else ""
    if re.search(r"^YOUTUBE_API_KEY=.*$", t, re.M):
        t = re.sub(r"^YOUTUBE_API_KEY=.*$", f"YOUTUBE_API_KEY={key}", t, count=1, flags=re.M)
    else:
        t = t.rstrip() + f"\nYOUTUBE_API_KEY={key}\n"
    if "YOUTUBE_CHANNEL_HANDLES=" not in t:
        t += "YOUTUBE_CHANNEL_HANDLES=thetradingfraternity,thestockmarket\n"
    if "YOUTUBE_ALLOW_KEYWORD_SEARCH=" not in t:
        t += "YOUTUBE_ALLOW_KEYWORD_SEARCH=false\n"
    env_path.write_text(t)
    Path("/tmp/ytkey_saved.flag").write_text("ok\n")
    note.config(text="Saved. Hand the computer back to SnowBall.")
    messagebox.showinfo("Snowball", "Saved to .env. Hand the computer back now.")

tk.Button(root, text="Save to .env", command=save).pack(pady=8)
root.bind("<Return>", save)
root.mainloop()
