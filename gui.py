"""tkinter GUI for scan-pdf-cleanup."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from PIL import Image, ImageTk

import cleanup
import i18n
from cleanup import Options
from i18n import t

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    _HAS_DND = True
except Exception:  # pragma: no cover - optional dependency
    _HAS_DND = False

PREVIEW_W, PREVIEW_H = 330, 440


class App:
    def __init__(self, initial_files: list[str] | None = None) -> None:
        self.root = TkinterDnD.Tk() if _HAS_DND else tk.Tk()
        self.root.geometry("1040x760")
        self.root.minsize(900, 640)

        self.files: list[str] = []
        self.passwords: dict[str, str] = {}
        self.page_counts: dict[str, int] = {}
        self.outputs: list[str] = []
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.preview_job: str | None = None
        self.preview_gen = 0
        self._photos: list[ImageTk.PhotoImage] = []
        self._texts: list[tuple[tk.Misc, str, str]] = []

        settings = i18n.load_settings()
        saved = settings.get("options", {}) if isinstance(settings.get("options"), dict) else {}
        self.var_whiten_auto = tk.BooleanVar(value=saved.get("whiten", cleanup.WHITEN_AUTO) == cleanup.WHITEN_AUTO)
        self.var_whiten = tk.IntVar(value=saved.get("whiten", 30) if saved.get("whiten", -1) >= 0 else 30)
        self.var_contrast = tk.StringVar(value=saved.get("contrast", "mid"))
        self.var_mode = tk.StringVar(value=saved.get("mode", "gray"))
        self.var_deskew = tk.BooleanVar(value=bool(saved.get("deskew", True)))
        self.var_dpi = tk.IntVar(value=saved.get("dpi", 200))
        self.var_lang = tk.StringVar(value=i18n.LANG_NAMES[i18n.current_lang()])

        self._build()
        self._apply_texts()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        if initial_files:
            self.root.after(100, lambda: self.add_paths(initial_files))

    # ------------------------------------------------------------ building
    def _reg(self, widget: tk.Misc, key: str, attr: str = "text") -> tk.Misc:
        self._texts.append((widget, key, attr))
        return widget

    def _build(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        # ---- header
        head = ttk.Frame(root, padding=(12, 10, 12, 4))
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)
        self.lbl_title = ttk.Label(head, font=("", 15, "bold"))
        self._reg(self.lbl_title, "app_title")
        self.lbl_title.grid(row=0, column=0, sticky="w")
        self._reg(ttk.Label(head), "lbl_language").grid(row=0, column=1, padx=(0, 6))
        self.cmb_lang = ttk.Combobox(
            head, state="readonly", width=10, textvariable=self.var_lang,
            values=[i18n.LANG_NAMES[c] for c in i18n.LANGS],
        )
        self.cmb_lang.grid(row=0, column=2)
        self.cmb_lang.bind("<<ComboboxSelected>>", self._on_lang)

        # ---- files
        files = ttk.LabelFrame(root, padding=8)
        self._reg(files, "lbl_files")
        files.grid(row=1, column=0, sticky="ew", padx=12, pady=4)
        files.columnconfigure(0, weight=1)
        self.lbl_drop = ttk.Label(files, anchor="center", relief="groove", padding=6, foreground="#555")
        self._reg(self.lbl_drop, "drop_hint")
        self.lbl_drop.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.lst_files = tk.Listbox(files, height=4, activestyle="none")
        self.lst_files.grid(row=1, column=0, sticky="nsew")
        btns = ttk.Frame(files)
        btns.grid(row=1, column=1, sticky="ns", padx=(8, 0))
        self.btn_add = self._reg(ttk.Button(btns, command=self._add_files_dialog), "btn_add_files")
        self.btn_add.pack(fill="x")
        self.btn_add_dir = self._reg(ttk.Button(btns, command=self._add_folder_dialog), "btn_add_folder")
        self.btn_add_dir.pack(fill="x", pady=4)
        self.btn_clear = self._reg(ttk.Button(btns, command=self.clear_files), "btn_clear")
        self.btn_clear.pack(fill="x")
        if _HAS_DND:
            for w in (root, self.lbl_drop, self.lst_files):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self._on_drop)

        # ---- middle: options | preview
        mid = ttk.Frame(root)
        mid.grid(row=2, column=0, sticky="nsew", padx=12, pady=4)
        mid.columnconfigure(1, weight=1)
        mid.rowconfigure(0, weight=1)

        opts = ttk.Frame(mid)
        opts.grid(row=0, column=0, sticky="nsw", padx=(0, 12))

        f_whiten = self._reg(ttk.LabelFrame(opts, padding=6), "opt_whiten")
        f_whiten.pack(fill="x", pady=(0, 6))
        self.chk_auto = self._reg(
            ttk.Checkbutton(f_whiten, variable=self.var_whiten_auto, command=self._on_whiten_auto), "whiten_auto")
        self.chk_auto.grid(row=0, column=0, sticky="w")
        self.lbl_whiten_val = ttk.Label(f_whiten, width=4, anchor="e")
        self.lbl_whiten_val.grid(row=0, column=1, sticky="e")
        self.scl_whiten = ttk.Scale(f_whiten, from_=0, to=100, orient="horizontal", length=200,
                                    variable=self.var_whiten, command=self._on_whiten_slide)
        self.scl_whiten.grid(row=1, column=0, columnspan=2, sticky="ew")

        f_contrast = self._reg(ttk.LabelFrame(opts, padding=6), "opt_contrast")
        f_contrast.pack(fill="x", pady=(0, 6))
        for i, lvl in enumerate(cleanup.CONTRAST_LEVELS):
            rb = ttk.Radiobutton(f_contrast, variable=self.var_contrast, value=lvl, command=self._schedule_preview)
            self._reg(rb, f"contrast_{lvl}")
            rb.grid(row=0, column=i, padx=(0, 10), sticky="w")

        f_mode = self._reg(ttk.LabelFrame(opts, padding=6), "opt_mode")
        f_mode.pack(fill="x", pady=(0, 6))
        for i, m in enumerate(cleanup.MODES):
            rb = ttk.Radiobutton(f_mode, variable=self.var_mode, value=m, command=self._schedule_preview)
            self._reg(rb, f"mode_{m}")
            rb.grid(row=i, column=0, sticky="w")

        self.chk_deskew = self._reg(
            ttk.Checkbutton(opts, variable=self.var_deskew, command=self._schedule_preview), "opt_deskew")
        self.chk_deskew.pack(anchor="w", pady=(0, 6))

        f_dpi = self._reg(ttk.LabelFrame(opts, padding=6), "opt_dpi")
        f_dpi.pack(fill="x", pady=(0, 6))
        for i, d in enumerate(cleanup.DPIS):
            ttk.Radiobutton(f_dpi, text=f"{d} dpi", variable=self.var_dpi, value=d,
                            command=self._schedule_preview).grid(row=0, column=i, padx=(0, 8))
        self.lbl_est = ttk.Label(f_dpi)
        self.lbl_est.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))

        prev = ttk.Frame(mid)
        prev.grid(row=0, column=1, sticky="nsew")
        prev.columnconfigure(0, weight=1)
        prev.columnconfigure(1, weight=1)
        prev.rowconfigure(1, weight=1)
        self._reg(ttk.Label(prev, anchor="center"), "preview_original").grid(row=0, column=0, sticky="ew")
        self._reg(ttk.Label(prev, anchor="center"), "preview_result").grid(row=0, column=1, sticky="ew")
        self.cv_orig = tk.Canvas(prev, bg="#d9d9d9", highlightthickness=1, highlightbackground="#999")
        self.cv_res = tk.Canvas(prev, bg="#d9d9d9", highlightthickness=1, highlightbackground="#999")
        self.cv_orig.grid(row=1, column=0, sticky="nsew", padx=(0, 4))
        self.cv_res.grid(row=1, column=1, sticky="nsew", padx=(4, 0))
        self.lbl_preview_msg = ttk.Label(prev, anchor="center", foreground="#555")
        self.lbl_preview_msg.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.cv_orig.bind("<Configure>", lambda e: self._redraw_preview())

        # ---- log
        logf = self._reg(ttk.LabelFrame(root, padding=4), "lbl_log")
        logf.grid(row=3, column=0, sticky="ew", padx=12, pady=4)
        logf.columnconfigure(0, weight=1)
        self.txt_log = tk.Text(logf, height=4, state="disabled", wrap="none")
        self.txt_log.grid(row=0, column=0, sticky="ew")
        sb = ttk.Scrollbar(logf, command=self.txt_log.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.txt_log.configure(yscrollcommand=sb.set)

        # ---- bottom
        bot = ttk.Frame(root, padding=(12, 4, 12, 10))
        bot.grid(row=4, column=0, sticky="ew")
        bot.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(bot, mode="determinate")
        self.progress.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 6))
        self.lbl_status = ttk.Label(bot)
        self.lbl_status.grid(row=1, column=0, sticky="w")
        self.btn_open = self._reg(ttk.Button(bot, command=self.open_result, state="disabled"), "btn_open_result")
        self.btn_open.grid(row=1, column=1, padx=4)
        self.btn_cancel = self._reg(ttk.Button(bot, command=self.cancel, state="disabled"), "btn_cancel")
        self.btn_cancel.grid(row=1, column=2, padx=4)
        self.btn_run = self._reg(ttk.Button(bot, command=self.run), "btn_run")
        self.btn_run.grid(row=1, column=3, padx=(4, 0))

        self._preview_images: tuple[Image.Image, Image.Image] | None = None
        self._est_bytes: int | None = None
        self._on_whiten_auto()
        self._set_status("status_ready")

    def _apply_texts(self) -> None:
        self.root.title(t("app_title"))
        for widget, key, attr in self._texts:
            try:
                widget.configure(**{attr: t(key)})
            except tk.TclError:
                pass
        if self._preview_images is None:
            self.lbl_preview_msg.configure(text=t("preview_empty"))
        self._update_est_label()
        if self._status_key:
            self._set_status(self._status_key, **self._status_kwargs)

    # ------------------------------------------------------------ helpers
    _status_key = ""
    _status_kwargs: dict = {}

    def _set_status(self, key: str, **kwargs) -> None:
        self._status_key, self._status_kwargs = key, kwargs
        self.lbl_status.configure(text=t(key, **kwargs))

    def log(self, key: str, **kwargs) -> None:
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", t(key, **kwargs) + "\n")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def options(self) -> Options:
        whiten = cleanup.WHITEN_AUTO if self.var_whiten_auto.get() else int(self.var_whiten.get())
        return Options(whiten=whiten, contrast=self.var_contrast.get(), mode=self.var_mode.get(),
                       deskew=self.var_deskew.get(), dpi=int(self.var_dpi.get())).validated()

    def _save_options(self) -> None:
        settings = i18n.load_settings()
        o = self.options()
        settings["options"] = {"whiten": o.whiten, "contrast": o.contrast, "mode": o.mode,
                               "deskew": o.deskew, "dpi": o.dpi}
        i18n.save_settings(settings)

    def _on_lang(self, _event=None) -> None:
        name = self.var_lang.get()
        for code, n in i18n.LANG_NAMES.items():
            if n == name:
                i18n.set_lang(code)
                break
        self._apply_texts()

    def _on_whiten_auto(self) -> None:
        auto = self.var_whiten_auto.get()
        self.scl_whiten.state(["disabled"] if auto else ["!disabled"])
        self.lbl_whiten_val.configure(text="" if auto else str(int(self.var_whiten.get())))
        self._schedule_preview()

    def _on_whiten_slide(self, _v=None) -> None:
        self.var_whiten.set(int(round(float(self.scl_whiten.get()))))
        self.lbl_whiten_val.configure(text=str(self.var_whiten.get()))
        self._schedule_preview()

    def _update_est_label(self) -> None:
        if self._est_bytes is None or not self.files:
            self.lbl_est.configure(text=t("lbl_est_size_unknown"))
        else:
            self.lbl_est.configure(text=t("lbl_est_size", size=cleanup.human_size(self._est_bytes)))

    # ------------------------------------------------------------ files
    def _on_drop(self, event) -> None:
        paths = self.root.tk.splitlist(event.data)
        self.add_paths(list(paths))

    def _add_files_dialog(self) -> None:
        paths = filedialog.askopenfilenames(filetypes=[(t("file_dialog_pdf"), "*.pdf")])
        if paths:
            self.add_paths(list(paths))

    def _add_folder_dialog(self) -> None:
        d = filedialog.askdirectory()
        if d:
            self.add_paths([d])

    def add_paths(self, paths: list[str]) -> None:
        found = cleanup.collect_pdfs(paths)
        if not found:
            self.log("err_no_pdf_found")
            return
        for path in found:
            if path in self.files:
                continue
            name = os.path.basename(path)
            password = None
            try:
                doc = cleanup.open_pdf(path)
            except cleanup.PasswordRequired:
                password = simpledialog.askstring(t("err_password"), t("dlg_password_prompt", name=name),
                                                  show="*", parent=self.root)
                if password is None:
                    self.log("log_skipped", name=name)
                    continue
                try:
                    doc = cleanup.open_pdf(path, password)
                except cleanup.PasswordRequired:
                    messagebox.showerror(t("dlg_error"), t("err_wrong_password", name=name), parent=self.root)
                    self.log("log_skipped", name=name)
                    continue
            except Exception:
                messagebox.showerror(t("dlg_error"), t("err_open_failed", name=name), parent=self.root)
                self.log("log_skipped", name=name)
                continue
            try:
                pages = len(doc)
                if pages == 0:
                    messagebox.showerror(t("dlg_error"), t("err_empty_pdf", name=name), parent=self.root)
                    self.log("err_empty_pdf", name=name)
                    continue
                if not cleanup.is_scanned_pdf(doc):
                    if not messagebox.askyesno(t("dlg_confirm"), f"{name}\n{t('warn_text_pdf')}", parent=self.root):
                        self.log("log_skipped", name=name)
                        continue
            finally:
                doc.close()
            self.files.append(path)
            if password:
                self.passwords[path] = password
            self.page_counts[path] = pages
            self.lst_files.insert("end", f"{name}  ({t('unit_pages', pages=pages)})")
            self.log("log_added", name=name, pages=pages)
        self._schedule_preview()

    def clear_files(self) -> None:
        self.files.clear()
        self.passwords.clear()
        self.page_counts.clear()
        self.lst_files.delete(0, "end")
        self._preview_images = None
        self._est_bytes = None
        self._photos.clear()
        self.cv_orig.delete("all")
        self.cv_res.delete("all")
        self.lbl_preview_msg.configure(text=t("preview_empty"))
        self._update_est_label()

    # ------------------------------------------------------------ preview
    def _schedule_preview(self) -> None:
        if self.preview_job:
            self.root.after_cancel(self.preview_job)
        self.preview_job = self.root.after(350, self._start_preview)

    def _start_preview(self) -> None:
        self.preview_job = None
        if not self.files:
            return
        path = self.files[0]
        opts = self.options()
        self.preview_gen += 1
        gen = self.preview_gen
        self.lbl_preview_msg.configure(text=t("preview_loading"))

        def work() -> None:
            try:
                doc = cleanup.open_pdf(path, self.passwords.get(path))
                try:
                    original, result, size, secs, level = cleanup.probe_first_page(doc, opts)
                    pages = len(doc)
                finally:
                    doc.close()
                original.thumbnail((PREVIEW_W * 2, PREVIEW_H * 2))
                result = result.convert("L") if result.mode == "1" else result
                result.thumbnail((PREVIEW_W * 2, PREVIEW_H * 2))
                self.root.after(0, lambda: self._preview_done(gen, original, result, size * pages, level, secs))
            except Exception as exc:  # pragma: no cover - UI feedback only
                self.root.after(0, lambda: self.lbl_preview_msg.configure(text=str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _preview_done(self, gen: int, original, result, est_bytes: int, level: int, secs: float) -> None:
        if gen != self.preview_gen:
            return
        self._preview_images = (original, result)
        self._est_bytes = est_bytes
        self._page_seconds = secs
        if self.var_whiten_auto.get():
            self.var_whiten.set(level)
            self.lbl_whiten_val.configure(text=f"({level})")
        self.lbl_preview_msg.configure(text="")
        self._update_est_label()
        self._redraw_preview()

    def _redraw_preview(self) -> None:
        if not self._preview_images:
            return
        self._photos.clear()
        for canvas, img in zip((self.cv_orig, self.cv_res), self._preview_images):
            cw, ch = max(canvas.winfo_width(), 50), max(canvas.winfo_height(), 50)
            scale = min(cw / img.width, ch / img.height, 1.0)
            shown = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
            photo = ImageTk.PhotoImage(shown)
            self._photos.append(photo)
            canvas.delete("all")
            canvas.create_image(cw // 2, ch // 2, image=photo, anchor="center")

    # ------------------------------------------------------------ running
    _page_seconds = 0.0

    def run(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not self.files:
            messagebox.showinfo(t("app_title"), t("msg_no_files"), parent=self.root)
            return
        opts = self.options()
        self._save_options()
        per_page = self._page_seconds or 0.4
        for path in self.files:
            pages = self.page_counts.get(path, 0)
            if pages >= cleanup.LARGE_PAGE_COUNT:
                minutes = max(1, int(round(pages * per_page / 60)))
                if not messagebox.askyesno(t("dlg_confirm"), t("msg_large_file", name=os.path.basename(path),
                                                                pages=pages, minutes=minutes), parent=self.root):
                    return
        self.cancel_event.clear()
        self.outputs = []
        total_pages = sum(self.page_counts.get(p, 1) for p in self.files)
        self.progress.configure(maximum=max(total_pages, 1), value=0)
        self._set_controls(running=True)
        files = list(self.files)

        def work() -> None:
            done_pages = 0
            count = 0
            failed_total = 0
            for idx, path in enumerate(files, 1):
                name = os.path.basename(path)
                base = done_pages

                def progress(page: int, pages: int, _base=base, _idx=idx, _name=name) -> None:
                    self.root.after(0, lambda: self._on_progress(_base + page, _idx, len(files), page, pages, _name))

                def page_failed(page: int, exc: Exception, _name=name) -> None:
                    self.root.after(0, lambda: self.log("log_page_failed", name=_name, page=page, error=str(exc)))

                try:
                    result = cleanup.process_pdf(path, opts, password=self.passwords.get(path),
                                                 progress=progress, cancel=self.cancel_event,
                                                 page_failed=page_failed)
                except cleanup.Cancelled:
                    self.root.after(0, lambda _n=name: self.log("log_cancelled", name=_n))
                    self.root.after(0, lambda: self._finished(cancelled=True, count=count, failed=failed_total))
                    return
                except Exception as exc:  # one bad file must not end the batch
                    self.root.after(0, lambda _n=name, _e=exc: self.log("err_file_failed", name=_n, error=str(_e)))
                    done_pages += self.page_counts.get(path, 0)
                    self.root.after(0, lambda _d=done_pages: self.progress.configure(value=_d))
                    continue
                count += 1
                failed_total += len(result.failed_pages)
                done_pages += result.pages
                self.outputs.append(result.output_path)
                self.root.after(0, lambda _p=result.output_path: self.log("log_saved", path=_p))
            self.root.after(0, lambda: self._finished(cancelled=False, count=count, failed=failed_total))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _on_progress(self, done: int, file_idx: int, files: int, page: int, pages: int, name: str) -> None:
        self.progress.configure(value=done)
        self._set_status("status_processing", file=file_idx, files=files, page=page, pages=pages, name=name)

    def _finished(self, cancelled: bool, count: int, failed: int) -> None:
        self._set_controls(running=False)
        if cancelled or self._closing:
            self._set_status("status_cancelled")
            return
        self._set_status("status_done")
        self.progress.configure(value=self.progress["maximum"])
        if self.outputs:
            self.btn_open.configure(state="normal")
        msg = t("msg_done", count=count)
        if failed:
            msg += "\n" + t("msg_failed_pages", count=failed)
        messagebox.showinfo(t("app_title"), msg, parent=self.root)

    def cancel(self) -> None:
        self.cancel_event.set()
        self.btn_cancel.configure(state="disabled")

    def _set_controls(self, running: bool) -> None:
        for w in (self.btn_run, self.btn_add, self.btn_add_dir, self.btn_clear):
            w.configure(state="disabled" if running else "normal")
        self.cmb_lang.configure(state="disabled" if running else "readonly")
        self.btn_cancel.configure(state="normal" if running else "disabled")
        if running:
            self.btn_open.configure(state="disabled")

    def open_result(self) -> None:
        if not self.outputs:
            return
        target = self.outputs[0]
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(target)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", target])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(target)])
        except OSError:
            pass

    _closing = False

    def _on_close(self) -> None:
        self._closing = True
        self.cancel_event.set()
        try:
            self._save_options()
        except Exception:
            pass
        self._close_when_idle()

    def _close_when_idle(self) -> None:
        """Wait for the worker before tearing down. It is a daemon thread, so
        exiting under it mid-save would leave a truncated _clean.pdf behind.
        The cancel flag stops it at the next page; a save already running is
        allowed to finish."""
        if self.worker and self.worker.is_alive():
            self.root.after(100, self._close_when_idle)
            return
        self.root.destroy()

    def mainloop(self) -> None:
        self.root.mainloop()


def launch(initial_files: list[str] | None = None) -> None:
    App(initial_files).mainloop()
