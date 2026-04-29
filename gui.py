"""
gui.py — ExifGhost arayüzü
CustomTkinter + TkinterDnD2 | Koyu tema | Sürükle-bırak | Thread'li temizleme
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

try:
    import tkinterdnd2 as tkdnd
    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False

from logic import clean_file, is_supported, CleanResult

# ─── Design tokens ────────────────────────────────────────────────────────────
BG      = "#0d0d1a"
PANEL   = "#12122a"
SURFACE = "#1a1a38"
BORDER  = "#2a2a55"
ACCENT  = "#7c3aed"
ACCENT2 = "#06b6d4"
SUCCESS = "#10b981"
ERROR   = "#ef4444"
TEXT    = "#e2e8f0"
MUTED   = "#64748b"
WHITE   = "#ffffff"

TYPE_ICONS = {
    ".jpg": "🖼", ".jpeg": "🖼", ".png": "🖼",
    ".mp3": "🎵", ".flac": "🎵", ".ogg": "🎵", ".m4a": "🎵", ".wav": "🎵",
    ".mp4": "🎬", ".mov": "🎬", ".m4v": "🎬",
}

STATUS_ICONS = {
    "pending":  "⏳",
    "cleaning": "🔄",
    "success":  "✅",
    "skipped":  "⏭",
    "error":    "❌",
}

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


# ─── FileRow ──────────────────────────────────────────────────────────────────
class FileRow(ctk.CTkFrame):
    """Tek bir dosyayı listede gösteren satır widget'ı."""

    def __init__(self, master, index: int, path: Path, **kwargs):
        super().__init__(master, fg_color=SURFACE, corner_radius=8, **kwargs)
        self.path = path

        ext      = path.suffix.lower()
        icon     = TYPE_ICONS.get(ext, "📄")
        st       = path.stat()
        size_str = (f"{st.st_size / 1_048_576:.1f} MB"
                    if st.st_size >= 1_048_576
                    else f"{st.st_size // 1024} KB")

        ctk.CTkLabel(self, text=f"{index:>2}", text_color=MUTED,
                     width=28, font=("Consolas", 11)).pack(side="left", padx=(10, 4))

        ctk.CTkLabel(self, text=f"{icon}  {path.name}", text_color=TEXT,
                     font=("Segoe UI", 12), anchor="w").pack(side="left", padx=4, fill="x", expand=True)

        ctk.CTkLabel(self, text=size_str, text_color=MUTED,
                     width=68, font=("Segoe UI", 11)).pack(side="left", padx=4)

        badge = ctk.CTkLabel(self, text=ext.upper().lstrip("."),
                             fg_color=ACCENT, text_color=WHITE,
                             corner_radius=4, width=42, font=("Segoe UI", 10, "bold"))
        badge.pack(side="left", padx=4)

        self._status_var = tk.StringVar(value=STATUS_ICONS["pending"])
        self._status_lbl = ctk.CTkLabel(self, textvariable=self._status_var,
                                         width=32, font=("Segoe UI", 14))
        self._status_lbl.pack(side="left", padx=(4, 10))

    def set_status(self, key: str) -> None:
        self._status_var.set(STATUS_ICONS.get(key, "❓"))
        colors = {"success": "#0d2818", "error": "#2d0d0d", "cleaning": "#1a1040"}
        self.configure(fg_color=colors.get(key, SURFACE))


# ─── DropZone ─────────────────────────────────────────────────────────────────
class DropZone(ctk.CTkFrame):
    """Animasyonlu sürükle-bırak alanı."""

    _COLORS = [BORDER, ACCENT, ACCENT2, ACCENT, BORDER]  # pulse cycle

    def __init__(self, master, on_files_cb, **kwargs):
        super().__init__(master, fg_color=PANEL, corner_radius=16,
                         border_width=2, border_color=BORDER, **kwargs)
        self._cb        = on_files_cb
        self._pulse_idx = 0
        self._anim_id   = None

        # İç içerik
        inner = ctk.CTkFrame(self, fg_color="transparent")
        inner.place(relx=0.5, rely=0.5, anchor="center")

        ctk.CTkLabel(inner, text="📂", font=("Segoe UI", 40)).pack()
        ctk.CTkLabel(inner, text="Dosyaları buraya sürükle",
                     font=("Segoe UI", 15, "bold"), text_color=TEXT).pack(pady=(4, 0))
        ctk.CTkLabel(inner, text="JPG · PNG · MP3 · FLAC · OGG · M4A · WAV · MP4 · MOV",
                     font=("Segoe UI", 10), text_color=MUTED).pack()

        sel_btn = ctk.CTkButton(inner, text="veya dosya seç",
                                fg_color="transparent", hover_color=SURFACE,
                                text_color=ACCENT2, font=("Segoe UI", 12, "underline"),
                                command=self._open_dialog)
        sel_btn.pack(pady=(8, 0))

        # Hover efektleri
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", lambda _: self._open_dialog())

        # Drag & drop
        if DND_AVAILABLE:
            self.drop_target_register(tkdnd.DND_FILES)        # type: ignore[attr-defined]
            self.dnd_bind("<<Drop>>", self._on_drop)           # type: ignore[attr-defined]

    # ── Animasyon ──
    def _on_enter(self, _=None):
        self._stop_pulse()
        self._pulse_idx = 0
        self._pulse()

    def _on_leave(self, _=None):
        self._stop_pulse()
        self.configure(border_color=BORDER)

    def _pulse(self):
        color = self._COLORS[self._pulse_idx % len(self._COLORS)]
        self.configure(border_color=color)
        self._pulse_idx += 1
        self._anim_id = self.after(220, self._pulse)

    def _stop_pulse(self):
        if self._anim_id:
            self.after_cancel(self._anim_id)
            self._anim_id = None

    # ── Dosya seçimi ──
    def _open_dialog(self):
        exts = ("*.jpg *.jpeg *.png *.mp3 *.flac *.ogg "
                "*.m4a *.wav *.mp4 *.mov *.m4v")
        paths = filedialog.askopenfilenames(
            title="Dosya Seç",
            filetypes=[("Desteklenen dosyalar", exts), ("Tümü", "*.*")],
        )
        if paths:
            self._cb([Path(p) for p in paths])

    def _on_drop(self, event):
        # tkinterdnd2 bize string döner; boşluklu path'ler {} içinde gelir
        raw: str = event.data
        paths: list[Path] = []
        i = 0
        while i < len(raw):
            if raw[i] == "{":
                end = raw.index("}", i)
                paths.append(Path(raw[i + 1:end]))
                i = end + 2
            else:
                token = raw[i:].split()[0]
                paths.append(Path(token))
                i += len(token) + 1
        self._cb(paths)


# ─── Ana Pencere ──────────────────────────────────────────────────────────────
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        if DND_AVAILABLE:
            tkdnd.TkinterDnD._require(self)     # type: ignore[attr-defined]

        self.files:      list[Path]     = []
        self.file_rows:  list[FileRow]  = []
        self._queue:     queue.Queue    = queue.Queue()
        self._cleaning   = False

        self._build_window()
        self._build_ui()

    # ── Pencere ayarları ──
    def _build_window(self):
        self.title("ExifGhost — Metadata Temizleyici")
        self.geometry("860x660")
        self.minsize(700, 560)
        self.configure(fg_color=BG)
        try:
            self.iconbitmap("assets/icon.ico")
        except Exception:
            pass

    # ── UI inşası ──
    def _build_ui(self):
        # ── Header ──
        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.pack(fill="x", padx=24, pady=(22, 0))

        ctk.CTkLabel(hdr, text="👻", font=("Segoe UI", 30)).pack(side="left")
        ctk.CTkLabel(hdr, text="ExifGhost",
                     font=("Segoe UI", 26, "bold"), text_color=ACCENT).pack(side="left", padx=(6, 0))
        ctk.CTkLabel(hdr, text="— Metadata Temizleyici",
                     font=("Segoe UI", 14), text_color=MUTED).pack(side="left", padx=(8, 0), pady=(6, 0))

        # Sağ üst: tema toggle
        self._theme_btn = ctk.CTkButton(
            hdr, text="☀ Açık", width=90, fg_color=SURFACE, hover_color=BORDER,
            text_color=MUTED, font=("Segoe UI", 11), command=self._toggle_theme)
        self._theme_btn.pack(side="right")

        # ── Drop zone ──
        self._drop = DropZone(self, on_files_cb=self.add_files, height=145)
        self._drop.pack(fill="x", padx=24, pady=16)

        # ── Liste başlığı ──
        lhdr = ctk.CTkFrame(self, fg_color="transparent")
        lhdr.pack(fill="x", padx=24)
        ctk.CTkLabel(lhdr, text="Dosya Listesi",
                     font=("Segoe UI", 13, "bold"), text_color=TEXT).pack(side="left")
        self._count_lbl = ctk.CTkLabel(lhdr, text="(0 dosya)",
                                        font=("Segoe UI", 12), text_color=MUTED)
        self._count_lbl.pack(side="left", padx=8)
        ctk.CTkButton(lhdr, text="Tümünü Kaldır", width=120,
                      fg_color="transparent", hover_color=SURFACE,
                      text_color=MUTED, font=("Segoe UI", 11),
                      command=self._clear_list).pack(side="right")

        # ── Scrollable liste ──
        self._list_frame = ctk.CTkScrollableFrame(
            self, fg_color=PANEL, corner_radius=12, height=230)
        self._list_frame.pack(fill="both", expand=True, padx=24, pady=(6, 0))

        self._empty_lbl = ctk.CTkLabel(
            self._list_frame,
            text="Henüz dosya eklenmedi.\nYukarıdan sürükle veya 'dosya seç'e tıkla.",
            font=("Segoe UI", 13), text_color=MUTED)
        self._empty_lbl.pack(pady=50)

        # ── Progress ──
        prog_f = ctk.CTkFrame(self, fg_color="transparent")
        prog_f.pack(fill="x", padx=24, pady=(10, 0))

        self._prog_bar = ctk.CTkProgressBar(prog_f, fg_color=SURFACE,
                                             progress_color=ACCENT, height=8,
                                             corner_radius=4)
        self._prog_bar.pack(fill="x")
        self._prog_bar.set(0)

        self._prog_lbl = ctk.CTkLabel(prog_f, text="",
                                       font=("Segoe UI", 11), text_color=MUTED)
        self._prog_lbl.pack(anchor="e", pady=(2, 0))

        # ── Butonlar ──
        btn_f = ctk.CTkFrame(self, fg_color="transparent")
        btn_f.pack(fill="x", padx=24, pady=10)

        self._clean_btn = ctk.CTkButton(
            btn_f, text="🧹  Temizle", width=170, height=42,
            fg_color=ACCENT, hover_color="#6d28d9",
            text_color=WHITE, font=("Segoe UI", 14, "bold"),
            command=self._start_cleaning)
        self._clean_btn.pack(side="right")

        ctk.CTkButton(
            btn_f, text="Çıkış", width=90, height=42,
            fg_color=SURFACE, hover_color=BORDER,
            text_color=MUTED, font=("Segoe UI", 13),
            command=self.destroy).pack(side="left")

        # ── Status bar ──
        sb = ctk.CTkFrame(self, fg_color=PANEL, height=28, corner_radius=0)
        sb.pack(fill="x", side="bottom")
        self._status_var = tk.StringVar(value="Hazır  •  Dosya bekleniyor")
        ctk.CTkLabel(sb, textvariable=self._status_var,
                     font=("Segoe UI", 11), text_color=MUTED).pack(side="left", padx=14, pady=4)

        # Versiyon
        ctk.CTkLabel(sb, text="v1.0.0", font=("Segoe UI", 10),
                     text_color=MUTED).pack(side="right", padx=14)

    # ── Dosya yönetimi ──
    def add_files(self, paths: list[Path]) -> None:
        added = 0
        skipped_fmt = 0
        for p in paths:
            if not is_supported(p):
                skipped_fmt += 1
                continue
            if p in self.files:
                continue
            self.files.append(p)
            self._add_row(p)
            added += 1

        self._refresh_state()
        msg = f"{added} dosya eklendi"
        if skipped_fmt:
            msg += f"  •  {skipped_fmt} desteklenmeyen atlandı"
        self._set_status(msg)

    def _add_row(self, path: Path) -> None:
        if len(self.files) == 1:
            self._empty_lbl.pack_forget()
        row = FileRow(self._list_frame, len(self.files), path)
        row.pack(fill="x", padx=6, pady=3)
        self.file_rows.append(row)

    def _clear_list(self) -> None:
        if self._cleaning:
            return
        self.files.clear()
        self.file_rows.clear()
        for w in self._list_frame.winfo_children():
            w.destroy()
        self._empty_lbl = ctk.CTkLabel(
            self._list_frame,
            text="Henüz dosya eklenmedi.\nYukarıdan sürükle veya 'dosya seç'e tıkla.",
            font=("Segoe UI", 13), text_color=MUTED)
        self._empty_lbl.pack(pady=50)
        self._prog_bar.set(0)
        self._prog_lbl.configure(text="")
        self._refresh_state()
        self._set_status("Liste temizlendi")

    def _refresh_state(self) -> None:
        n = len(self.files)
        self._count_lbl.configure(text=f"({n} dosya)")
        self._clean_btn.configure(
            state="normal" if n > 0 and not self._cleaning else "disabled")

    def _set_status(self, msg: str) -> None:
        self._status_var.set(msg)

    # ── Temizleme — thread ──
    def _start_cleaning(self) -> None:
        if not self.files or self._cleaning:
            return
        self._cleaning = True
        self._clean_btn.configure(state="disabled", text="⏳  Temizleniyor…")
        self._prog_bar.set(0)

        for row in self.file_rows:
            row.set_status("pending")

        t = threading.Thread(target=self._worker, daemon=True)
        t.start()
        self.after(100, self._poll_queue)

    def _worker(self) -> None:
        total = len(self.files)
        for idx, path in enumerate(self.files):
            self._queue.put(("progress", idx, None))
            result = clean_file(path)
            self._queue.put(("result", idx, result))
        self._queue.put(("done", total, None))

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, idx, payload = self._queue.get_nowait()

                if kind == "progress":
                    self.file_rows[idx].set_status("cleaning")

                elif kind == "result":
                    result: CleanResult = payload
                    self.file_rows[idx].set_status(result.status)

                elif kind == "done":
                    self._on_cleaning_done(idx)  # idx = total burada
                    return
        except queue.Empty:
            pass
        self.after(80, self._poll_queue)

    def _on_cleaning_done(self, total: int) -> None:
        self._cleaning = False
        self._clean_btn.configure(state="normal", text="🧹  Temizle")

        results = [r for r in
                   (clean_file.__wrapped__ if hasattr(clean_file, "__wrapped__") else [])
                   ]  # placeholder; gerçek sonuçlar row'larda

        success = sum(1 for r in self.file_rows if r._status_var.get() == STATUS_ICONS["success"])
        errors  = sum(1 for r in self.file_rows if r._status_var.get() == STATUS_ICONS["error"])

        self._prog_bar.set(1.0)
        self._prog_lbl.configure(text=f"{total} dosya tamamlandı")
        self._set_status(f"✅ {success} başarılı  •  ❌ {errors} hata  •  {total} toplam")

        # Özet dialog
        msg = (f"İşlem tamamlandı!\n\n"
               f"✅  Başarılı  : {success}\n"
               f"❌  Hata       : {errors}\n"
               f"📁  Toplam    : {total}\n\n"
               f"Temizlenen dosyalar '_cleaned' son ekiyle\n"
               f"orijinalin yanına kaydedildi.")
        messagebox.showinfo("ExifGhost — Tamamlandı", msg)

        # Progress bar'ı gerçek zamanlı güncelle
        self._update_progress_bar()

    def _update_progress_bar(self) -> None:
        done = sum(1 for r in self.file_rows
                   if r._status_var.get() in (STATUS_ICONS["success"],
                                               STATUS_ICONS["error"],
                                               STATUS_ICONS["skipped"]))
        total = len(self.file_rows)
        if total:
            self._prog_bar.set(done / total)
            self._prog_lbl.configure(text=f"{done} / {total} dosya")

    # ── Tema toggle ──
    def _toggle_theme(self) -> None:
        current = ctk.get_appearance_mode()
        if current == "Dark":
            ctk.set_appearance_mode("light")
            self._theme_btn.configure(text="🌙 Koyu")
        else:
            ctk.set_appearance_mode("dark")
            self._theme_btn.configure(text="☀ Açık")
