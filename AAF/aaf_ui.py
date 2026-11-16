# @noindex
"""External Tk interface. REAPER's embedded Python never creates a Tk window."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
import traceback

from aaf_core import read_sessions, materialize, matching_pair, write_json

from aaf_theme import (ALTERNATE, BORDER, MUTED, SUBTLE, TIMELINE,
                       apply_theme, track_indicators)

PALETTE = ("#a9abb3", "#82858e", "#999ca6", "#777b84", "#b6b8bf", "#8d929a")


def clock(seconds):
    sign = "−" if seconds < 0 else ""
    seconds = abs(seconds)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return "%s%02d:%02d:%06.3f" % (sign, h, m, s)


class ImportWindow:
    def __init__(self, root, args):
        self.root, self.args = root, args
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.sessions, self.session = [], None
        self.selected, self.pairs = set(), []
        self.media_roots = []
        self.future = None
        self.busy = False
        self.clip_boxes = {}
        self.root.title("AAF Station — Import")
        self.root.geometry("1280x820")
        self.root.minsize(1060, 710)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        apply_theme(self.root)
        self.track_indicators = track_indicators(self.root)
        self.build()

    def build(self):
        outer = ttk.Frame(self.root, padding=20)
        outer.pack(fill="both", expand=True)
        head = ttk.Frame(outer)
        head.pack(fill="x")
        ttk.Label(head, text="AAF Station", style="Title.TLabel").pack(side="left")
        self.path_label = ttk.Label(outer, text="Choose an AAF file to inspect its audio tracks.", style="Muted.TLabel")
        self.path_label.pack(fill="x", pady=(8, 14))

        source = ttk.Frame(outer)
        source.pack(fill="x")
        self.open_button = ttk.Button(source, text="Open AAF…", command=self.choose_file)
        self.open_button.pack(side="left")
        ttk.Label(source, text="  Composition ").pack(side="left")
        self.composition = ttk.Combobox(source, state="readonly", width=42)
        self.composition.pack(side="left", fill="x", expand=True)
        self.composition.bind("<<ComboboxSelected>>", lambda e: self.use_session())
        self.relink_button = ttk.Button(source, text="Locate media folder…", command=self.relink)
        self.relink_button.pack(side="right", padx=(10, 0))

        tools = ttk.Frame(outer)
        tools.pack(fill="x", pady=(14, 10))
        self.all_button = ttk.Button(tools, text="All", command=lambda: self.check_all(True))
        self.all_button.configure(width=4)
        self.all_button.pack(side="left")
        self.none_button = ttk.Button(tools, text="None", command=lambda: self.check_all(False))
        self.none_button.configure(width=4)
        self.none_button.pack(side="left", padx=5)
        self.pair_button = ttk.Button(tools, text="Pair selected → L / R", command=self.pair)
        self.pair_button.pack(side="left", padx=(10, 5))
        self.swap_button = ttk.Button(tools, text="Swap L/R", command=self.swap)
        self.swap_button.pack(side="left", padx=3)
        self.unpair_button = ttk.Button(tools, text="Unpair", command=self.unpair)
        self.unpair_button.pack(side="left", padx=3)
        self.channel_button = ttk.Button(tools, text="Source channel…", command=self.channel)
        self.channel_button.pack(side="left", padx=8)

        pane = ttk.Panedwindow(outer, orient="horizontal")
        pane.pack(fill="both", expand=True)
        table_frame = ttk.Frame(pane)
        timeline_frame = ttk.Frame(pane)
        pane.add(table_frame, weight=1)
        pane.add(timeline_frame, weight=2)
        self.tree = ttk.Treeview(table_frame, columns=("count", "channel", "pair", "state"),
                                 show=("tree", "headings"), selectmode="extended", height=5)
        self.tree.heading("#0", text="Track", anchor="w")
        self.tree.column("#0", width=185, minwidth=140, stretch=True)
        for col, title, width in (("count", "Clips", 50),
                                  ("channel", "Source", 65), ("pair", "Pair", 52), ("state", "Status", 70)):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, minwidth=width,
                             stretch=False, anchor="center")
        self.tree.pack(fill="both", expand=True)
        self.tree.tag_configure("odd", background="#303136")
        self.tree.tag_configure("error", foreground="#f2b5b5")
        self.tree.tag_configure("warn", foreground="#e6bf78")
        self.tree.bind("<Button-1>", self.table_click)
        self.tree.bind("<space>", self.space)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.draw())
        self.tree.bind("<MouseWheel>", self.mousewheel)
        ttk.Frame(table_frame, height=15).pack(fill="x")
        self.build_timeline(timeline_frame)
        zoom_row = ttk.Frame(outer)
        zoom_row.pack(fill="x", pady=(7, 0))
        ttk.Label(zoom_row, text="First selected row = L; second = R.  ⌘-click two rows to pair.", style="Muted.TLabel").pack(side="left")
        ttk.Label(zoom_row, text="Zoom", style="Muted.TLabel").pack(side="right")
        self.zoom = tk.DoubleVar(value=1)
        self.scale = ttk.Scale(zoom_row, from_=1, to=16, variable=self.zoom, command=lambda v: self.draw())
        self.scale.pack(side="right", padx=10)
        self.tc_ruler = tk.BooleanVar(value=True)
        ttk.Checkbutton(zoom_row, text="TC ruler", variable=self.tc_ruler, command=self.draw).pack(side="right", padx=10)
        self.detail = tk.StringVar(value="Clip rectangles show timing and length; waveforms are not rendered.")
        ttk.Label(outer, textvariable=self.detail, style="Muted.TLabel", wraplength=1180).pack(fill="x", pady=(8, 10))

        options = ttk.Frame(outer)
        options.pack(fill="x")
        ttk.Label(options, text="Placement ").grid(row=0, column=0, sticky="w")
        self.placement = ttk.Combobox(options, state="readonly", width=30,
                                     values=("Session timeline from 0", "First selected clip at cursor", "AAF timecode → project timecode"))
        self.placement.current(0)
        self.placement.grid(row=0, column=1, sticky="w")
        ttk.Label(options, text="  Additional offset (s) ").grid(row=0, column=2)
        self.offset = tk.StringVar(value="0")
        ttk.Entry(options, textvariable=self.offset, width=10).grid(row=0, column=3)
        self.copy_linked = tk.BooleanVar(value=True)
        ttk.Checkbutton(options, text="Copy linked media", variable=self.copy_linked).grid(row=0, column=4, padx=16)
        self.allow_dry = tk.BooleanVar(value=False)
        ttk.Checkbutton(options, text="Import dry audio despite effect/automation warnings",
                        variable=self.allow_dry).grid(row=1, column=0, columnspan=5, sticky="w", pady=(8, 0))
        target = ttk.Frame(outer)
        target.pack(fill="x", pady=(8, 10))
        ttk.Label(target, text="Media folder ").pack(side="left")
        self.folder = tk.StringVar()
        ttk.Entry(target, textvariable=self.folder).pack(side="left", fill="x", expand=True)
        self.folder_button = ttk.Button(target, text="Choose…", command=self.choose_folder)
        self.folder_button.pack(side="right", padx=(8, 0))
        footer = ttk.Frame(outer)
        footer.pack(fill="x")
        self.summary = tk.StringVar(value="No session loaded")
        ttk.Label(footer, textvariable=self.summary).pack(side="left")
        self.cancel_button = ttk.Button(footer, text="Cancel", command=self.close)
        self.cancel_button.pack(side="right", padx=(8, 0))
        title = "Import selected tracks" if self.args.result else "Save import plan…"
        self.import_button = ttk.Button(footer, text=title, style="Accent.TButton", command=self.prepare)
        self.import_button.pack(side="right")
        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        self.progress.pack(fill="x", pady=(10, 0))
        self.mutation_buttons = [self.open_button, self.relink_button, self.pair_button, self.swap_button,
                                 self.unpair_button, self.channel_button, self.all_button, self.none_button,
                                 self.import_button, self.folder_button]

    def build_timeline(self, timeline_frame):
        timeline_frame.rowconfigure(1, weight=1)
        timeline_frame.columnconfigure(0, weight=1)
        self.ruler = tk.Canvas(timeline_frame, height=29, bg=TIMELINE, highlightthickness=0)
        self.ruler.grid(row=0, column=0, sticky="ew")
        self.canvas = tk.Canvas(timeline_frame, bg=TIMELINE, highlightthickness=0)
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.analysis_label = tk.Label(self.canvas, text="Analyzing…", bg=TIMELINE,
                                       fg=MUTED, font=("Helvetica", 15))
        self.canvas.bind("<Configure>", lambda e: self.draw())
        self.canvas.bind("<Motion>", self.hover)
        self.canvas.bind("<Button-1>", self.timeline_click)
        self.canvas.bind("<MouseWheel>", self.mousewheel)
        self.scroll = ttk.Scrollbar(timeline_frame, orient="vertical", command=self.scroll_y)
        self.scroll.grid(row=1, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.xscroll = ttk.Scrollbar(timeline_frame, orient="horizontal", command=self.scroll_x)
        self.xscroll.grid(row=2, column=0, sticky="ew")
        self.canvas.configure(xscrollcommand=self.set_xscroll)

    def run_task(self, function, callback, *, analyzing=False):
        if self.busy:
            return
        self.busy = True
        for button in self.mutation_buttons:
            button.configure(state="disabled")
        self.composition.configure(state="disabled")
        self.progress.start(12)
        def poll():
            if not self.future.done():
                self.root.after(100, poll)
                return
            self.busy = False
            self.progress.stop()
            if analyzing:
                self.analysis_label.place_forget()
            for button in self.mutation_buttons:
                button.configure(state="normal")
            self.composition.configure(state="readonly")
            try:
                result = self.future.result()
                callback(result)
            except Exception as exc:
                traceback.print_exc()
                self.summary.set("Operation stopped — " + str(exc).splitlines()[0])
                messagebox.showerror("AAF import stopped", str(exc), parent=self.root)
        def start():
            self.future = self.pool.submit(function)
            self.root.after(100, poll)

        if analyzing:
            self.analysis_label.place(relx=0.5, rely=0.5, anchor="center")
            # Give Tk a turn to lay out and paint the loading state first.
            self.root.after_idle(lambda: self.root.after(0, start))
        else:
            start()

    def choose_file(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(parent=self.root, title="Open AAF",
                                          filetypes=[("AAF files", "*.aaf *.AAF"), ("All files", "*")])
        if path:
            self.media_roots = []
            self.load(path)

    def load(self, path):
        if self.busy:
            return
        path = Path(path).absolute()
        self.path_label.configure(text=str(path))
        self.summary.set("Reading AAF edit structure and channel references…")
        self.folder.set(str(path.with_suffix("")) + "_REAPER_Media")
        self.sessions, self.session = [], None
        self.composition.set("")
        self.composition["values"] = ()
        self.refresh()
        roots = list(self.media_roots)
        def loaded(sessions):
            self.sessions = sessions
            self.composition["values"] = ["%s  (%d tracks)" % (s.name, len(s.tracks)) for s in sessions]
            self.composition.current(0)
            self.use_session()
        self.run_task(lambda: read_sessions(path, roots), loaded, analyzing=True)

    def use_session(self):
        if self.busy or not self.sessions:
            return
        self.session = self.sessions[self.composition.current()]
        self.selected = {t.id for t in self.session.tracks if not t.errors}
        self.pairs = []
        self.canvas.yview_moveto(0)
        self.refresh()

    def refresh(self):
        focus = set(self.tree.selection())
        for row in self.tree.get_children():
            self.tree.delete(row)
        if not self.session:
            self.draw()
            return
        for index, track in enumerate(self.session.tracks):
            assets = [self.session.assets[c.asset_key] for clip in track.clips
                      for c in (clip, clip.stereo_right) if c is not None]
            missing = any(a.problem for a in assets)
            channels = {"L/R" if c.stereo_right is not None else "all" if c.channel is None
                        else str(c.channel + 1) for c in track.clips}
            source = "/".join(sorted(channels)) or "—"
            pair = "—"
            for i, (left, right) in enumerate(self.pairs, 1):
                if track.id in (left, right):
                    pair = "%d %s" % (i, "L" if track.id == left else "R")
            status = "Blocked" if track.errors else "Offline" if missing else "Review" if track.warnings else "Ready"
            tags = ("error",) if track.errors or missing else ("warn",) if track.warnings else ()
            self.tree.insert("", "end", iid=track.id, text="  " + track.name,
                             image=self.track_indicators[track.id in self.selected],
                             values=(len(track.clips), source, pair, status), tags=tags + (("odd",) if index % 2 else ()))
        self.tree.selection_set([i for i in focus if self.tree.exists(i)])
        tracks = [t for t in self.session.tracks if t.id in self.selected]
        count = len(tracks) - sum(1 for l, r in self.pairs if l in self.selected and r in self.selected)
        self.summary.set("%d / %d tracks selected  •  %d output tracks  •  %d clips" %
                         (len(tracks), len(self.session.tracks), count, sum(len(t.clips) for t in tracks)))
        self.draw()

    def table_click(self, event):
        if self.busy:
            return "break"
        if self.tree.identify_column(event.x) == "#0" and event.x <= 38:
            row = self.tree.identify_row(event.y)
            if row:
                self.toggle(row)
            return "break"

    def toggle(self, row):
        ids = {row}
        for left, right in self.pairs:
            if row in (left, right):
                ids = {left, right}
        if row in self.selected:
            self.selected.difference_update(ids)
        else:
            self.selected.update(ids)
        self.refresh()

    def space(self, event):
        if not self.busy:
            rows = self.tree.selection()
            if rows:
                self.toggle(rows[0])
        return "break"

    def check_all(self, value):
        if self.session and not self.busy:
            self.selected = {t.id for t in self.session.tracks if not t.errors} if value else set()
            self.refresh()

    def pair(self):
        if not self.session or self.busy:
            return
        rows = set(self.tree.selection())
        tracks = [t for t in self.session.tracks if t.id in rows]
        if len(tracks) != 2:
            messagebox.showinfo("Stereo pair", "Select exactly two rows with ⌘-click.\nTop row = left; bottom row = right.", parent=self.root)
            return
        left, right = tracks
        problem = matching_pair(left, right)
        if problem:
            messagebox.showerror("Cannot pair L/R", problem, parent=self.root)
            return
        self.pairs = [p for p in self.pairs if not rows.intersection(p)]
        self.pairs.append((left.id, right.id))
        self.selected.update(rows)
        self.refresh()
        self.detail.set("Stereo pair: %s → L, %s → R. One interleaved stereo WAV per source alignment." % (left.name, right.name))

    def swap(self):
        if self.busy:
            return
        rows = set(self.tree.selection())
        self.pairs = [(r, l) if rows.intersection((l, r)) else (l, r) for l, r in self.pairs]
        self.refresh()

    def unpair(self):
        if self.busy:
            return
        rows = set(self.tree.selection())
        self.pairs = [p for p in self.pairs if not rows.intersection(p)]
        self.refresh()

    def channel(self):
        if self.busy or not self.session:
            return
        rows = self.tree.selection()
        if len(rows) != 1:
            messagebox.showinfo("Source channel", "Select one track row to override its PCM source channel.", parent=self.root)
            return
        track = next(t for t in self.session.tracks if t.id == rows[0])
        if any(c.stereo_right is not None for c in track.clips):
            messagebox.showinfo("Source channel", "This track already has L/R channels from the AAF.", parent=self.root)
            return
        value = simpledialog.askinteger("Explicit source channel", "PCM channel number (1 = first, 2 = second).\nApplies to all clips on: " + track.name +
                                        "\nReload the AAF to restore automatic channel selection.",
                                        minvalue=1, maxvalue=64, parent=self.root)
        if value is None:
            return
        if any(self.session.assets[c.asset_key].channels and value > self.session.assets[c.asset_key].channels for c in track.clips):
            messagebox.showerror("Source channel", "At least one source has fewer channels than requested.", parent=self.root)
            return
        for clip in track.clips:
            clip.channel = value - 1
        self.refresh()

    def relink(self):
        if self.busy or not self.session:
            return
        path = filedialog.askdirectory(parent=self.root, title="Locate Pro Tools Audio Files folder")
        if path:
            self.media_roots.append(path)
            self.load(self.session.path)

    def choose_folder(self):
        path = filedialog.askdirectory(parent=self.root, title="Destination for prepared media")
        if path:
            self.folder.set(path)

    def scroll_y(self, *args):
        self.canvas.yview(*args)
        self.tree.yview(*args)

    def scroll_x(self, *args):
        self.canvas.xview(*args)
        self.ruler.xview(*args)

    def set_xscroll(self, first, last):
        self.xscroll.set(first, last)
        self.ruler.xview_moveto(first)

    def mousewheel(self, event):
        delta = -1 if event.delta > 0 else 1
        self.scroll_y("scroll", delta, "units")
        return "break"

    def preview_fades(self, clip):
        return clip.fade_in, clip.fade_out

    def preview_selected(self, track):
        return track.id in self.selected

    def timeline_bounds(self, clips):
        return (min((clip.position for clip in clips), default=0),
                max((clip.position + clip.duration for clip in clips), default=1))

    def draw(self):
        c = self.canvas
        if not c.winfo_exists() or not self.ruler.winfo_exists():
            return
        c.delete("all")
        self.ruler.delete("all")
        self.clip_boxes = {}
        if not self.session:
            return
        clips = [cl for t in self.session.tracks for cl in t.clips]
        first, last = self.timeline_bounds(clips)
        span = max(1, last - first)
        width = max(280, c.winfo_width() - 8) * self.zoom.get()
        scale = max(1e-9, (width - 24) / span)
        height = max(c.winfo_height(), len(self.session.tracks) * 34)
        c.configure(scrollregion=(0, 0, width, height), yscrollincrement=34)
        self.ruler.configure(scrollregion=(0, 0, width, 29))
        base = self.session.timecode_zero if self.tc_ruler.get() and self.session.timecode_zero is not None else 0
        step_raw = span / max(2, width / 110)
        magnitude = 10 ** math.floor(math.log10(max(step_raw, 0.001)))
        step = next(v * magnitude for v in (1, 2, 5, 10) if v * magnitude >= step_raw)
        tick = math.ceil((first + base) / step) * step
        while tick <= last + base:
            x = 12 + (tick - base - first) * scale
            self.ruler.create_line(x, 25, x, 29, fill=BORDER)
            self.ruler.create_text(x + 3, 13, text=clock(tick), fill=SUBTLE, anchor="w", font=("Helvetica", 9))
            tick += step
        focused = set(self.tree.selection())
        for i, track in enumerate(self.session.tracks):
            top = i * 34
            c.create_rectangle(0, top, width, top + 34, fill=ALTERNATE if i % 2 == 0 else TIMELINE, outline="")
            color = PALETTE[i % len(PALETTE)]
            for l, r in self.pairs:
                if track.id == r:
                    li = next(j for j, t in enumerate(self.session.tracks) if t.id == l)
                    color = PALETTE[li % len(PALETTE)]
            if not self.preview_selected(track):
                color = "#55575d"
            for clip in track.clips:
                x1 = 12 + (clip.position - first) * scale
                x2 = max(x1 + 2, x1 + clip.duration * scale)
                box = c.create_rectangle(x1, top + 4, x2, top + 29, fill=color,
                                         outline="#e2e4e8" if track.id in focused else color)
                self.clip_boxes[box] = (track, clip)
                if x2 - x1 > 55:
                    max_chars = int((x2 - x1 - 10) / 6.4)
                    label = clip.name[:max_chars]
                    text_id = c.create_text(x1 + 5, top + 16, text=label, fill="#1b1c1f", anchor="w", font=("Helvetica", 10))
                    self.clip_boxes[text_id] = (track, clip)
                fade_in, fade_out = self.preview_fades(clip)
                if fade_in:
                    c.create_line(x1, top + 28, min(x2, x1 + fade_in * scale), top + 4, fill="#dedfe3")
                if fade_out:
                    c.create_line(max(x1, x2 - fade_out * scale), top + 4, x2, top + 28, fill="#dedfe3")

    def at_pointer(self, event):
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        for item in reversed(self.canvas.find_overlapping(x, y, x, y)):
            if item in self.clip_boxes:
                return self.clip_boxes[item]
        return None

    def hover(self, event):
        found = self.at_pointer(event)
        if found:
            track, clip = found
            channel = "stereo L/R" if clip.stereo_right is not None else (
                "all channels" if clip.channel is None else "source ch %d" % (clip.channel + 1))
            self.detail.set("%s  •  %s  •  %s + %.3fs  •  source offset %.6fs  •  %s" %
                            (track.name, clip.name, clock(clip.position), clip.duration, clip.offset, channel))

    def timeline_click(self, event):
        if not self.busy:
            found = self.at_pointer(event)
            if found:
                self.tree.selection_set(found[0].id)

    def prepare(self):
        if self.busy or not self.session:
            return
        try:
            addition = float(self.offset.get().replace(",", "."))
            if not math.isfinite(addition):
                raise ValueError("Offset must be finite")
            if not self.folder.get().strip():
                raise ValueError("Choose a destination media folder")
            selected = set(self.selected)
            pairs = list(self.pairs)
            session = self.session
            clips = [c for t in session.tracks if t.id in selected for c in t.clips]
            if not clips:
                raise ValueError("Select at least one track containing audio clips")
            mode = self.placement.current()
            shift = 0 if mode == 0 else self.args.cursor - min(c.position for c in clips)
            if mode == 2:
                if session.timecode_zero is None:
                    raise ValueError("AAF has no readable timecode track")
                shift = session.timecode_zero - self.args.project_offset
            shift += addition
            if min(c.position + shift for c in clips) < -1e-9:
                raise ValueError("Placement produces negative REAPER positions. Choose cursor placement or add an offset.")
            result_path = self.args.result
            if not result_path:
                result_path = filedialog.asksaveasfilename(parent=self.root, title="Save REAPER import plan (developer mode)",
                                                         defaultextension=".json", initialfile="AAF_import_plan.json")
                if not result_path:
                    return
            folder = self.folder.get()
            dry, copy = self.allow_dry.get(), self.copy_linked.get()
            self.summary.set("Preparing selected PCM media and validating L/R channels…")
            def work():
                plan = materialize(session, selected, pairs, folder, dry, copy)
                for track in plan["tracks"]:
                    for clip in track["clips"]:
                        clip["position"] = max(0, clip["position"] + shift)
                plan["placement_shift"] = shift
                write_json(result_path, {"status": "ready", "plan": plan})
                return plan
            def complete(plan):
                if self.args.result:
                    self.pool.shutdown(wait=False)
                    self.root.destroy()
                else:
                    self.summary.set("Import plan saved. To import into REAPER, run REAPER_AAF_Import.py from Actions.")
                    messagebox.showinfo("Plan saved", "Media and import plan prepared.\nFor direct import, load REAPER_AAF_Import.py in REAPER Actions.", parent=self.root)
            self.run_task(work, complete)
        except Exception as exc:
            messagebox.showerror("AAF import", str(exc), parent=self.root)

    def close(self):
        if self.busy:
            stage = "Analyzing AAF" if self.analysis_label.winfo_manager() else "Preparing media"
            self.summary.set(stage + ". To terminate immediately, stop the deferred AAF script in REAPER.")
            return
        if self.args.result:
            write_json(self.args.result, {"status": "cancelled"})
        self.pool.shutdown(wait=False)
        self.root.destroy()


def present_window(root):
    root.attributes("-topmost", True)
    root.lift()
    root.focus_force()
    # Restore normal stacking once macOS has activated the window.
    root.after(350, root.attributes, "-topmost", False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file")
    parser.add_argument("--result")
    parser.add_argument("--cursor", type=float, default=0)
    parser.add_argument("--project-offset", type=float, default=0)
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    if args.probe:
        if sys.version_info < (3, 9):
            raise RuntimeError("Python 3.9 or newer is required")
        print(json.dumps({"python": sys.executable, "version": sys.version.split()[0], "tk": tk.TkVersion}))
        return
    try:
        root = tk.Tk()
        window = ImportWindow(root, args)
        if args.result:
            root.after_idle(present_window, root)
        if args.file:
            root.after_idle(window.load, args.file)
        root.mainloop()
    except Exception as exc:
        traceback.print_exc()
        if args.result:
            write_json(args.result, {"status": "error", "message": str(exc)})
        raise


if __name__ == "__main__":
    main()
