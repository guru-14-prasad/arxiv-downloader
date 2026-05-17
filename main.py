import tkinter as tk
from tkinter import ttk, messagebox
import threading
import os
import re
import requests
import feedparser
import subprocess
import sys
import time
import queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# ================= OPTIMIZED CONFIG =================
MAX_WORKERS = 30
ARXIV_BATCH = 100
CHUNK_SIZE = 81920
MIN_PDF_SIZE = 10 * 1024
MAX_RETRIES = 2
REQUEST_TIMEOUT = 30
# ====================================================

# ================= COLOR PALETTE =================
COLORS = {
    "bg_deep":       "#050d1a",   # Deepest navy background
    "bg_dark":       "#0a1628",   # Main window background
    "bg_panel":      "#0f2040",   # Panel / card background
    "bg_input":      "#0d1c36",   # Entry fields
    "bg_hover":      "#1a3560",   # Hover state
    "accent":        "#1e6fff",   # Primary blue accent
    "accent_bright": "#4d8fff",   # Brighter accent
    "accent_dim":    "#1250b8",   # Dimmer accent (for buttons)
    "success":       "#00c97a",   # Green for success
    "warning":       "#f59e0b",   # Amber for warnings/pause
    "danger":        "#ef4444",   # Red for stop
    "resume":        "#3b82f6",   # Blue for resume
    "text_primary":  "#e8f0ff",   # Near-white text
    "text_secondary":"#7ba3d8",   # Muted blue-white
    "text_dim":      "#3d6094",   # Dimmed text
    "border":        "#1a3560",   # Subtle border
    "border_bright": "#1e6fff",   # Accent border
    "scrollbar":     "#1a3560",
}
# =================================================

def clean_name(text):
    return re.sub(r'[\\/:*?"<>|]', '', text).replace(" ", "_").lower()

def clean_filename(name):
    cleaned = re.sub(r'[\\/:*?"<>|]', '', name).replace(" ", "_")
    return cleaned[:100]


class ArxivSearchWorker(threading.Thread):
    def __init__(self, keyword, result_queue, stop_event, start_index=0):
        super().__init__()
        self.keyword = keyword
        self.result_queue = result_queue
        self.stop_event = stop_event
        self.start_index = start_index
        self.daemon = True

    def run(self):
        session = requests.Session()
        current_index = self.start_index

        while not self.stop_event.is_set():
            try:
                q = self.keyword.replace(" ", "+")
                url = (f"http://export.arxiv.org/api/query?"
                       f"search_query=all:{q}&start={current_index}&max_results={ARXIV_BATCH}")
                response = session.get(url, timeout=60)
                entries = feedparser.parse(response.text).entries
                if not entries:
                    break
                for entry in entries:
                    if self.stop_event.is_set():
                        break
                    self.result_queue.put(entry)
                current_index += ARXIV_BATCH
                time.sleep(0.5)
            except Exception as e:
                print(f"Search error: {e}")
                time.sleep(2)


def download_pdf_with_retry(entry, pdf_dir, stop_event, pause_event, session):
    title = entry.title.strip()
    pid = entry.id.split("/")[-1].split("v")[0]
    filename = f"{clean_filename(title)}__{pid}.pdf"
    path = os.path.join(pdf_dir, filename)
    temp_path = path + ".part"

    if os.path.exists(path):
        return None, filename

    for attempt in range(MAX_RETRIES + 1):
        if stop_event.is_set():
            return None, filename
        try:
            pdf_url = f"https://arxiv.org/pdf/{pid}.pdf"
            headers = {}
            if os.path.exists(temp_path):
                downloaded_size = os.path.getsize(temp_path)
                headers['Range'] = f'bytes={downloaded_size}-'
            response = session.get(pdf_url, headers=headers, stream=True, timeout=REQUEST_TIMEOUT)
            if response.status_code not in [200, 206]:
                continue
            mode = 'ab' if 'Range' in headers else 'wb'
            with open(temp_path, mode) as f:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    pause_event.wait()
                    if stop_event.is_set():
                        return None, filename
                    if chunk:
                        f.write(chunk)
            if os.path.getsize(temp_path) >= MIN_PDF_SIZE:
                os.rename(temp_path, path)
                return title, filename
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            if os.path.exists(temp_path) and attempt == MAX_RETRIES:
                try:
                    os.remove(temp_path)
                except:
                    pass
            continue

    return None, filename


# ───────────────────────────── custom widgets ─────────────────────────────

class DarkEntry(tk.Entry):
    """Styled entry widget matching dark blue theme."""
    def __init__(self, master, **kwargs):
        super().__init__(
            master,
            bg=COLORS["bg_input"],
            fg=COLORS["text_primary"],
            insertbackground=COLORS["accent_bright"],
            relief="flat",
            highlightthickness=1,
            highlightcolor=COLORS["accent"],
            highlightbackground=COLORS["border"],
            selectbackground=COLORS["accent_dim"],
            selectforeground=COLORS["text_primary"],
            **kwargs
        )
        self.bind("<FocusIn>",  self._on_focus_in)
        self.bind("<FocusOut>", self._on_focus_out)

    def _on_focus_in(self, _e):
        self.config(highlightbackground=COLORS["accent"], highlightcolor=COLORS["accent"])

    def _on_focus_out(self, _e):
        self.config(highlightbackground=COLORS["border"], highlightcolor=COLORS["border"])


class DarkButton(tk.Canvas):
    """
    Pill-shaped canvas button with hover animation,
    configurable accent color, and icon support.
    """
    def __init__(self, master, text, command=None,
                 color=None, width=160, height=42, **kwargs):
        self._btn_width  = width
        self._btn_height = height
        super().__init__(master, width=width, height=height,
                         bg=COLORS["bg_dark"], highlightthickness=0, **kwargs)
        self._text    = text
        self._command = command
        self._color   = color or COLORS["accent"]
        self._hover   = False
        self._enabled = True
        self._draw()
        self.bind("<Enter>",         self._on_enter)
        self.bind("<Leave>",         self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)

    # ── drawing helpers ──────────────────────────────────────────────────
    def _draw(self):
        self.delete("all")
        w, h = self._btn_width, self._btn_height
        r = h // 2          # pill radius

        if not self._enabled:
            fill   = COLORS["bg_panel"]
            outline= COLORS["border"]
            txt_c  = COLORS["text_dim"]
        elif self._hover:
            fill   = self._color
            outline= self._color
            txt_c  = "#ffffff"
        else:
            fill   = COLORS["bg_panel"]
            outline= self._color
            txt_c  = self._color

        # Pill shape via two arcs + rectangle
        self.create_arc(0, 0, r*2, h, start=90, extent=180,
                        fill=fill, outline=outline, width=1)
        self.create_arc(w-r*2, 0, w, h, start=270, extent=180,
                        fill=fill, outline=outline, width=1)
        self.create_rectangle(r, 0, w-r, h, fill=fill, outline=fill)
        # Left / right borders
        self.create_line(r, 0, w-r, 0, fill=outline)
        self.create_line(r, h, w-r, h, fill=outline)

        self.create_text(w//2, h//2, text=self._text,
                         fill=txt_c, font=("Segoe UI", 9, "bold"))

    # ── state helpers ────────────────────────────────────────────────────
    def _on_enter(self, _e):
        if self._enabled:
            self._hover = True;  self._draw()

    def _on_leave(self, _e):
        self._hover = False; self._draw()

    def _on_press(self, _e):
        if self._enabled and self._command:
            self._command()

    def config_state(self, state):
        self._enabled = (state == "normal")
        self._draw()

    def update_text(self, text):
        self._text = text; self._draw()


# ───────────────────────────── main app ────────────────────────────────────

class OptimizedDownloader:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("ArXiv Research Downloader")
        root.geometry("960x700")
        root.minsize(800, 600)
        root.configure(bg=COLORS["bg_dark"])

        # State
        self.running          = False
        self.stop_event       = threading.Event()
        self.pause_event      = threading.Event()
        self.pause_event.set()
        self.done_called      = False
        self.downloaded_count = 0
        self.failed_count     = 0
        self.start_time       = None
        self.search_worker    = None
        self.required_count   = 0
        self.base_folder      = ""
        self.pdf_folder       = ""

        self._apply_style()
        self._build_ui()
        self._setup_shortcuts()

        threading.Thread(target=self._perf_monitor, daemon=True).start()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── theming ──────────────────────────────────────────────────────────
    def _apply_style(self):
        style = ttk.Style()
        style.theme_use("clam")

        # Notebook
        style.configure("Dark.TNotebook",
                         background=COLORS["bg_dark"],
                         borderwidth=0,
                         tabmargins=[0, 0, 0, 0])
        style.configure("Dark.TNotebook.Tab",
                         background=COLORS["bg_panel"],
                         foreground=COLORS["text_secondary"],
                         padding=[18, 8],
                         font=("Segoe UI", 9),
                         borderwidth=0)
        style.map("Dark.TNotebook.Tab",
                  background=[("selected", COLORS["bg_input"])],
                  foreground=[("selected", COLORS["accent_bright"])])

        # Progressbar
        style.configure("Blue.Horizontal.TProgressbar",
                         troughcolor=COLORS["bg_panel"],
                         background=COLORS["accent"],
                         borderwidth=0,
                         thickness=6)
        style.configure("TProgressbar",
                         troughcolor=COLORS["bg_panel"],
                         background=COLORS["accent"],
                         borderwidth=0)

    # ── UI construction ───────────────────────────────────────────────────
    def _build_ui(self):
        C = COLORS

        # ── outer padding frame ──
        outer = tk.Frame(self.root, bg=C["bg_dark"], padx=24, pady=20)
        outer.pack(fill="both", expand=True)

        # ── header ──────────────────────────────────────────────────────
        hdr_frame = tk.Frame(outer, bg=C["bg_dark"])
        hdr_frame.pack(fill="x", pady=(0, 20))

        # Left accent stripe
        tk.Frame(hdr_frame, bg=C["accent"], width=4).pack(side="left", fill="y")

        hdr_text = tk.Frame(hdr_frame, bg=C["bg_dark"])
        hdr_text.pack(side="left", padx=(14, 0))

        tk.Label(hdr_text,
                 text="ArXiv Research Downloader",
                 font=("Segoe UI", 20, "bold"),
                 fg=C["text_primary"], bg=C["bg_dark"]).pack(anchor="w")
        tk.Label(hdr_text,
                 text="High-speed parallel PDF fetcher for academic papers",
                 font=("Segoe UI", 9),
                 fg=C["text_secondary"], bg=C["bg_dark"]).pack(anchor="w")

        # Config badge (top right)
        badge = tk.Frame(hdr_frame, bg=C["bg_panel"], padx=14, pady=8)
        badge.pack(side="right")
        tk.Label(badge,
                 text=f"⚡ {MAX_WORKERS} workers  •  batch {ARXIV_BATCH}",
                 font=("Segoe UI", 8),
                 fg=C["text_secondary"], bg=C["bg_panel"]).pack()

        # ── input card ──────────────────────────────────────────────────
        card = tk.Frame(outer, bg=C["bg_panel"], padx=20, pady=18)
        card.pack(fill="x", pady=(0, 14))

        # Keyword row
        row1 = tk.Frame(card, bg=C["bg_panel"])
        row1.pack(fill="x", pady=(0, 10))

        tk.Label(row1, text="KEYWORD", font=("Segoe UI", 8, "bold"),
                 fg=C["text_dim"], bg=C["bg_panel"]).pack(anchor="w")
        self.keyword_entry = DarkEntry(row1, font=("Segoe UI", 11))
        self.keyword_entry.pack(fill="x", ipady=8, pady=(4, 0))

        # Count + path row
        row2 = tk.Frame(card, bg=C["bg_panel"])
        row2.pack(fill="x")

        left2 = tk.Frame(row2, bg=C["bg_panel"])
        left2.pack(side="left", fill="x", expand=True, padx=(0, 20))
        tk.Label(left2, text="NUMBER OF PAPERS", font=("Segoe UI", 8, "bold"),
                 fg=C["text_dim"], bg=C["bg_panel"]).pack(anchor="w")
        self.count_entry = DarkEntry(left2, font=("Segoe UI", 11), width=18)
        self.count_entry.pack(anchor="w", ipady=8, pady=(4, 0))

        right2 = tk.Frame(row2, bg=C["bg_panel"])
        right2.pack(side="right", fill="x", expand=True)
        tk.Label(right2, text="OUTPUT FOLDER", font=("Segoe UI", 8, "bold"),
                 fg=C["text_dim"], bg=C["bg_panel"]).pack(anchor="w")
        self.folder_label = tk.Label(right2,
                                     text="(auto-generated from keyword)",
                                     font=("Segoe UI", 9),
                                     fg=C["text_secondary"], bg=C["bg_panel"])
        self.folder_label.pack(anchor="w", pady=(8, 0))

        # ── stats bar ───────────────────────────────────────────────────
        stats_bg = tk.Frame(outer, bg=C["bg_deep"], padx=16, pady=10)
        stats_bg.pack(fill="x", pady=(0, 10))

        self.stat_speed  = self._stat_cell(stats_bg, "SPEED",       "0 PDFs/min")
        self.stat_done   = self._stat_cell(stats_bg, "DOWNLOADED",  "0")
        self.stat_failed = self._stat_cell(stats_bg, "FAILED",      "0")
        self.stat_eta    = self._stat_cell(stats_bg, "ETA",         "--:--:--")
        self.stat_elapsed= self._stat_cell(stats_bg, "ELAPSED",     "00:00")

        # ── progress ────────────────────────────────────────────────────
        prog_frame = tk.Frame(outer, bg=C["bg_dark"])
        prog_frame.pack(fill="x", pady=(0, 14))

        top_prog = tk.Frame(prog_frame, bg=C["bg_dark"])
        top_prog.pack(fill="x")
        self.progress_label = tk.Label(top_prog,
                                       text="0 / 0",
                                       font=("Segoe UI", 9),
                                       fg=C["text_secondary"],
                                       bg=C["bg_dark"])
        self.progress_label.pack(side="right")
        self.status_label = tk.Label(top_prog,
                                     text="Ready",
                                     font=("Segoe UI", 9, "bold"),
                                     fg=C["accent_bright"],
                                     bg=C["bg_dark"])
        self.status_label.pack(side="left")

        self.progress_bar = ttk.Progressbar(prog_frame, length=900,
                                             mode="determinate",
                                             style="Blue.Horizontal.TProgressbar")
        self.progress_bar.pack(fill="x", pady=(6, 0))

        # ── buttons ──────────────────────────────────────────────────────
        btn_frame = tk.Frame(outer, bg=C["bg_dark"])
        btn_frame.pack(pady=(0, 14))

        self.btn_start  = DarkButton(btn_frame, "▶  START",   self.start_download,
                                     color=C["success"], width=150)
        self.btn_pause  = DarkButton(btn_frame, "⏸  PAUSE",   self.pause_download,
                                     color=C["warning"], width=150)
        self.btn_resume = DarkButton(btn_frame, "▶  RESUME",  self.resume_download,
                                     color=C["resume"],  width=150)
        self.btn_stop   = DarkButton(btn_frame, "■  STOP",    self.end_download,
                                     color=C["danger"],  width=150)

        for i, btn in enumerate([self.btn_start, self.btn_pause,
                                  self.btn_resume, self.btn_stop]):
            btn.grid(row=0, column=i, padx=8)

        self.btn_pause.config_state("disabled")
        self.btn_resume.config_state("disabled")
        self.btn_stop.config_state("disabled")

        # Shortcut hints
        tk.Label(btn_frame,
                 text="Enter = start  •  Space = pause/resume  •  Esc = stop",
                 font=("Segoe UI", 8), fg=C["text_dim"], bg=C["bg_dark"]
                 ).grid(row=1, column=0, columnspan=4, pady=(6, 0))

        # ── log notebook ─────────────────────────────────────────────────
        notebook = ttk.Notebook(outer, style="Dark.TNotebook")
        notebook.pack(fill="both", expand=True)

        self.log_text  = self._log_tab(notebook, "📋  Download Log")
        self.perf_text = self._log_tab(notebook, "📊  Failures & Perf")

    # ── helper: stat cell ─────────────────────────────────────────────────
    def _stat_cell(self, parent, label, value):
        C = COLORS
        cell = tk.Frame(parent, bg=C["bg_deep"], padx=20, pady=4)
        cell.pack(side="left", expand=True)
        tk.Label(cell, text=label, font=("Segoe UI", 7, "bold"),
                 fg=C["text_dim"], bg=C["bg_deep"]).pack()
        val_lbl = tk.Label(cell, text=value, font=("Segoe UI", 13, "bold"),
                           fg=C["text_primary"], bg=C["bg_deep"])
        val_lbl.pack()
        return val_lbl

    # ── helper: log tab ───────────────────────────────────────────────────
    def _log_tab(self, notebook, title):
        C = COLORS
        frame = tk.Frame(notebook, bg=C["bg_input"])
        notebook.add(frame, text=title)
        txt = tk.Text(frame,
                      font=("Consolas", 9),
                      bg=C["bg_input"],
                      fg=C["text_secondary"],
                      insertbackground=C["accent_bright"],
                      selectbackground=C["accent_dim"],
                      relief="flat",
                      wrap="word",
                      padx=10, pady=8)
        sb = tk.Scrollbar(frame, bg=C["bg_panel"],
                           troughcolor=C["bg_panel"],
                           activebackground=C["accent_dim"])
        sb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True, side="left")
        txt.config(yscrollcommand=sb.set)
        sb.config(command=txt.yview)

        # Custom tags
        txt.tag_config("success", foreground=C["success"])
        txt.tag_config("error",   foreground=C["danger"])
        txt.tag_config("info",    foreground=C["text_secondary"])
        txt.tag_config("heading", foreground=C["accent_bright"])
        txt.tag_config("dim",     foreground=C["text_dim"])
        return txt

    # ── shortcuts ─────────────────────────────────────────────────────────
    def _setup_shortcuts(self):
        self.keyword_entry.bind("<Return>", lambda _: self.count_entry.focus_set())
        self.count_entry.bind("<Return>",   lambda _: self.start_download())
        self.root.bind("<space>",  self._toggle_pause)
        self.root.bind("<Escape>", lambda _: self.end_download())
        self.root.after(100, lambda: self.keyword_entry.focus_set())

    # ── logging ───────────────────────────────────────────────────────────
    def _log(self, message, tag="info", widget=None):
        target = widget or self.log_text
        ts = datetime.now().strftime("%H:%M:%S")
        target.insert(tk.END, f"  {ts}  ", "dim")
        target.insert(tk.END, f"{message}\n", tag)
        target.see(tk.END)
        self.root.update_idletasks()

    # ── performance monitor ───────────────────────────────────────────────
    def _perf_monitor(self):
        while True:
            if self.running and self.start_time:
                elapsed = time.time() - self.start_time
                if elapsed > 0:
                    speed = (self.downloaded_count / elapsed) * 60
                    remaining = max(0, self.required_count - self.downloaded_count)
                    eta_str = "--:--:--"
                    if speed > 0:
                        eta_s = remaining / (speed / 60)
                        eta_str = time.strftime("%H:%M:%S", time.gmtime(eta_s))

                    self.stat_speed.config(text=f"{speed:.1f} PDFs/min")
                    self.stat_eta.config(text=eta_str)
                    self.stat_elapsed.config(
                        text=time.strftime("%M:%S", time.gmtime(elapsed)))
            time.sleep(2)

    # ── progress update ───────────────────────────────────────────────────
    def _update_progress(self):
        self.progress_bar["value"]   = self.downloaded_count
        self.progress_bar["maximum"] = self.required_count
        pct = int(self.downloaded_count / max(self.required_count, 1) * 100)
        self.progress_label.config(
            text=f"{self.downloaded_count} / {self.required_count}  ({pct}%)")
        self.stat_done.config(text=str(self.downloaded_count))
        self.stat_failed.config(text=str(self.failed_count))

    # ── START ─────────────────────────────────────────────────────────────
    def start_download(self):
        if self.running:
            return

        keyword    = self.keyword_entry.get().strip()
        count_text = self.count_entry.get().strip()

        if not keyword:
            messagebox.showerror("Missing Input", "Please enter a research keyword.")
            self.keyword_entry.focus_set()
            return
        if not count_text.isdigit() or int(count_text) <= 0:
            messagebox.showerror("Invalid Input", "Please enter a valid number of PDFs.")
            self.count_entry.focus_set()
            return

        self.required_count   = int(count_text)
        self.running          = True
        self.done_called      = False
        self.stop_event.clear()
        self.pause_event.set()
        self.downloaded_count = 0
        self.failed_count     = 0
        self.start_time       = time.time()

        self.base_folder = clean_name(keyword)
        self.pdf_folder  = os.path.join(self.base_folder, "pdfs")
        os.makedirs(self.pdf_folder, exist_ok=True)
        self.folder_label.config(text=os.path.abspath(self.base_folder))

        existing = len([f for f in os.listdir(self.pdf_folder) if f.endswith('.pdf')])
        if existing > 0:
            self.downloaded_count = existing

        self._update_progress()
        self.log_text.delete(1.0, tk.END)
        self.perf_text.delete(1.0, tk.END)

        self._log("─" * 56, "dim")
        self._log(f"STARTED  ·  {keyword}  ·  target {self.required_count} PDFs", "heading")
        self._log(f"Workers: {MAX_WORKERS}  ·  Batch: {ARXIV_BATCH}  ·  Chunk: {CHUNK_SIZE//1024} KB", "dim")
        self._log("─" * 56, "dim")

        self.status_label.config(text="Downloading…", fg=COLORS["accent_bright"])

        # Button states
        self.keyword_entry.config(state="disabled")
        self.count_entry.config(state="disabled")
        self.btn_start.config_state("disabled")
        self.btn_pause.config_state("normal")
        self.btn_stop.config_state("normal")
        self.btn_resume.config_state("disabled")

        threading.Thread(target=self._download_process, daemon=True).start()

    # ── download process ──────────────────────────────────────────────────
    def _download_process(self):
        result_queue = queue.Queue(maxsize=MAX_WORKERS * 2)

        self.search_worker = ArxivSearchWorker(
            self.keyword_entry.get(),
            result_queue,
            self.stop_event,
            start_index=self.downloaded_count
        )
        self.search_worker.start()

        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
        })

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = []

            while (self.downloaded_count < self.required_count
                   and not self.stop_event.is_set()):

                while (len(futures) < MAX_WORKERS * 2
                       and self.downloaded_count + len(futures) < self.required_count):
                    try:
                        entry = result_queue.get(timeout=5)
                        fut = executor.submit(download_pdf_with_retry,
                                              entry, self.pdf_folder,
                                              self.stop_event, self.pause_event, session)
                        futures.append(fut)
                    except queue.Empty:
                        if not self.search_worker.is_alive():
                            break
                        continue

                for fut in as_completed(futures[:]):
                    if self.stop_event.is_set():
                        break
                    try:
                        title, filename = fut.result(timeout=1)
                        if title:
                            self.downloaded_count += 1
                            self.root.after(0, self._on_success, title)
                        else:
                            self.failed_count += 1
                            self.root.after(0, self._on_failed, filename)
                    except Exception:
                        self.failed_count += 1
                    futures.remove(fut)
                    if self.downloaded_count >= self.required_count:
                        break

                time.sleep(0.1)

        self.stop_event.set()
        if self.search_worker and self.search_worker.is_alive():
            self.search_worker.join(timeout=1)

        self.root.after(0, self._on_complete)

    def _on_success(self, title):
        short = title[:72] + "…" if len(title) > 72 else title
        self._log(f"✓  {short}", "success")
        self._update_progress()

    def _on_failed(self, filename):
        self._log(f"✗  {filename}", "error", widget=self.perf_text)
        self._update_progress()

    def _on_complete(self):
        if self.done_called:
            return
        self.done_called = True
        self.running     = False
        elapsed = time.time() - self.start_time

        self._log("─" * 56, "dim")
        self._log("COMPLETE", "heading")
        self._log(f"Downloaded : {self.downloaded_count} PDFs", "success")
        self._log(f"Failed     : {self.failed_count}", "error" if self.failed_count else "info")
        self._log(f"Duration   : {elapsed:.1f}s  ·  "
                  f"Avg {self.downloaded_count/max(elapsed,1)*60:.1f} PDFs/min", "info")
        self._log("─" * 56, "dim")

        self.status_label.config(text="Complete ✓", fg=COLORS["success"])
        self._reset_buttons()

        try:
            path = os.path.abspath(self.base_folder)
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception:
            pass

    # ── pause / resume / stop ─────────────────────────────────────────────
    def _toggle_pause(self, _e=None):
        if not self.running:
            return
        if self.pause_event.is_set():
            self.pause_download()
        else:
            self.resume_download()

    def pause_download(self):
        if not self.running:
            return
        self.pause_event.clear()
        self._log("⏸  Paused", "info")
        self.status_label.config(text="Paused", fg=COLORS["warning"])
        self.btn_pause.config_state("disabled")
        self.btn_resume.config_state("normal")

    def resume_download(self):
        if not self.running:
            return
        self.pause_event.set()
        self._log("▶  Resumed", "info")
        self.status_label.config(text="Downloading…", fg=COLORS["accent_bright"])
        self.btn_resume.config_state("disabled")
        self.btn_pause.config_state("normal")

    def end_download(self):
        if not self.running:
            return
        self.stop_event.set()
        self.pause_event.set()
        self.running = False

        if hasattr(self, 'pdf_folder') and os.path.exists(self.pdf_folder):
            for f in os.listdir(self.pdf_folder):
                if f.endswith(".part"):
                    try:
                        os.remove(os.path.join(self.pdf_folder, f))
                    except Exception:
                        pass

        self._log("■  Stopped by user", "error")
        self.status_label.config(text="Stopped", fg=COLORS["danger"])
        self._reset_buttons()

    def _reset_buttons(self):
        self.keyword_entry.config(state="normal")
        self.count_entry.config(state="normal")
        self.btn_start.config_state("normal")
        self.btn_pause.config_state("disabled")
        self.btn_resume.config_state("disabled")
        self.btn_stop.config_state("disabled")

    def _on_close(self):
        if self.running:
            self.end_download()
        self.root.destroy()


# ═══════════════════════════════ ENTRY POINT ═══════════════════════════════
if __name__ == "__main__":
    required_packages = ['feedparser', 'requests']
    missing = [p for p in required_packages if not __import__('importlib').util.find_spec(p)]
    if missing:
        print(f"Missing packages: {', '.join(missing)}")
        print("Install with: pip install feedparser requests")
        sys.exit(1)

    root = tk.Tk()
    app  = OptimizedDownloader(root)

    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    x = (root.winfo_screenwidth()  // 2) - (w // 2)
    y = (root.winfo_screenheight() // 2) - (h // 2)
    root.geometry(f"{w}x{h}+{x}+{y}")

    root.mainloop()