# @noindex
"""Shared flat Tk styling for the AAF import and export windows."""
import base64
import struct
import tkinter as tk
from tkinter import ttk
import zlib


BG = "#202124"
PANEL = "#292a2e"
TIMELINE = "#1b1c1f"
ALTERNATE = "#242528"
CONTROL = "#303135"
HOVER = "#3a3c41"
BORDER = "#414348"
TEXT = "#ececef"
MUTED = "#abaeb6"
SUBTLE = "#848891"
FOCUS = "#454851"


def apply_theme(root):
    root.configure(background=BG)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=BG, foreground=TEXT, font=("Helvetica", 11))
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=TEXT, font=("Helvetica", 11))
    style.configure("Title.TLabel", foreground=TEXT, font=("Helvetica", 22, "bold"))
    style.configure("Muted.TLabel", foreground=MUTED)
    style.configure("TButton", padding=(11, 7), background=CONTROL, foreground=TEXT,
                    borderwidth=1, bordercolor=CONTROL, lightcolor=CONTROL, darkcolor=CONTROL, relief="flat")
    style.map("TButton", background=[("pressed", BORDER), ("active", HOVER)],
              foreground=[("disabled", SUBTLE)], bordercolor=[("focus", SUBTLE)])
    style.configure("Accent.TButton", background="#dedfe3", foreground="#202124",
                    bordercolor="#dedfe3", lightcolor="#dedfe3", darkcolor="#dedfe3", font=("Helvetica", 12, "bold"))
    style.map("Accent.TButton", background=[("disabled", CONTROL), ("pressed", "#c8c9ce"), ("active", "#f4f4f5")],
              foreground=[("disabled", SUBTLE)])
    style.configure("TCheckbutton", background=BG, foreground=TEXT, indicatorbackground=CONTROL,
                    bordercolor=BORDER, lightcolor=CONTROL, darkcolor=CONTROL, padding=(0, 2))
    style.map("TCheckbutton", background=[("active", BG)], indicatorbackground=[("selected", "#b4b6bd")])
    style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                    bordercolor=PANEL, lightcolor=PANEL, darkcolor=PANEL, relief="flat",
                    borderwidth=0, rowheight=34, indent=0, font=("Helvetica", 11))
    style.map("Treeview", background=[("selected", FOCUS)], foreground=[("selected", TEXT)])
    style.configure("Treeview.Heading", background=CONTROL, foreground=MUTED, padding=(8, 6),
                    bordercolor=CONTROL, lightcolor=CONTROL, darkcolor=CONTROL, relief="flat")
    style.map("Treeview.Heading", background=[("active", CONTROL)])
    style.configure("TPanedwindow", background=BG, sashwidth=6, sashrelief="flat")
    style.layout("Horizontal.Sash", [("Sash.hsash", {"sticky": "nswe"})])
    style.layout("Vertical.Sash", [("Sash.vsash", {"sticky": "nswe"})])
    style.configure("Sash", sashthickness=5, sashpad=1, relief="flat")
    style.configure("TEntry", fieldbackground=CONTROL, foreground=TEXT, insertcolor=TEXT,
                    borderwidth=1, lightcolor=CONTROL, darkcolor=CONTROL, bordercolor=BORDER, padding=5)
    style.configure("TCombobox", background=CONTROL, fieldbackground=CONTROL, foreground=TEXT,
                    arrowcolor=MUTED, arrowsize=10, borderwidth=1, relief="flat", padding=(6, 5),
                    lightcolor=CONTROL, darkcolor=CONTROL, bordercolor=BORDER)
    style.map("TCombobox", fieldbackground=[("readonly", CONTROL), ("disabled", PANEL)],
              foreground=[("disabled", SUBTLE), ("readonly", TEXT)],
              background=[("active", HOVER)], bordercolor=[("focus", SUBTLE)],
              selectbackground=[("readonly", CONTROL)], selectforeground=[("readonly", TEXT)])
    root.option_add("*TCombobox*Listbox.background", CONTROL)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", FOCUS)
    root.option_add("*TCombobox*Listbox.selectForeground", TEXT)
    root.option_add("*TCombobox*Listbox.borderWidth", 0)
    root.option_add("*TCombobox*Listbox.highlightThickness", 0)
    root.option_add("*TCombobox*Listbox.font", ("Helvetica", 11))
    for direction in ("Vertical", "Horizontal"):
        layout = direction + ".TScrollbar"
        style.layout(layout, [(direction + ".Scrollbar.trough", {"sticky": "nswe", "children": [
            (direction + ".Scrollbar.thumb", {"expand": True, "sticky": "nswe"})]})])
        style.configure(layout, background="#62646a", troughcolor=TIMELINE, bordercolor=TIMELINE,
                        lightcolor="#62646a", darkcolor="#62646a", relief="flat", borderwidth=0,
                        gripcount=0, arrowsize=8, width=8, troughrelief="flat")
        style.map(layout, background=[("pressed", "#a3a6ad"), ("active", "#84868e")])
    style.configure("Horizontal.TScale", background="#777981", troughcolor=CONTROL,
                    lightcolor="#777981", darkcolor="#777981", bordercolor=CONTROL,
                    sliderrelief="flat", sliderlength=14)
    style.configure("Horizontal.TProgressbar", background="#aaaeb8", troughcolor=CONTROL,
                    bordercolor=CONTROL, lightcolor="#aaaeb8", darkcolor="#aaaeb8", thickness=4)


def track_indicators(master):
    """Return transparent antialiased circle icons at the current display scale."""
    size = max(24, round(master.winfo_fpixels("24p") * 0.85))
    center = (size - 1) / 2

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    def icon(selected):
        pixels = bytearray()
        for y in range(size):
            pixels.append(0)
            for x in range(size):
                distance = ((x - center) ** 2 + (y - center) ** 2) ** 0.5
                alpha = round(255 * max(0, min(1, center - 0.6 - distance)))
                light = max(0, min(1, center * 0.51 - distance + 0.5)) if selected else 0
                rim = max(0, min(1, distance - (center - 2)))
                shade = round(23 + 64 * rim + 185 * light)
                pixels.extend((shade, shade, shade + 2, alpha))
        header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
        png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
        png += chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")
        return tk.PhotoImage(master=master, data=base64.b64encode(png))

    return {False: icon(False), True: icon(True)}
