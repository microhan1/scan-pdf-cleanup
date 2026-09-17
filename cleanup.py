"""Image processing for scan-pdf-cleanup.

Pipeline per page: render -> deskew -> background normalization ->
whiten/levels -> color mode -> encode -> insert into a new image-only PDF.
Pages are processed one at a time so memory use stays flat.
"""
from __future__ import annotations

import io
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
import pymupdf
from PIL import Image, ImageFilter

log = logging.getLogger("scan_pdf_cleanup")

CONTRAST_LEVELS = ("low", "mid", "high")
MODES = ("color", "gray", "bw")
DPIS = (150, 200, 300)
WHITEN_AUTO = -1

# contrast level -> (black point, gamma). Higher gamma darkens midtones.
_CONTRAST = {"low": (15, 1.25), "mid": (35, 1.6), "high": (60, 2.1)}
JPEG_QUALITY = 85
MAX_DESKEW_ANGLE = 5.0
LARGE_PAGE_COUNT = 500


class Cancelled(Exception):
    """Raised inside process_pdf when the cancel event is set."""


class PasswordRequired(Exception):
    """The PDF is encrypted and no (or a wrong) password was given."""


@dataclass
class Options:
    whiten: int = WHITEN_AUTO  # -1 = auto, otherwise 0..100
    contrast: str = "mid"
    mode: str = "gray"
    deskew: bool = True
    dpi: int = 200

    def validated(self) -> "Options":
        if self.contrast not in CONTRAST_LEVELS:
            self.contrast = "mid"
        if self.mode not in MODES:
            self.mode = "gray"
        if self.dpi not in DPIS:
            self.dpi = 200
        if self.whiten != WHITEN_AUTO:
            self.whiten = int(max(0, min(100, self.whiten)))
        return self


@dataclass
class FileResult:
    input_path: str
    output_path: str
    pages: int
    failed_pages: list[int] = field(default_factory=list)
    elapsed: float = 0.0


# ---------------------------------------------------------------- PDF access

def open_pdf(path: str, password: Optional[str] = None) -> pymupdf.Document:
    """Open a PDF read-only. Raises PasswordRequired if it is encrypted and
    the password is missing or wrong. Never writes to ``path``."""
    doc = pymupdf.open(path)
    if doc.needs_pass:
        if not password or not doc.authenticate(password):
            doc.close()
            raise PasswordRequired(path)
    return doc


def is_scanned_pdf(doc: pymupdf.Document, sample: int = 5) -> bool:
    """True when the first pages are dominated by full-page images.
    A text-only PDF (no big image, but extractable text) returns False."""
    n = min(len(doc), sample)
    if n == 0:
        return True
    image_pages = text_pages = 0
    for i in range(n):
        page = doc[i]
        area = max(page.rect.width * page.rect.height, 1.0)
        big = False
        try:
            for info in page.get_image_info():
                x0, y0, x1, y1 = info["bbox"]
                if (x1 - x0) * (y1 - y0) >= 0.4 * area:
                    big = True
                    break
        except Exception:
            pass
        if big:
            image_pages += 1
        elif len(page.get_text("text").strip()) > 20:
            text_pages += 1
    return text_pages <= image_pages


def output_path_for(input_path: str) -> str:
    """<name>_clean.pdf next to the input; numbered if it already exists."""
    base, _ = os.path.splitext(input_path)
    candidate = f"{base}_clean.pdf"
    n = 2
    while os.path.exists(candidate):
        candidate = f"{base}_clean({n}).pdf"
        n += 1
    return candidate


def render_page(page: pymupdf.Page, dpi: int, mode: str) -> Image.Image:
    cs = pymupdf.csRGB if mode == "color" else pymupdf.csGRAY
    pix = page.get_pixmap(dpi=dpi, colorspace=cs, alpha=False)
    pil_mode = "RGB" if mode == "color" else "L"
    return Image.frombytes(pil_mode, (pix.width, pix.height), pix.samples)


# ------------------------------------------------------------ image algebra

def _downscale(gray: np.ndarray, target: int) -> Image.Image:
    img = Image.fromarray(gray)
    factor = max(1, int(round(max(gray.shape) / target)))
    return img.reduce(factor) if factor > 1 else img


def estimate_background(channel: np.ndarray) -> np.ndarray:
    """Smooth estimate of the paper color for one uint8 channel."""
    h, w = channel.shape
    small = _downscale(channel, 400)
    small = small.filter(ImageFilter.MaxFilter(7)).filter(ImageFilter.GaussianBlur(6))
    bg = small.resize((w, h), Image.BILINEAR)
    return np.asarray(bg, dtype=np.float32)


def normalize_background(arr: np.ndarray) -> np.ndarray:
    """Divide each channel by its estimated background so paper becomes ~255.
    Works for HxW (gray) and HxWx3 (RGB). Gain is capped so dark figures
    are not blown out."""
    channels = [arr] if arr.ndim == 2 else [arr[..., c] for c in range(arr.shape[2])]
    out = []
    for ch in channels:
        bg = estimate_background(ch)
        gain = np.clip(255.0 / np.maximum(bg, 1.0), 1.0, 1.8)
        out.append(np.clip(ch.astype(np.float32) * gain, 0, 255))
    res = out[0] if arr.ndim == 2 else np.stack(out, axis=-1)
    return res.astype(np.uint8)


def otsu_threshold(gray: np.ndarray) -> int:
    hist = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
    total = hist.sum()
    if total == 0:
        return 128
    levels = np.arange(256, dtype=np.float64)
    w_b = np.cumsum(hist)
    w_f = total - w_b
    sum_b = np.cumsum(hist * levels)
    sum_all = sum_b[-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        m_b = sum_b / w_b
        m_f = (sum_all - sum_b) / w_f
        var = w_b * w_f * (m_b - m_f) ** 2
    var[~np.isfinite(var)] = -1
    return int(np.argmax(var))


def auto_whiten_level(gray_norm: np.ndarray) -> int:
    """Whiten slider value (0..100) derived from the Otsu split of a
    background-normalized gray image."""
    thr = otsu_threshold(gray_norm)
    white_point = (thr + 255) / 2.0
    white_point = max(170.0, min(250.0, white_point))
    return int(round((255.0 - white_point) / 0.95))


def white_point_for(level: int) -> float:
    return 255.0 - max(0, min(100, level)) * 0.95


def apply_levels(arr: np.ndarray, white_point: float, black: float, gamma: float) -> np.ndarray:
    x = np.arange(256, dtype=np.float32)
    x = np.clip((x - black) / max(white_point - black, 1.0), 0.0, 1.0)
    lut = (np.power(x, gamma) * 255.0 + 0.5).astype(np.uint8)
    return lut[arr]


def estimate_skew(gray: np.ndarray, max_angle: float = MAX_DESKEW_ANGLE) -> float:
    """Angle (degrees, PIL rotate convention) that best aligns text rows.
    Returns 0.0 when the page is already straight."""
    small = np.asarray(_downscale(gray, 800))
    norm = normalize_background(small)
    thr = otsu_threshold(norm)
    binary = Image.fromarray(np.where(norm < thr, 255, 0).astype(np.uint8))
    # ignore a border band where scanner edges/shadows would dominate
    w, h = binary.size
    binary = binary.crop((int(w * 0.05), int(h * 0.05), int(w * 0.95), int(h * 0.95)))
    if binary.size[0] < 20 or binary.size[1] < 20:
        return 0.0

    def score(angle: float) -> float:
        r = binary.rotate(angle, resample=Image.NEAREST, expand=False, fillcolor=0)
        rows = np.asarray(r, dtype=np.float32).sum(axis=1)
        return float(rows.var())

    best, best_score = 0.0, score(0.0)
    for ang in np.arange(-max_angle, max_angle + 1e-6, 0.5):
        s = score(float(ang))
        if s > best_score:
            best, best_score = float(ang), s
    for ang in np.arange(best - 0.4, best + 0.4 + 1e-6, 0.1):
        s = score(float(ang))
        if s > best_score:
            best, best_score = float(ang), s
    return round(best, 2) if abs(best) >= 0.15 else 0.0


def process_image(img: Image.Image, opts: Options) -> tuple[Image.Image, int]:
    """Run the full pipeline on one rendered page.
    Returns (result image, whiten level actually used)."""
    if opts.mode == "color":
        img = img.convert("RGB")
        fill = (255, 255, 255)
    else:
        img = img.convert("L")
        fill = 255

    if opts.deskew:
        gray = np.asarray(img if img.mode == "L" else img.convert("L"))
        angle = estimate_skew(gray)
        if angle:
            img = img.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=fill)

    arr = normalize_background(np.asarray(img))
    gray_norm = arr if arr.ndim == 2 else np.asarray(Image.fromarray(arr).convert("L"))
    level = opts.whiten if opts.whiten != WHITEN_AUTO else auto_whiten_level(gray_norm)
    black, gamma = _CONTRAST[opts.contrast]
    arr = apply_levels(arr, white_point_for(level), black, gamma)

    if opts.mode == "bw":
        thr = max(100, min(200, otsu_threshold(arr)))
        out = Image.fromarray(np.where(arr > thr, 255, 0).astype(np.uint8)).convert("1")
    else:
        out = Image.fromarray(arr)
    return out, level


def encode_image(img: Image.Image, mode: str) -> bytes:
    buf = io.BytesIO()
    if mode == "bw":
        img.save(buf, "PNG", optimize=True)
    else:
        img.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue()


# ------------------------------------------------------------- whole files

def probe_first_page(doc: pymupdf.Document, opts: Options) -> tuple[Image.Image, Image.Image, int, float, int]:
    """Process page 1 only. Returns (original, result, encoded bytes,
    seconds taken, whiten level used). Used for preview and estimates."""
    opts = opts.validated()
    t0 = time.perf_counter()
    original = render_page(doc[0], opts.dpi, opts.mode)
    result, level = process_image(original, opts)
    size = len(encode_image(result, opts.mode))
    return original, result, size, time.perf_counter() - t0, level


def process_pdf(
    input_path: str,
    opts: Options,
    password: Optional[str] = None,
    output_path: Optional[str] = None,
    progress: Optional[Callable[[int, int], None]] = None,
    cancel: Optional[threading.Event] = None,
    page_failed: Optional[Callable[[int, Exception], None]] = None,
) -> FileResult:
    """Clean one PDF into a new image PDF. The input is never modified.
    A page that fails is copied from the original as-is and reported through
    ``page_failed``. Raises Cancelled (writing nothing) if ``cancel`` is set."""
    opts = opts.validated()
    doc = open_pdf(input_path, password)
    out_path = output_path or output_path_for(input_path)
    out = pymupdf.open()
    failed: list[int] = []
    n = len(doc)
    t0 = time.perf_counter()
    try:
        for i in range(n):
            if cancel is not None and cancel.is_set():
                raise Cancelled(input_path)
            try:
                page = doc[i]
                img = render_page(page, opts.dpi, opts.mode)
                result, _ = process_image(img, opts)
                data = encode_image(result, opts.mode)
                w_pt = result.width * 72.0 / opts.dpi
                h_pt = result.height * 72.0 / opts.dpi
                new_page = out.new_page(width=w_pt, height=h_pt)
                new_page.insert_image(new_page.rect, stream=data)
                del img, result, data
            except Cancelled:
                raise
            except Exception as exc:  # keep the file whole
                failed.append(i + 1)
                log.warning("%s page %d failed: %s", input_path, i + 1, exc)
                if page_failed:
                    page_failed(i + 1, exc)
                try:
                    out.insert_pdf(doc, from_page=i, to_page=i)
                except Exception as exc2:
                    log.error("%s page %d could not be copied either: %s", input_path, i + 1, exc2)
            if progress:
                progress(i + 1, n)
        if cancel is not None and cancel.is_set():
            raise Cancelled(input_path)
        out.save(out_path, garbage=3, deflate=True)
    finally:
        out.close()
        doc.close()
    return FileResult(input_path, out_path, n, failed, time.perf_counter() - t0)


def collect_pdfs(paths: list[str]) -> list[str]:
    """Expand folders (recursively) into PDF paths; keep order, drop dupes."""
    seen: set[str] = set()
    result: list[str] = []

    def add(p: str) -> None:
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            result.append(os.path.abspath(p))

    for p in paths:
        if os.path.isdir(p):
            for root, _dirs, files in os.walk(p):
                for name in sorted(files):
                    if name.lower().endswith(".pdf"):
                        add(os.path.join(root, name))
        elif p.lower().endswith(".pdf") and os.path.isfile(p):
            add(p)
    return result


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit in ("B", "KB") else f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} GB"
