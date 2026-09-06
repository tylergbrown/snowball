#!/usr/bin/env python3
"""Secure local paste of Coinbase CDP keys into ~/snowball/.env — never chat."""
from __future__ import annotations

import re
from pathlib import Path

ENV_PATH = Path("/home/tb/snowball/.env")
FLAG = Path("/tmp/coinbase_keys_saved.flag")

try:
    import tkinter as tk
    from tkinter import messagebox, scrolledtext
except Exception as e:
    Path("/tmp/coinbase_key_gui_error.txt").write_text(f"tkinter failed: {e}\n")
    raise


def upsert(text: str, key: str, value: str) -> str:
    line = f"{key}={value}"
    if re.search(rf"^{re.escape(key)}=.*$", text, re.M):
        return re.sub(rf"^{re.escape(key)}=.*$", line, text, count=1, flags=re.M)
    return text.rstrip() + "\n" + line + "\n"


def normalize_pem(raw: str) -> str:
    s = raw.strip().strip('"').strip("'")
    # Allow pasted PEM with real newlines → store as single-line \n escapes for .env
    if "BEGIN" in s and "\\n" not in s and "\n" in s:
        s = s.replace("\r\n", "\n").replace("\r", "\n")
        s = s.replace("\n", "\\n")
    return s


root = tk.Tk()
root.title("Snowball Coinbase CDP keys")
root.geometry("720x520")
root.attributes("-topmost", True)

tk.Label(
    root,
    text=(
        "Paste CDP Secret API key details below.\n"
        "Key name looks like: organizations/.../apiKeys/...\n"
        "Private key is the PEM (-----BEGIN ... PRIVATE KEY-----).\n"
        "Saved only to /home/tb/snowball/.env — not to chat.\n"
        "Use ECDSA when creating the key. Trade yes, transfer/withdraw NO."
    ),
    justify="left",
).pack(anchor="w", padx=12, pady=10)

tk.Label(root, text="API key name (apiKey):").pack(anchor="w", padx=12)
name_entry = tk.Entry(root, width=90, show="*")
name_entry.pack(padx=12, fill="x")

tk.Label(root, text="Private key PEM (secret) — paste full block:").pack(anchor="w", padx=12, pady=(8, 0))
pem_box = scrolledtext.ScrolledText(root, width=90, height=12, show=None)
pem_box.pack(padx=12, fill="both", expand=True)

tk.Label(root, text="Passphrase (legacy only — leave blank for CDP):").pack(anchor="w", padx=12, pady=(8, 0))
pass_entry = tk.Entry(root, width=90, show="*")
pass_entry.pack(padx=12, fill="x")

note = tk.Label(root, text="", fg="#0a0")
note.pack(pady=6)


def save(_event=None):
    name = name_entry.get().strip().strip('"').strip("'")
    pem = normalize_pem(pem_box.get("1.0", "end"))
    passphrase = pass_entry.get().strip()
    if "organizations/" not in name and len(name) < 20:
        messagebox.showerror(
            "Snowball",
            "Paste the full CDP key name (usually starts with organizations/).",
        )
        return
    if "BEGIN" not in pem or "PRIVATE KEY" not in pem:
        messagebox.showerror("Snowball", "Paste the full private key PEM block.")
        return
    t = ENV_PATH.read_text() if ENV_PATH.exists() else ""
    t = upsert(t, "COINBASE_API_KEY", name)
    t = upsert(t, "COINBASE_API_SECRET", pem)
    t = upsert(t, "COINBASE_API_PASSPHRASE", passphrase)
    # Keep paper until SnowBall flips live after a dry check
    if "MODE=" not in t:
        t = upsert(t, "MODE", "paper")
    if "LIVE_ENABLED=" not in t:
        t = upsert(t, "LIVE_ENABLED", "false")
    ENV_PATH.write_text(t)
    FLAG.write_text("ok\n")
    note.config(text="Saved. Hand the computer back to SnowBall — still paper until we flip live.")
    messagebox.showinfo(
        "Snowball",
        "Saved to .env. Still MODE=paper. Hand the computer back now.",
    )


tk.Button(root, text="Save to .env", command=save).pack(pady=10)
name_entry.focus_set()
root.mainloop()
