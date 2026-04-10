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
from concurrent.futures import ThreadPoolExecutor, as_completed, ProcessPoolExecutor
from datetime import datetime

# ================= OPTIMIZED CONFIG =================
MAX_WORKERS = 30                     # Increased for more parallel downloads
ARXIV_BATCH = 100                   # Larger batches for fewer API calls
CHUNK_SIZE = 81920                  # Larger chunks for faster downloads
MIN_PDF_SIZE = 10 * 1024            # 10KB minimum (more permissive)
MAX_RETRIES = 2                     # Retry failed downloads
REQUEST_TIMEOUT = 30                # Longer timeout for large files
# ====================================================

def clean_name(text):
    """Clean folder name to be filesystem-safe"""
    return re.sub(r'[\\/:*?"<>|]', '', text).replace(" ", "_").lower()

def clean_filename(name):
    """Clean filename to be filesystem-safe, limit length"""
    cleaned = re.sub(r'[\\/:*?"<>|]', '', name).replace(" ", "_")
    return cleaned[:100]  # Shorter for speed

class ArxivSearchWorker(threading.Thread):
    """Dedicated thread for searching arXiv to avoid blocking"""
    def __init__(self, keyword, result_queue, stop_event, start_index=0):
        super().__init__()
        self.keyword = keyword
        self.result_queue = result_queue
        self.stop_event = stop_event
        self.start_index = start_index
        self.daemon = True
    
    def run(self):
        """Search arXiv and put results in queue"""
        session = requests.Session()
        current_index = self.start_index
        
        while not self.stop_event.is_set():
            try:
                q = self.keyword.replace(" ", "+")
                url = (f"http://export.arxiv.org/api/query?"
                      f"search_query=all:{q}&start={current_index}&max_results={ARXIV_BATCH}")
                
                # Use longer timeout for search
                response = session.get(url, timeout=60)
                entries = feedparser.parse(response.text).entries
                
                if not entries:
                    break  # No more results
                
                # Put entries in queue for download workers
                for entry in entries:
                    if self.stop_event.is_set():
                        break
                    self.result_queue.put(entry)
                
                current_index += ARXIV_BATCH
                
                # Small delay to be polite to arXiv
                time.sleep(0.5)
                
            except Exception as e:
                print(f"Search error: {e}")
                time.sleep(2)  # Wait before retry

def download_pdf_with_retry(entry, pdf_dir, stop_event, pause_event, session):
    """Download with retry logic for reliability"""
    title = entry.title.strip()
    pid = entry.id.split("/")[-1].split("v")[0]  # Remove version
    filename = f"{clean_filename(title)}__{pid}.pdf"
    path = os.path.join(pdf_dir, filename)
    temp_path = path + ".part"
    
    # Skip if already downloaded
    if os.path.exists(path):
        return None, filename
    
    for attempt in range(MAX_RETRIES + 1):
        if stop_event.is_set():
            return None, filename
        
        try:
            # Get PDF URL
            pdf_url = f"https://arxiv.org/pdf/{pid}.pdf"
            
            # Download with resume capability
            headers = {}
            if os.path.exists(temp_path):
                downloaded_size = os.path.getsize(temp_path)
                headers['Range'] = f'bytes={downloaded_size}-'
            
            response = session.get(pdf_url, 
                                 headers=headers, 
                                 stream=True, 
                                 timeout=REQUEST_TIMEOUT)
            
            if response.status_code not in [200, 206]:  # OK or Partial Content
                continue
            
            mode = 'ab' if 'Range' in headers else 'wb'
            with open(temp_path, mode) as f:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    pause_event.wait()
                    if stop_event.is_set():
                        return None, filename
                    if chunk:
                        f.write(chunk)
            
            # Validate file
            if os.path.getsize(temp_path) >= MIN_PDF_SIZE:
                os.rename(temp_path, path)
                return title, filename
            
            # File too small, remove and retry
            if os.path.exists(temp_path):
                os.remove(temp_path)
                
        except Exception:
            # Clean up on error
            if os.path.exists(temp_path) and attempt == MAX_RETRIES:
                try:
                    os.remove(temp_path)
                except:
                    pass
            continue
    
    return None, filename

class OptimizedDownloader:
    def __init__(self, root):
        self.root = root
        root.title("⚡ Ultra-Fast Research Paper Downloader")
        root.geometry("920x650")
        
        # State variables
        self.running = False
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.done_called = False
        self.downloaded_count = 0
        self.failed_count = 0
        self.start_time = None
        self.search_worker = None
        
        # Performance tracking
        self.performance_queue = queue.Queue()
        self.downloaded_files = set()
        
        # Setup UI
        self.setup_ui()
        self.setup_keyboard_shortcuts()
        
        # Start performance monitor
        threading.Thread(target=self.performance_monitor, daemon=True).start()
        
        # Handle window close
        root.protocol("WM_DELETE_WINDOW", self.on_window_close)

    def setup_ui(self):
        """Create optimized UI"""
        main_frame = tk.Frame(self.root, padx=20, pady=20)
        main_frame.pack(fill="both", expand=True)
        
        # Header
        header = tk.Label(main_frame, 
                         text="⚡ ULTRA-FAST AI Paper Downloader", 
                         font=("Arial", 18, "bold"))
        header.pack(pady=(0, 15))
        
        # Input frame
        input_frame = tk.Frame(main_frame)
        input_frame.pack(fill="x", pady=(0, 15))
        
        tk.Label(input_frame, text="Research Keyword:", 
                font=("Arial", 11)).grid(row=0, column=0, sticky="w", pady=5)
        self.keyword_entry = tk.Entry(input_frame, width=70, font=("Arial", 10))
        self.keyword_entry.grid(row=0, column=1, padx=(10, 0), pady=5)
        self.keyword_entry.focus_set()
        
        tk.Label(input_frame, text="Number of PDFs:", 
                font=("Arial", 11)).grid(row=1, column=0, sticky="w", pady=5)
        self.count_entry = tk.Entry(input_frame, width=15, font=("Arial", 10))
        self.count_entry.grid(row=1, column=1, sticky="w", padx=(10, 0), pady=5)
        
        # Stats frame
        stats_frame = tk.Frame(main_frame)
        stats_frame.pack(fill="x", pady=(10, 5))
        
        self.stats_label = tk.Label(stats_frame, 
                                   text="Ready | Speed: 0 PDFs/min | ETA: --", 
                                   font=("Arial", 9))
        self.stats_label.pack()
        
        # Progress section
        progress_frame = tk.Frame(main_frame)
        progress_frame.pack(fill="x", pady=(5, 10))
        
        self.progress_bar = ttk.Progressbar(progress_frame, length=860, mode="determinate")
        self.progress_bar.pack()
        
        self.status_label = tk.Label(progress_frame, 
                                    text="Progress: 0/0 (0 failed)", 
                                    font=("Arial", 10, "bold"))
        self.status_label.pack(pady=(5, 0))
        
        # Control buttons
        button_frame = tk.Frame(main_frame)
        button_frame.pack(pady=(10, 15))
        
        self.start_button = tk.Button(button_frame, 
                                     text="🚀 START (Enter)", 
                                     command=self.start_download,
                                     bg="#4CAF50", fg="white",
                                     font=("Arial", 10, "bold"),
                                     width=15, height=2)
        self.pause_button = tk.Button(button_frame, 
                                     text="⏸ PAUSE (Space)", 
                                     command=self.pause_download,
                                     bg="#FF9800", fg="white",
                                     font=("Arial", 10),
                                     width=15, height=2,
                                     state="disabled")
        self.resume_button = tk.Button(button_frame, 
                                      text="▶ RESUME (Space)", 
                                      command=self.resume_download,
                                      bg="#2196F3", fg="white",
                                      font=("Arial", 10),
                                      width=15, height=2,
                                      state="disabled")
        self.end_button = tk.Button(button_frame, 
                                   text="⏹ STOP (Esc)", 
                                   command=self.end_download,
                                   bg="#F44336", fg="white",
                                   font=("Arial", 10, "bold"),
                                   width=15, height=2,
                                   state="disabled")
        
        self.start_button.grid(row=0, column=0, padx=5)
        self.pause_button.grid(row=0, column=1, padx=5)
        self.resume_button.grid(row=0, column=2, padx=5)
        self.end_button.grid(row=0, column=3, padx=5)
        
        # Log display with tabs
        notebook = ttk.Notebook(main_frame)
        notebook.pack(fill="both", expand=True, pady=(10, 0))
        
        # Download log tab
        log_frame = tk.Frame(notebook)
        notebook.add(log_frame, text="📝 Download Log")
        
        self.log_text = tk.Text(log_frame, height=12, font=("Consolas", 9), wrap="word")
        self.log_text.pack(fill="both", expand=True, side="left")
        
        log_scrollbar = tk.Scrollbar(log_frame)
        log_scrollbar.pack(side="right", fill="y")
        self.log_text.config(yscrollcommand=log_scrollbar.set)
        log_scrollbar.config(command=self.log_text.yview)
        
        # Performance tab
        perf_frame = tk.Frame(notebook)
        notebook.add(perf_frame, text="📊 Performance")
        
        self.perf_text = tk.Text(perf_frame, height=12, font=("Consolas", 9), wrap="word")
        self.perf_text.pack(fill="both", expand=True, side="left")
        
        perf_scrollbar = tk.Scrollbar(perf_frame)
        perf_scrollbar.pack(side="right", fill="y")
        self.perf_text.config(yscrollcommand=perf_scrollbar.set)
        perf_scrollbar.config(command=self.perf_text.yview)

    def setup_keyboard_shortcuts(self):
        """Setup keyboard shortcuts"""
        self.keyword_entry.bind("<Return>", lambda e: self.count_entry.focus_set())
        self.count_entry.bind("<Return>", lambda e: self.start_download())
        self.root.bind("<space>", self.toggle_pause_resume)
        self.root.bind("<Escape>", lambda e: self.end_download())
        self.root.after(100, lambda: self.keyword_entry.focus_set())

    def log_message(self, message, level="INFO"):
        """Add message to log"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log_text.see(tk.END)
        self.root.update_idletasks()

    def perf_message(self, message):
        """Add message to performance log"""
        self.perf_text.insert(tk.END, f"{message}\n")
        self.perf_text.see(tk.END)
        self.root.update_idletasks()

    def performance_monitor(self):
        """Monitor and report performance metrics"""
        while True:
            if self.running and self.start_time:
                elapsed = time.time() - self.start_time
                if elapsed > 0:
                    speed = (self.downloaded_count / elapsed) * 60  # PDFs per minute
                    remaining = self.required_count - self.downloaded_count
                    if speed > 0:
                        eta_seconds = remaining / (speed / 60)
                        eta_str = time.strftime("%H:%M:%S", time.gmtime(eta_seconds))
                    else:
                        eta_str = "--:--:--"
                    
                    self.stats_label.config(
                        text=f"Speed: {speed:.1f} PDFs/min | "
                             f"Downloaded: {self.downloaded_count} | "
                             f"Failed: {self.failed_count} | "
                             f"ETA: {eta_str}"
                    )
            
            time.sleep(2)  # Update every 2 seconds

    def update_progress(self):
        """Update progress display"""
        self.progress_bar["value"] = self.downloaded_count
        self.progress_bar["maximum"] = self.required_count
        self.status_label.config(
            text=f"Progress: {self.downloaded_count}/{self.required_count} "
                 f"({self.failed_count} failed)"
        )

    def start_download(self):
        """Start optimized download process"""
        if self.running:
            return
        
        # Validate inputs
        keyword = self.keyword_entry.get().strip()
        count_text = self.count_entry.get().strip()
        
        if not keyword:
            messagebox.showerror("Error", "Please enter a research keyword")
            self.keyword_entry.focus_set()
            return
        
        if not count_text.isdigit() or int(count_text) <= 0:
            messagebox.showerror("Error", "Please enter a valid number of PDFs")
            self.count_entry.focus_set()
            return
        
        self.required_count = int(count_text)
        
        # Reset state
        self.running = True
        self.done_called = False
        self.stop_event.clear()
        self.pause_event.set()
        self.downloaded_count = 0
        self.failed_count = 0
        self.start_time = time.time()
        self.downloaded_files.clear()
        
        # Setup folders
        self.base_folder = clean_name(keyword)
        self.pdf_folder = os.path.join(self.base_folder, "pdfs")
        os.makedirs(self.pdf_folder, exist_ok=True)
        
        # Count existing files
        existing = len([f for f in os.listdir(self.pdf_folder) if f.endswith('.pdf')])
        if existing > 0:
            self.downloaded_count = existing
            self.log_message(f"Found {existing} existing PDFs, resuming...")
        
        # Reset UI
        self.progress_bar["value"] = self.downloaded_count
        self.update_progress()
        
        self.log_text.delete(1.0, tk.END)
        self.perf_text.delete(1.0, tk.END)
        
        self.log_message("="*60)
        self.log_message("🚀 ULTRA-FAST DOWNLOAD STARTED")
        self.log_message(f"Keyword: {keyword}")
        self.log_message(f"Target: {self.required_count} PDFs")
        self.log_message(f"Parallel workers: {MAX_WORKERS}")
        self.log_message(f"Batch size: {ARXIV_BATCH}")
        self.log_message("="*60)
        
        # Disable/enable buttons
        self.keyword_entry.config(state="disabled")
        self.count_entry.config(state="disabled")
        self.start_button.config(state="disabled")
        self.pause_button.config(state="normal")
        self.end_button.config(state="normal")
        
        # Start search and download threads
        threading.Thread(target=self.optimized_download_process, daemon=True).start()

    def optimized_download_process(self):
        """Optimized download process with parallel search and download"""
        result_queue = queue.Queue(maxsize=MAX_WORKERS * 2)
        
        # Start search worker
        self.search_worker = ArxivSearchWorker(
            self.keyword_entry.get(),
            result_queue,
            self.stop_event,
            start_index=self.downloaded_count
        )
        self.search_worker.start()
        
        # Create session for all downloads
        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        
        # Use ThreadPoolExecutor for parallel downloads
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = []
            
            while (self.downloaded_count < self.required_count and 
                   not self.stop_event.is_set()):
                
                # Submit new download tasks
                while (len(futures) < MAX_WORKERS * 2 and 
                       self.downloaded_count + len(futures) < self.required_count):
                    
                    try:
                        entry = result_queue.get(timeout=5)
                        future = executor.submit(
                            download_pdf_with_retry,
                            entry,
                            self.pdf_folder,
                            self.stop_event,
                            self.pause_event,
                            session
                        )
                        futures.append(future)
                    except queue.Empty:
                        # No more search results
                        if not self.search_worker.is_alive():
                            break
                        continue
                
                # Process completed downloads
                for future in as_completed(futures[:]):
                    if self.stop_event.is_set():
                        break
                    
                    try:
                        title, filename = future.result(timeout=1)
                        if title:
                            self.downloaded_count += 1
                            self.root.after(0, self.on_download_success, title)
                        else:
                            self.failed_count += 1
                            self.root.after(0, self.on_download_failed, filename)
                    
                    except Exception as e:
                        self.failed_count += 1
                    
                    # Remove processed future
                    futures.remove(future)
                    
                    # Check if we're done
                    if self.downloaded_count >= self.required_count:
                        break
                
                # Small delay to prevent CPU spinning
                time.sleep(0.1)
        
        # Cleanup
        self.stop_event.set()
        if self.search_worker and self.search_worker.is_alive():
            self.search_worker.join(timeout=1)
        
        # Finalize
        if not self.stop_event.is_set() or self.downloaded_count >= self.required_count:
            self.root.after(0, self.on_download_complete)

    def on_download_success(self, title):
        """Handle successful download"""
        self.log_message(f"✓ {title[:70]}...")
        self.update_progress()

    def on_download_failed(self, filename):
        """Handle failed download"""
        self.perf_message(f"✗ Failed: {filename}")

    def on_download_complete(self):
        """Handle completion"""
        if self.done_called:
            return
        
        self.done_called = True
        self.running = False
        elapsed = time.time() - self.start_time
        
        self.log_message("="*60)
        self.log_message("✅ DOWNLOAD COMPLETE!")
        self.log_message(f"Total time: {elapsed:.1f} seconds")
        self.log_message(f"Downloaded: {self.downloaded_count} PDFs")
        self.log_message(f"Failed: {self.failed_count}")
        self.log_message(f"Average speed: {self.downloaded_count/elapsed*60:.1f} PDFs/min")
        self.log_message("="*60)
        self.log_message("📁 Opening folder...")
        
        # Update UI
        self.keyword_entry.config(state="normal")
        self.count_entry.config(state="normal")
        self.start_button.config(state="normal")
        self.pause_button.config(state="disabled")
        self.resume_button.config(state="disabled")
        self.end_button.config(state="disabled")
        
        # Open folder
        try:
            folder_path = os.path.abspath(self.base_folder)
            if sys.platform == "win32":
                os.startfile(folder_path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder_path])
            else:
                subprocess.Popen(["xdg-open", folder_path])
        except:
            pass

    def toggle_pause_resume(self, event=None):
        """Toggle pause/resume"""
        if not self.running:
            return
        
        if self.pause_event.is_set():
            self.pause_download()
        else:
            self.resume_download()

    def pause_download(self):
        """Pause downloads"""
        if not self.running:
            return
        
        self.pause_event.clear()
        self.log_message("⏸️ DOWNLOADS PAUSED")
        self.pause_button.config(state="disabled")
        self.resume_button.config(state="normal")

    def resume_download(self):
        """Resume downloads"""
        if not self.running:
            return
        
        self.pause_event.set()
        self.log_message("▶️ DOWNLOADS RESUMED")
        self.resume_button.config(state="disabled")
        self.pause_button.config(state="normal")

    def end_download(self):
        """Stop all downloads"""
        if not self.running:
            return
        
        self.stop_event.set()
        self.pause_event.set()
        self.running = False
        
        # Cleanup partial files
        if hasattr(self, 'pdf_folder') and os.path.exists(self.pdf_folder):
            for file in os.listdir(self.pdf_folder):
                if file.endswith(".part"):
                    try:
                        os.remove(os.path.join(self.pdf_folder, file))
                    except:
                        pass
        
        self.log_message("\n🛑 STOPPED BY USER")
        
        # Update UI
        self.keyword_entry.config(state="normal")
        self.count_entry.config(state="normal")
        self.start_button.config(state="normal")
        self.pause_button.config(state="disabled")
        self.resume_button.config(state="disabled")
        self.end_button.config(state="disabled")

    def on_window_close(self):
        """Handle window close"""
        if self.running:
            self.end_download()
        self.root.destroy()

# ================= MAIN =================
if __name__ == "__main__":
    # Check for required packages
    required_packages = ['feedparser', 'requests']
    missing = []
    for package in required_packages:
        try:
            __import__(package)
        except ImportError:
            missing.append(package)
    
    if missing:
        print(f"Missing packages: {', '.join(missing)}")
        print("Install with: pip install feedparser requests")
        sys.exit(1)
    
    # Create and run application
    root = tk.Tk()
    app = OptimizedDownloader(root)
    
    # Center window
    root.update_idletasks()
    width = root.winfo_width()
    height = root.winfo_height()
    x = (root.winfo_screenwidth() // 2) - (width // 2)
    y = (root.winfo_screenheight() // 2) - (height // 2)
    root.geometry(f'{width}x{height}+{x}+{y}')
    
    root.mainloop()