"""Image processing for scan-pdf-cleanup.

Pipeline per page: render -> deskew -> background normalization ->
whiten/levels -> color mode -> encode -> insert into a new image-only PDF.
Pages are processed one at a time so memory use stays flat.
"""
from __future__ import annotations

import io
import logging
import math
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

# contrast level -> (ink alpha, gamma). Alpha slides the black point from the
# ink cluster toward the ink/paper split: higher means bolder, blacker text.
# Gamma darkens what is left in between.
_CONTRAST = {"low": (0.15, 1.25), "mid": (0.35, 1.6), "high": (0.60, 2.1)}
_FALLBACK_BLACK = 35.0  # used when a page has no ink to measure
_INK_PERCENTILE = 1.0  # percentile of a page taken to sit inside ink strokes
_MIN_WINDOW = 18.0  # narrowest black-to-white span the tone curve may use
_EDGE_MARGIN = 0.06  # page edge ignored when measuring: shadows and rims live there
_DENOISE_WINDOW = 45.0  # below this span the stretch would amplify paper grain
JPEG_QUALITY = 85
MAX_DESKEW_ANGLE = 5.0
LARGE_PAGE_COUNT = 500
# A page bigger than this is rendered at a lower dpi so one page cannot
# exhaust memory. 30 Mpx covers A3 at 300 dpi.
MAX_PIXELS = 30_000_000
# Row bands are sized by pixel budget: an ordinary page is one band (fast),
# only an outsized one is split (bounded memory).
_BLOCK_PIXELS = 8_000_000
_MAX_GAIN = 1.8  # ceiling on background lift, so dark figures are not blown out
_BG_TARGET = 240  # working width for the background estimate, in pixels


class Cancelled(Exception):
    """Raised inside process_pdf when the cancel event is set."""


class PasswordRequired(Exception):
    """The PDF is encrypted and no (or a wrong) password was given."""


class EmptyDocument(Exception):
    """The PDF has no pages, so there is nothing to clean."""


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


def effective_dpi(page: pymupdf.Page, dpi: int, max_pixels: int = MAX_PIXELS) -> int:
    """Lower the render dpi for outsized pages (posters, plans) so a single
    page cannot exhaust memory. Normal book pages are returned unchanged."""
    pixels = (page.rect.width / 72.0 * dpi) * (page.rect.height / 72.0 * dpi)
    if pixels <= max_pixels or pixels <= 0:
        return dpi
    reduced = max(36, int(dpi * math.sqrt(max_pixels / pixels)))
    log.info("page %s is %.0f Mpx at %d dpi; rendering at %d dpi instead",
             getattr(page, "number", "?"), pixels / 1e6, dpi, reduced)
    return reduced


def render_page(page: pymupdf.Page, dpi: int, mode: str) -> Image.Image:
    cs = pymupdf.csRGB if mode == "color" else pymupdf.csGRAY
    pix = page.get_pixmap(dpi=effective_dpi(page, dpi), colorspace=cs, alpha=False)
    pil_mode = "RGB" if mode == "color" else "L"
    return Image.frombytes(pil_mode, (pix.width, pix.height), pix.samples)


# ------------------------------------------------------------ image algebra

def _downscale(gray: np.ndarray, target: int) -> Image.Image:
    img = Image.fromarray(gray)
    factor = max(1, int(round(max(gray.shape) / target)))
    return img.reduce(factor) if factor > 1 else img


def estimate_background(channel: np.ndarray) -> np.ndarray:
    """Smooth estimate of the paper color for one uint8 channel.

    Measured on a small copy, since lighting varies slowly: two 3x3 maximum
    passes reach as far as one 7x7 on a larger copy, agree with it to within
    two levels, and cost a tenth as much. Kept as uint8, because a float copy
    of a full page would cost four times the memory."""
    h, w = channel.shape
    small = _downscale(channel, _BG_TARGET)
    if min(small.size) >= 3:
        small = small.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.MaxFilter(3))
    small = small.filter(ImageFilter.GaussianBlur(4))
    return np.asarray(small.resize((w, h), Image.BILINEAR), dtype=np.uint8)


def _gain_table() -> np.ndarray:
    """table[b, v] = pixel v lifted by the gain that takes background b to
    white. Backgrounds are quantised to 64 steps, an error under 2%, which
    turns the whole normalization into one gather instead of float arithmetic
    over every pixel of every page."""
    bg_levels = np.arange(64, dtype=np.float32) * 4 + 2
    values = np.arange(256, dtype=np.float32)
    gain = np.clip(255.0 / np.maximum(bg_levels, 1.0), 1.0, _MAX_GAIN)[:, None]
    return np.clip(values[None, :] * gain, 0, 255).astype(np.uint8)


_GAIN_TABLE = _gain_table()


def _normalize_channel(ch: np.ndarray, dst: np.ndarray) -> None:
    """dst = ch scaled so its estimated background reaches white. Runs in row
    bands so peak memory stays bounded on an outsized page."""
    bg = estimate_background(ch)
    rows = max(256, _BLOCK_PIXELS // max(ch.shape[1], 1))
    for y0 in range(0, ch.shape[0], rows):
        y1 = min(y0 + rows, ch.shape[0])
        dst[y0:y1] = _GAIN_TABLE[bg[y0:y1] >> 2, ch[y0:y1]]
    del bg


def normalize_background(arr: np.ndarray) -> np.ndarray:
    """Divide each channel by its estimated background so paper becomes ~255.
    Works for HxW (gray) and HxWx3 (RGB). Gain is capped so dark figures
    are not blown out."""
    out = np.empty_like(arr)
    if arr.ndim == 2:
        _normalize_channel(arr, out)
    else:
        for c in range(arr.shape[2]):
            _normalize_channel(np.ascontiguousarray(arr[..., c]), out[..., c])
    return out


def interior(arr: np.ndarray, margin: float = _EDGE_MARGIN) -> np.ndarray:
    """The page without its outermost band. Every measurement uses this: the
    rim of a scan holds shadows, the edge of the platen and the neighbouring
    page, and letting that junk set the tone curve darkens the whole result."""
    h, w = arr.shape[:2]
    my, mx = int(h * margin), int(w * margin)
    if h - 2 * my < 16 or w - 2 * mx < 16:
        return arr
    return arr[my:h - my, mx:w - mx]


def denoise(arr: np.ndarray) -> np.ndarray:
    """Light median pass, used only when the tone curve is about to stretch a
    narrow band of levels and would otherwise turn paper grain into pepper."""
    return np.asarray(Image.fromarray(arr).filter(ImageFilter.MedianFilter(3)))


def histogram(gray: np.ndarray) -> np.ndarray:
    """256-bin tally of one gray page. Every page measurement is derived from
    this instead of from boolean masks, which would copy megapixels each time."""
    return np.bincount(gray.ravel(), minlength=256).astype(np.float64)


def _mean_std_above(hist: np.ndarray, thr: int) -> Optional[tuple[float, float, float]]:
    """(mean, standard deviation, count) of the levels at or above thr."""
    tail = hist[thr:]
    n = float(tail.sum())
    if n <= 0:
        return None
    levels = np.arange(thr, 256, dtype=np.float64)
    mean = float((tail * levels).sum() / n)
    var = float((tail * (levels - mean) ** 2).sum() / n)
    return mean, math.sqrt(max(var, 0.0)), n


def _percentile_from_hist(hist: np.ndarray, q: float) -> float:
    total = float(hist.sum())
    if total <= 0:
        return 0.0
    idx = int(np.searchsorted(np.cumsum(hist), total * q / 100.0))
    return float(min(idx, 255))


def otsu_threshold(gray: np.ndarray, hist: Optional[np.ndarray] = None) -> int:
    """Otsu's split point. When several thresholds tie -- which is exactly
    what a clean scan looks like, ink and paper with an empty gap between --
    the middle of the tied range is returned rather than its lower edge."""
    if hist is None:
        hist = histogram(gray)
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
    var[~np.isfinite(var)] = -1.0
    best = var.max()
    if best <= 0:  # a flat page: no split exists
        return 128
    tied = np.flatnonzero(var >= best - 1e-9)
    return int(round(float(tied.mean())))


def auto_whiten_level(gray_norm: np.ndarray, thr: Optional[int] = None,
                      hist: Optional[np.ndarray] = None) -> int:
    """Whiten slider value (0..100) derived from the Otsu split of a
    background-normalized gray image.

    The split alone is not enough. On a faint scan it lands just under the
    paper, and a white point up there leaves half the paper below it, greyed
    out instead of cleared. So the paper's own low tail caps it."""
    if hist is None:
        hist = histogram(gray_norm)
    if thr is None:
        thr = otsu_threshold(gray_norm, hist)
    white_point = (thr + 255) / 2.0
    stats = _mean_std_above(hist, thr)
    if stats is not None:
        white_point = min(white_point, stats[0] - 3.0 * stats[1])
    white_point = max(170.0, min(250.0, white_point))
    return int(round((255.0 - white_point) / 0.95))


def black_point_for(gray_norm: np.ndarray, thr: int, contrast: str,
                    hist: Optional[np.ndarray] = None) -> float:
    """Where to clip to pure black, measured from the page itself.

    A fixed black point cannot rescue a washed-out scan whose ink sits at 220:
    nothing ever reaches it. So the ink cluster is measured and the black point
    is placed a fraction of the way from it toward the ink/paper split.

    Only a page that actually looks like ink on paper is treated this way. A
    dark plate or a full-bleed photo has no bright paper to anchor on, and
    clipping it to the measured black would crush every detail out of it."""
    alpha = _CONTRAST[contrast][0]
    if hist is None:
        hist = histogram(gray_norm)
    total = float(hist.sum())
    ink_count = float(hist[:thr].sum())
    if ink_count < max(64.0, total / 2000.0):  # nothing that reads as ink
        return _FALLBACK_BLACK
    if ink_count > 0.5 * total:  # mostly dark page
        return _FALLBACK_BLACK
    stats = _mean_std_above(hist, thr)
    if stats is None or stats[0] < 170.0:  # no bright paper: not a text scan
        return _FALLBACK_BLACK

    # Read the ink level from a low percentile of the page, i.e. from stroke
    # cores. Averaging everything below the split looks tempting but on a faint
    # scan that set is mostly paper grain, which drags the estimate up until
    # the tone curve collapses onto the noise and peppers the page black.
    centre = _percentile_from_hist(hist, _INK_PERCENTILE)
    if centre >= thr - 3:  # too little ink to locate reliably
        return _FALLBACK_BLACK
    black = centre + alpha * (thr - centre)
    # Stay clear of the paper's own noise so grain never crosses into black.
    black = min(black, stats[0] - 3.0 * stats[1])
    return float(np.clip(black, 0.0, 250.0))


def white_point_for(level: int) -> float:
    return 255.0 - max(0, min(100, level)) * 0.95


def apply_levels(arr: np.ndarray, white_point: float, black: float, gamma: float) -> np.ndarray:
    # Keep a usable span between the two points: collapsing them would turn the
    # curve into a hard threshold sitting on top of the paper grain.
    black = min(black, white_point - _MIN_WINDOW)
    x = np.arange(256, dtype=np.float32)
    x = np.clip((x - black) / max(white_point - black, 1.0), 0.0, 1.0)
    lut = (np.power(x, gamma) * 255.0 + 0.5).astype(np.uint8)
    return lut[arr]


def _row_variance(binary: Image.Image, angle: float) -> float:
    """How strongly the ink piles into rows at this angle. Text lines line up
    into sharp peaks when the page is straight, so the variance peaks there."""
    rotated = binary.rotate(angle, resample=Image.NEAREST, expand=False, fillcolor=0)
    rows = np.asarray(rotated, dtype=np.float32).sum(axis=1)
    return float(rows.var())


def _ink_mask(gray: np.ndarray, target: int, normalized: bool) -> Optional[Image.Image]:
    small = np.asarray(_downscale(gray, target))
    if not normalized:
        small = normalize_background(small)
    thr = otsu_threshold(small)
    binary = Image.fromarray(np.where(small < thr, 255, 0).astype(np.uint8))
    # ignore a border band where scanner edges and shadows would dominate
    w, h = binary.size
    binary = binary.crop((int(w * 0.05), int(h * 0.05), int(w * 0.95), int(h * 0.95)))
    return binary if min(binary.size) >= 20 else None


def _refine_angle(binary: Image.Image, angles, best: float, best_score: float) -> tuple[float, float]:
    for ang in angles:
        score = _row_variance(binary, float(ang))
        if score > best_score:
            best, best_score = float(ang), score
    return best, best_score


def estimate_skew(gray: np.ndarray, max_angle: float = MAX_DESKEW_ANGLE,
                  normalized: bool = False) -> float:
    """Angle (degrees, PIL rotate convention) that best aligns text rows.
    Returns 0.0 when the page is already straight.

    Searched coarsely on a small copy first, then refined on a larger one, so
    the expensive rotations only happen near the answer."""
    coarse = _ink_mask(gray, 360, normalized)
    if coarse is None:
        return 0.0
    best, _ = _refine_angle(coarse, np.arange(-max_angle, max_angle + 1e-6, 1.0),
                            0.0, _row_variance(coarse, 0.0))
    fine = _ink_mask(gray, 760, normalized)
    if fine is None:
        return round(best, 2) if abs(best) >= 0.15 else 0.0
    best_score = _row_variance(fine, best)
    best, best_score = _refine_angle(fine, np.arange(best - 1.0, best + 1.0 + 1e-6, 0.25),
                                     best, best_score)
    best, _ = _refine_angle(fine, np.arange(best - 0.2, best + 0.2 + 1e-6, 0.1), best, best_score)
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

    # Normalize first, then straighten: the skew search can reuse the evened-out
    # page instead of normalizing its own copy, and the corners the rotation
    # fills stay pure white instead of being pulled back toward grey.
    arr = normalize_background(np.asarray(img))
    if opts.deskew:
        gray = arr if arr.ndim == 2 else np.asarray(Image.fromarray(arr).convert("L"))
        angle = estimate_skew(gray, normalized=True)
        if angle:
            # Bilinear, not bicubic: for a tilt of a few degrees the two are
            # indistinguishable on text, and this one is three times quicker.
            arr = np.asarray(Image.fromarray(arr).rotate(
                angle, resample=Image.BILINEAR, expand=False, fillcolor=fill))

    gray_norm = arr if arr.ndim == 2 else np.asarray(Image.fromarray(arr).convert("L"))
    probe = interior(gray_norm)
    hist = histogram(probe)
    thr = otsu_threshold(probe, hist)
    level = opts.whiten if opts.whiten != WHITEN_AUTO else auto_whiten_level(probe, thr, hist)
    black = black_point_for(probe, thr, opts.contrast, hist)
    white = white_point_for(level)
    gamma = _CONTRAST[opts.contrast][1]
    if white - black < _DENOISE_WINDOW:
        arr = denoise(arr)
    arr = apply_levels(arr, white, black, gamma)

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
    if len(doc) == 0:
        raise EmptyDocument(doc.name)
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
    n = len(doc)
    if n == 0:
        doc.close()
        raise EmptyDocument(input_path)
    out_path = output_path or output_path_for(input_path)
    out = pymupdf.open()
    failed: list[int] = []
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
                # Size the page from the source page, not from pixels over dpi,
                # so geometry survives any dpi reduction on outsized pages.
                new_page = out.new_page(width=page.rect.width, height=page.rect.height)
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
