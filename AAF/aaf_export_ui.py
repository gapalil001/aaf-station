# @noindex
"""AAF Station export window."""
import argparse
import json
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from aaf_export import plan_export, safe_name, write_aaf
from aaf_theme import PANEL, TEXT
from aaf_ui import ImportWindow, clock, present_window


class ExportWindow(ImportWindow):
    def __init__(self, root, project):
        self.project = project
        self.available_tracks = [track for track in project["tracks"] if track["clips"]]
        self.reaper_selection = {track["id"] for track in self.available_tracks if track["selected"]}
        end = max([project["length"]] + [c["position"] + c["duration"]
                                        for t in self.available_tracks for c in t["clips"]])
        self.ranges = [("Entire project", 0, end)]
        begin, finish = project["time_selection"]
        if finish > begin:
            self.ranges.append(("Time selection", begin, finish))
        for row in project["regions"]:
            self.ranges.append((f"Region {row['number']} · {row['name'] or 'Untitled'}",
                                row["start"], row["end"]))
        super().__init__(root, argparse.Namespace(result=None))
        self.root.title("AAF Station — Export")
        self.pick_tracks("selected" if any(t["selected"] for t in project["tracks"]) else "all")

    def build(self):
        outer = ttk.Frame(self.root, padding=20)
        outer.pack(fill="both", expand=True)
        head = ttk.Frame(outer)
        head.pack(fill="x")
        ttk.Label(head, text="AAF Station", style="Title.TLabel").pack(side="left")
        ttk.Label(head, text="Export AAF", style="Muted.TLabel").pack(side="right")
        ttk.Label(outer, text=self.project["name"], style="Muted.TLabel").pack(anchor="w", pady=(4, 15))

        settings = ttk.Frame(outer)
        settings.pack(fill="x", pady=(0, 12))
        settings.columnconfigure(1, weight=1)
        ttk.Label(settings, text="Range").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.range_menu = ttk.Combobox(settings, state="readonly",
                                      values=[row[0] for row in self.ranges])
        self.range_menu.current(1 if len(self.ranges) > 1 and self.ranges[1][0] == "Time selection" else 0)
        self.range_menu.grid(row=0, column=1, sticky="ew")
        self.range_menu.bind("<<ComboboxSelected>>", lambda _: self.refresh())
        self.range_text = tk.StringVar()
        ttk.Label(settings, textvariable=self.range_text, style="Muted.TLabel").grid(row=0, column=2, padx=12)
        self.keep_positions = tk.BooleanVar(value=False)
        ttk.Checkbutton(settings, text="Keep project positions", variable=self.keep_positions,
                        command=self.refresh).grid(row=0, column=3, sticky="e")

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(0, 8))
        self.track_buttons = []
        for label, mode in (("Selected in REAPER", "selected"), ("All", "all"), ("None", "none")):
            button = ttk.Button(buttons, text=label, command=lambda m=mode: self.pick_tracks(m))
            button.pack(side="left", padx=(0, 6))
            self.track_buttons.append(button)
        ttk.Label(buttons, text="AAF timeline", style="Muted.TLabel").pack(side="right")

        pane = ttk.Panedwindow(outer, orient="horizontal")
        pane.pack(fill="both", expand=True)
        inputs = ttk.Frame(pane)
        preview = ttk.Panedwindow(pane, orient="horizontal")
        pane.add(inputs, weight=1)
        pane.add(preview, weight=4)

        self.input_tree = ttk.Treeview(inputs, columns=("count",), selectmode="browse",
                                       show=("tree", "headings"), height=5)
        self.input_tree.heading("#0", text="REAPER tracks", anchor="w")
        self.input_tree.heading("count", text="Clips")
        self.input_tree.column("#0", width=180, minwidth=120)
        self.input_tree.column("count", width=50, stretch=False, anchor="center")
        self.input_tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(inputs, orient="vertical", command=self.input_tree.yview)
        scroll.pack(side="right", fill="y")
        self.input_tree.configure(yscrollcommand=scroll.set)
        self.input_tree.tag_configure("odd", background="#303136")
        self.input_tree.bind("<Button-1>", self.input_click)
        self.input_tree.bind("<space>", self.input_space)
        for i, track in enumerate(self.available_tracks):
            self.input_tree.insert("", "end", iid=track["id"], text="  " + track["name"],
                                   values=(len(track["clips"]),), tags=("odd",) if i % 2 else ())

        rows = ttk.Frame(preview)
        timeline = ttk.Frame(preview)
        preview.add(rows, weight=1)
        preview.add(timeline, weight=4)
        self.tree = ttk.Treeview(rows, columns=("type",), show=("tree", "headings"), height=5)
        self.tree.heading("#0", text="Exported tracks", anchor="w")
        self.tree.heading("type", text="Audio")
        self.tree.column("#0", width=160, minwidth=100)
        self.tree.column("type", width=55, stretch=False, anchor="center")
        self.tree.pack(fill="both", expand=True)
        ttk.Frame(rows, height=13).pack(fill="x")
        self.tree.tag_configure("odd", background="#303136")
        self.tree.tag_configure("blocked", foreground="#f2b5b5")
        self.tree.bind("<<TreeviewSelect>>", lambda _: self.draw())
        self.tree.bind("<MouseWheel>", self.mousewheel)
        self.build_timeline(timeline)

        overview = ttk.Frame(outer)
        overview.pack(fill="x", pady=(8, 7))
        self.detail = tk.StringVar(value="Point to a clip for its position, length and source offset.")
        ttk.Label(overview, textvariable=self.detail, style="Muted.TLabel").pack(side="left")
        self.zoom = tk.DoubleVar(value=1)
        self.tc_ruler = tk.BooleanVar(value=False)
        ttk.Scale(overview, from_=1, to=16, variable=self.zoom, command=lambda _: self.draw()).pack(side="right", padx=8)
        ttk.Label(overview, text="Zoom").pack(side="right")
        ttk.Checkbutton(overview, text="Timecode", variable=self.tc_ruler, command=self.draw).pack(side="right", padx=10)

        output = ttk.Frame(outer)
        output.pack(fill="x", pady=(5, 9))
        self.media = ttk.Combobox(output, state="readonly", width=23,
                                  values=("Linked WAV files", "Embedded audio"))
        self.media.current(0)
        self.media.pack(side="left", padx=(0, 12))
        ttk.Label(output, text="Sample rate").pack(side="left")
        rates = sorted({44100, 48000, 88200, 96000, 192000, self.project["sample_rate"]})
        self.rate_menu = ttk.Combobox(output, state="readonly", width=8, values=rates)
        self.rate_menu.set(self.project["sample_rate"])
        self.rate_menu.pack(side="left", padx=(6, 12))
        self.rate_menu.bind("<<ComboboxSelected>>", lambda _: self.refresh())
        ttk.Label(output, text="Handles (ms)").pack(side="left")
        self.handles = ttk.Entry(output, width=7)
        self.handles.insert(0, "1000")
        self.handles.pack(side="left", padx=(6, 12))
        self.stereo_menu = ttk.Combobox(output, state="readonly", width=24,
                                        values=("Stereo for Pro Tools", "Split into L/R mono tracks"))
        self.stereo_menu.current(0)
        self.stereo_menu.pack(side="left")
        self.stereo_menu.bind("<<ComboboxSelected>>", lambda _: self.refresh())
        self.fades_menu = ttk.Combobox(output, state="readonly", width=24,
                                       values=("Render fades (Pro Tools)", "Editable linear fades"))
        self.fades_menu.current(0)
        self.fades_menu.pack(side="left", padx=(10, 0))
        self.fades_menu.bind("<<ComboboxSelected>>", lambda _: self.refresh())

        footer = ttk.Frame(outer)
        footer.pack(fill="x")
        self.summary = tk.StringVar()
        ttk.Label(footer, textvariable=self.summary).pack(side="left")
        self.cancel_button = ttk.Button(footer, text="Close", command=self.close)
        self.cancel_button.pack(side="right")
        self.import_button = ttk.Button(footer, text="Export AAF…", style="Accent.TButton", command=self.save)
        self.import_button.pack(side="right", padx=6)
        self.notes_button = ttk.Button(footer, text="Export notes", command=self.notes)
        self.notes_button.pack(side="right", padx=6)
        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        self.progress.pack(fill="x", pady=(8, 0))
        self.export_controls = [*self.track_buttons, self.range_menu, self.rate_menu, self.media,
                                self.stereo_menu, self.fades_menu, self.handles, self.import_button]

    def pick_tracks(self, mode):
        if self.busy:
            return
        self.selected = (set(self.reaper_selection) if mode == "selected" else
                         {track["id"] for track in self.available_tracks} if mode == "all" else set())
        self.refresh()

    def input_click(self, event):
        row = self.input_tree.identify_row(event.y)
        if row and self.input_tree.identify_region(event.x, event.y) in ("tree", "cell"):
            self.input_tree.focus_set()
            self.input_tree.focus(row)
            self.input_tree.selection_set(row)
            self.toggle_input(row)
            return "break"

    def input_space(self, _event):
        row = self.input_tree.focus()
        if row:
            self.toggle_input(row)
        return "break"

    def toggle_input(self, row):
        if not self.busy:
            self.selected.symmetric_difference_update({row})
            self.refresh()

    def refresh(self):
        if self.busy:
            return
        for row in self.input_tree.get_children():
            self.input_tree.item(row, image=self.track_indicators[row in self.selected])
        _, begin, end = self.ranges[self.range_menu.current()]
        self.range_text.set(f"{clock(begin)}  –  {clock(end)}")
        self.tree.delete(*self.tree.get_children())
        try:
            plan = plan_export(self.project, self.selected, (begin, end), int(self.rate_menu.get()),
                               self.keep_positions.get(), self.stereo_menu.current() == 1,
                               self.fades_menu.current() == 0)
        except ValueError as exc:
            self.session = None
            self.summary.set(str(exc))
            self.notes_button.configure(state="disabled")
            self.import_button.configure(state="disabled")
            self.draw()
            return
        self.session = plan
        for index, track in enumerate(plan.tracks):
            channels = len(track.clips[0].channels)
            tags = ("odd",) if index % 2 else ()
            if any(clip.error for clip in track.clips):
                tags += ("blocked",)
            self.tree.insert("", "end", iid=track.id, text="  " + track.name,
                             image=self.track_indicators[True],
                             values=("L/R" if channels == 2 else "Mono",), tags=tags)
        self.canvas.yview_moveto(0)
        self.tree.yview_moveto(0)
        count = sum(len(track.clips) for track in plan.tracks)
        status = f"{len(plan.tracks)} output tracks · {count} clips"
        if plan.errors:
            status += f" · {len(plan.errors)} blocked"
        elif len(plan.warnings) == 1:
            status += " · 1 note"
        elif plan.warnings:
            status += f" · {len(plan.warnings)} notes"
        self.summary.set(status)
        self.import_button.configure(state="normal" if count and not plan.errors else "disabled")
        self.notes_button.configure(state="normal" if plan.errors or plan.warnings else "disabled")
        self.draw()

    def timeline_bounds(self, clips):
        return 0, self.session.length / self.session.rate

    def preview_selected(self, track):
        return True

    def preview_fades(self, clip):
        return clip.fade_in / clip.rate, clip.fade_out / clip.rate

    def hover(self, event):
        found = self.at_pointer(event)
        if not found:
            return
        track, clip = found
        text = f"{track.name} · {clip.name} · {clock(clip.position)} + {clip.duration:.3f}s · source {clip.offset:.3f}s"
        self.detail.set(clip.error or text)

    def notes(self):
        if not self.session:
            return
        plan = self.session
        window = tk.Toplevel(self.root)
        window.title("AAF Station — Export notes")
        window.geometry("760x400")
        text = tk.Text(window, wrap="word", bg=PANEL, fg=TEXT, padx=14, pady=12,
                       highlightthickness=0, font=("Helvetica", 12))
        scroll = ttk.Scrollbar(window, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        for label, lines in (("Blocked items", plan.errors), ("Export notes", plan.warnings)):
            if lines:
                text.insert("end", label + "\n\n" + "\n".join("• " + line for line in lines) + "\n\n")
        text.configure(state="disabled")

    def save(self):
        if self.busy or not self.session or self.session.errors or not self.session.tracks:
            return
        try:
            handles = int(self.handles.get())
            if not 0 <= handles <= 60000:
                raise ValueError
        except ValueError:
            messagebox.showerror("AAF Station", "Enter a handle length between 0 and 60000 ms.", parent=self.root)
            return
        if self.session.warnings:
            lines = self.session.warnings
            brief = "\n\n".join(lines[:4])
            if len(lines) > 4:
                brief += f"\n\n{len(lines) - 4} more in Export notes."
            if not messagebox.askokcancel("AAF Station — Export notes", brief, parent=self.root):
                return
        project_path = Path(self.project["path"]) if self.project["path"] else None
        destination = filedialog.asksaveasfilename(parent=self.root, title="Export AAF",
                                                    initialfile=safe_name(self.project["name"]) + ".aaf",
                                                    initialdir=project_path.parent if project_path else None,
                                                    defaultextension=".aaf",
                                                    filetypes=[("AAF audio session", "*.aaf")])
        if not destination:
            return
        plan = self.session
        embedded = self.media.current() == 1
        self.busy = True
        self.summary.set("Writing AAF and audio…")
        self.progress.start(12)
        for widget in self.export_controls:
            widget.state(["disabled"])
        self.future = self.pool.submit(write_aaf, plan, destination, embedded=embedded, handles_ms=handles)

        def poll():
            if not self.future.done():
                self.root.after(100, poll)
                return
            self.busy = False
            self.progress.stop()
            for widget in self.export_controls:
                widget.state(["!disabled"])
            self.refresh()
            try:
                aaf, media = self.future.result()
            except Exception as exc:
                messagebox.showerror("AAF export stopped", str(exc), parent=self.root)
            else:
                text = str(aaf)
                if media:
                    text += "\n\nKeep the AAF together with " + media.name + "."
                messagebox.showinfo("AAF exported", text, parent=self.root)

        self.root.after(100, poll)

    def close(self):
        if self.busy:
            self.summary.set("Writing AAF and audio…")
            return
        self.pool.shutdown(wait=False)
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    root = tk.Tk()
    try:
        project = json.loads(args.snapshot.read_text(encoding="utf-8"))
        args.snapshot.unlink()
        args.snapshot.parent.rmdir()
        ExportWindow(root, project)
    except Exception as exc:
        root.withdraw()
        messagebox.showerror("AAF Station — Export", str(exc), parent=root)
        root.destroy()
        return
    root.after_idle(present_window, root)
    root.mainloop()


if __name__ == "__main__":
    main()
