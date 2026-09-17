"""Entry point for scan-pdf-cleanup.

    python main.py                      -> GUI
    python main.py input.pdf [...]      -> CLI
    python main.py input.pdf --contrast mid --mode gray --dpi 200 --deskew
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

import i18n
from i18n import t


def _preselect_lang(argv: list[str]) -> str | None:
    for i, a in enumerate(argv):
        if a == "--lang" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--lang="):
            return a.split("=", 1)[1]
    return None


def _localize_argparse() -> None:
    """argparse's own labels go through gettext; route them to lang files."""
    table = {
        "usage: ": t("cli_usage"),
        "positional arguments": t("cli_positional"),
        "options": t("cli_options"),
        "show this help message and exit": t("cli_help"),
    }
    argparse._ = lambda s: table.get(s, s)  # type: ignore[attr-defined]


def build_parser() -> argparse.ArgumentParser:
    import cleanup

    _localize_argparse()
    p = argparse.ArgumentParser(prog="scan-pdf-cleanup", description=t("cli_desc"))
    p.add_argument("inputs", nargs="*", help=t("cli_inputs"))
    p.add_argument("--whiten", type=int, default=cleanup.WHITEN_AUTO, metavar="0-100", help=t("cli_whiten"))
    p.add_argument("--contrast", choices=cleanup.CONTRAST_LEVELS, default="mid", help=t("cli_contrast"))
    p.add_argument("--mode", choices=cleanup.MODES, default="gray", help=t("cli_mode"))
    p.add_argument("--dpi", type=int, choices=cleanup.DPIS, default=200, help=t("cli_dpi"))
    g = p.add_mutually_exclusive_group()
    g.add_argument("--deskew", dest="deskew", action="store_true", default=True, help=t("cli_deskew"))
    g.add_argument("--no-deskew", dest="deskew", action="store_false", help=t("cli_no_deskew"))
    p.add_argument("--password", help=t("cli_password"))
    p.add_argument("-y", "--yes", action="store_true", help=t("cli_yes"))
    p.add_argument("--lang", choices=i18n.LANGS, help=t("cli_lang"))
    p.add_argument("--gui", action="store_true", help=t("cli_gui"))
    return p


def run_cli(args: argparse.Namespace) -> int:
    import cleanup

    files = cleanup.collect_pdfs(args.inputs)
    missing = [p for p in args.inputs if not os.path.exists(p)]
    for p in missing:
        print(t("err_open_failed", name=p), file=sys.stderr)
    if not files:
        print(t("cli_no_input"), file=sys.stderr)
        return 2
    failures = 0
    opts = cleanup.Options(whiten=args.whiten, contrast=args.contrast, mode=args.mode,
                           deskew=args.deskew, dpi=args.dpi).validated()
    processed = 0
    for idx, path in enumerate(files, 1):
        name = os.path.basename(path)
        password = args.password
        try:
            doc = cleanup.open_pdf(path, password)
        except cleanup.PasswordRequired:
            print(f"{name}: {t('err_password')}")
            if args.yes or not sys.stdin.isatty():
                print(t("log_skipped", name=name))
                failures += 1
                continue
            password = getpass.getpass(t("cli_password_prompt", name=name))
            try:
                doc = cleanup.open_pdf(path, password)
            except cleanup.PasswordRequired:
                print(t("err_wrong_password", name=name))
                failures += 1
                continue
        except Exception:
            print(t("err_open_failed", name=name), file=sys.stderr)
            failures += 1
            continue
        try:
            scanned = cleanup.is_scanned_pdf(doc)
            pages = len(doc)
        finally:
            doc.close()
        if pages == 0:
            print(t("err_empty_pdf", name=name), file=sys.stderr)
            failures += 1
            continue
        if not scanned and not args.yes:
            answer = input(f"{name}: {t('warn_text_pdf')}{t('cli_confirm_hint')}").strip().lower()
            if answer not in ("y", "yes"):
                print(t("log_skipped", name=name))
                continue
        print(t("cli_processing", index=idx, total=len(files), name=name, pages=pages))

        def progress(page: int, total: int) -> None:
            print("\r" + t("cli_progress", page=page, pages=total), end="", flush=True)

        def page_failed(page: int, exc: Exception) -> None:
            print("\n" + t("log_page_failed", name=name, page=page, error=str(exc)))

        try:
            result = cleanup.process_pdf(path, opts, password=password, progress=progress, page_failed=page_failed)
        except KeyboardInterrupt:
            print("\n" + t("status_cancelled"))
            return 130
        except Exception as exc:  # one bad file must not end the batch
            print("\n" + t("err_file_failed", name=name, error=exc), file=sys.stderr)
            failures += 1
            continue
        print("\n" + t("log_saved", path=result.output_path))
        if result.failed_pages:
            print(t("msg_failed_pages", count=len(result.failed_pages)))
        processed += 1
    print(t("msg_done", count=processed))
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    i18n.init(_preselect_lang(argv))
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.lang:
        i18n.set_lang(args.lang, persist=False)
    # A windowed exe has no console, so dropping files on it opens the GUI
    # with those files loaded instead of running the CLI into nowhere.
    headless = getattr(sys, "frozen", False) and sys.stdout is None
    if args.gui or headless or not args.inputs:
        import gui

        gui.launch(args.inputs or None)
        return 0
    return run_cli(args)


if __name__ == "__main__":
    sys.exit(main())
