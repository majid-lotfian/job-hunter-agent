"""A4 PDF copies of the tailored HTML CVs, for uploading to application platforms.

The HTML CV (data/cvs/job-<id>.html) stays the source of truth; its PDF (data/cvs/job-<id>.pdf) is derived from it
and never involves Gemini. A sidecar file `job-<id>.pdf.sha256` records the hash of the HTML the PDF was rendered
from, so a PDF is only used while it still matches the current HTML CV.

Renderer: WeasyPrint (HTML/CSS -> PDF via the system Pango library), run in a short-lived child process
(`python -m app.pdf --render IN OUT`) so its memory returns to the system as soon as the PDF is written and a
hang can be killed. Headless Chrome was evaluated first on this 1 GB VM: 50-65 s and ~1 GB RSS per render, and it
often exited without writing a file; WeasyPrint renders a CV in ~4 s with ~55 MB.

Output: A4 (the CV's own @page / print CSS is honoured; WeasyPrint adds no browser header/footer), checked after
rendering.

Safety: one render at a time (file lock shared by the service and the CLI), a timeout, a memory cap on the child,
no network or local file access while rendering (only inline data: URIs), atomic replace (a failed render never
touches an existing PDF), and validation (PDF header/trailer, size, 1..PDF_MAX_PAGES pages, real text present).

CLI (on the VM, project directory):
    .venv/bin/python -m app.pdf --job-id 7            # PDF for one job (skipped if already current)
    .venv/bin/python -m app.pdf --job-id 7 --force    # re-render even if current
    .venv/bin/python -m app.pdf --missing             # PDFs for all completed CVs that lack a current one
"""
import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIN_BYTES, MAX_BYTES = 1000, 10 * 1024 * 1024
MIN_TEXT_CHARS = 200


def _int_env(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def limits():
    return {'timeout': max(10, _int_env('PDF_TIMEOUT_SECONDS', 120)),
            'max_pages': max(1, _int_env('PDF_MAX_PAGES', 4)),
            'memory_mb': max(128, _int_env('PDF_MAX_MEMORY_MB', 1024))}


class PdfError(Exception):
    pass


# --------------------------------------------------------------------------- child process (renderer)

def offline_fetcher():
    """URL fetcher that only resolves inline data: URIs - no http(s), no local files."""
    from weasyprint import URLFetcher
    return URLFetcher(allowed_protocols={'data'}, timeout=1)


def _render_child(html_path, out_path):
    """Runs in the child process only. Prints one JSON line: {"pages": n, "text_chars": n}."""
    from weasyprint import HTML
    from weasyprint.formatting_structure.boxes import TextBox

    with open(html_path, 'rb') as handle:
        document = HTML(file_obj=handle, url_fetcher=offline_fetcher(), media_type='print').render()
    chars = 0
    for page in document.pages:
        chars += sum(len(box.text.strip()) for box in page._page_box.descendants() if isinstance(box, TextBox))
    document.write_pdf(str(out_path))
    first = document.pages[0] if document.pages else None
    size_mm = [round(first.width * 25.4 / 96, 1), round(first.height * 25.4 / 96, 1)] if first else [0, 0]
    print(json.dumps({'pages': len(document.pages), 'text_chars': chars, 'page_mm': size_mm}))


def _limit_memory(megabytes):
    def apply():
        import resource
        limit = megabytes * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    return apply


# --------------------------------------------------------------------------- parent side

def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sidecar(pdf_path):
    return Path(str(pdf_path) + '.sha256')


def validate_pdf(path):
    """Structural checks for an uploadable PDF. Returns its size in bytes; raises PdfError."""
    data = Path(path).read_bytes()
    if not data.startswith(b'%PDF-'):
        raise PdfError('not a PDF (missing %PDF- header)')
    if b'%%EOF' not in data[-2048:]:
        raise PdfError('PDF is truncated (no %%EOF trailer)')
    if not MIN_BYTES <= len(data) <= MAX_BYTES:
        raise PdfError(f'PDF size {len(data)} bytes is outside {MIN_BYTES}..{MAX_BYTES}')
    return len(data)


@contextmanager
def _render_lock():
    """One renderer at a time on this VM, across the service and CLI processes."""
    path = ROOT / 'data' / '.pdf-render.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _run_child(html_path, out_path, cfg):
    cmd = [sys.executable, '-m', 'app.pdf', '--render', str(html_path), str(out_path)]
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=cfg['timeout'],
                          preexec_fn=_limit_memory(cfg['memory_mb']))


def render_pdf(html_path, pdf_path, runner=None):
    """Render html_path to pdf_path (A4, print CSS). Returns {'pages','bytes','text_chars','html_sha256'}."""
    cfg = limits()
    html_path, pdf_path = Path(html_path).resolve(), Path(pdf_path)
    if not html_path.is_file():
        raise PdfError(f'HTML CV not found: {html_path.name}')
    html_hash = sha256_file(html_path)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f'.{pdf_path.name}.', suffix='.tmp', dir=pdf_path.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with _render_lock():
            try:
                proc = (runner or _run_child)(html_path, tmp, cfg)
            except subprocess.TimeoutExpired:
                raise PdfError(f'renderer timed out after {cfg["timeout"]}s') from None
        if proc.returncode != 0:
            tail = (proc.stderr or '').strip().splitlines()[-1:] or ['no error output']
            raise PdfError(f'renderer failed (exit {proc.returncode}): {tail[0][:200]}')
        try:
            report = json.loads((proc.stdout or '').strip().splitlines()[-1])
        except (IndexError, ValueError):
            raise PdfError('renderer returned no result') from None
        size = validate_pdf(tmp)
        pages, chars = int(report.get('pages', 0)), int(report.get('text_chars', 0))
        if not 1 <= pages <= cfg['max_pages']:
            raise PdfError(f'PDF has {pages} page(s); expected 1..{cfg["max_pages"]}')
        if chars < MIN_TEXT_CHARS:
            raise PdfError(f'PDF looks blank ({chars} characters of text)')
        width, height = (report.get('page_mm') or [0, 0])[:2]
        if not (205 <= width <= 215 and 292 <= height <= 302):
            raise PdfError(f'page size {width}x{height} mm is not A4')
        if sha256_file(html_path) != html_hash:
            raise PdfError('HTML CV changed during rendering; try again')
        os.replace(tmp, pdf_path)                                    # atomic: never a half-written PDF
        sidecar(pdf_path).write_text(html_hash + '\n', encoding='utf-8')
        return {'pages': pages, 'bytes': size, 'text_chars': chars, 'page_mm': [width, height], 'html_sha256': html_hash}
    finally:
        tmp.unlink(missing_ok=True)


def pdf_is_current(html_path, pdf_path):
    """True only if the PDF exists, is structurally valid and was rendered from exactly this HTML."""
    html_path, pdf_path = Path(html_path), Path(pdf_path)
    if not (html_path.is_file() and pdf_path.is_file() and sidecar(pdf_path).is_file()):
        return False
    if sidecar(pdf_path).read_text(encoding='utf-8').strip() != sha256_file(html_path):
        return False
    try:
        validate_pdf(pdf_path)
    except PdfError:
        return False
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m app.pdf', description='PDF copies of tailored HTML CVs (no Gemini).')
    parser.add_argument('--job-id', type=int, help='render the PDF for this job')
    parser.add_argument('--missing', action='store_true', help='render PDFs for all completed CVs that lack a current one')
    parser.add_argument('--force', action='store_true', help='with --job-id: re-render even if the PDF is current')
    parser.add_argument('--render', nargs=2, metavar=('HTML', 'PDF'), help=argparse.SUPPRESS)   # child process
    args = parser.parse_args(argv)
    if args.render:
        _render_child(*args.render)
        return 0
    from . import cv_pipeline                     # parent-only imports (the child stays light)
    from .core import init_db
    init_db()
    if args.job_id is not None:
        jobs = [args.job_id]
    elif args.missing:
        jobs = cv_pipeline.pdf_candidates(include_failed=True)
    else:
        parser.error('use --job-id N or --missing')
    failures = 0
    for job_id in jobs:
        status, detail = cv_pipeline.make_pdf(job_id, force=args.force, manual=True)
        print(f'job {job_id}: {status} {detail}')
        failures += status != 'ready'
    if not jobs:
        print('No completed CVs without a current PDF.')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
