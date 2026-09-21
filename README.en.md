# Chaekgalpi Tools – Scan PDF Cleanup

[한국어](README.md) · [中文](README.zh-CN.md) · [日本語](README.ja.md)

Turns yellowed, faint scanned PDFs into white pages with dark text so they read well on a black-and-white e-reader. No server, no install, the original file is never modified.

![Before and after](docs/before_after.png)

## Download

- **Executable**: grab `scan-pdf-cleanup.exe` from [Releases](https://github.com/microhan1/scan-pdf-cleanup/releases) and double-click it. Nothing to install.
- **Run from source**:

```bash
pip install -r requirements.txt
python main.py
```

## Usage

1. Drop PDF files or a folder onto the window.
2. Adjust the options while watching the preview (whiten background · darken text · color mode · deskew · resolution).
3. Press **Run**. `<name>_clean.pdf` is written next to the original.

There is a command line too.

```bash
python main.py input.pdf --contrast mid --mode gray --dpi 200 --deskew
```

`python main.py --help` prints the options in your OS language (한국어 · English · 中文 · 日本語).

## What it does not do

- No OCR. The output is an image PDF.
- Margin cropping and two-page splitting are separate tools. This one only handles image quality.
- Color-photo magazines are out of scope.

## Series

- Chaekgalpi Tools: [Margin Crop (TrimPDF)](https://github.com/microhan1/TrimPDF) · [Two-page Split (spread-split)](https://github.com/microhan1/spread-split) · [TOC Bookmarks (pdf-toc-add)](https://github.com/microhan1/pdf-toc-add)
- [Chaekgalpi](https://github.com/microhan1/chaekgalpi)

## License

The source code is MIT. See [LICENSE](LICENSE).

The exe on Releases bundles [PyMuPDF](https://github.com/pymupdf/PyMuPDF) (AGPL-3.0), so the exe as a whole is distributed under AGPL-3.0 terms. The corresponding source is this repository and the PyMuPDF repository.
