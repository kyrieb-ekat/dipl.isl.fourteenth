"""
On-demand extraction of a single PDF page as an image, for the review app's
"Source page image" evidence pane. Not a Streamlit module (no `streamlit`
import) so its pure logic is testable/importable standalone -- ui/evidence.py
wraps render_page_image() with @st.cache_data.

Uses `pdfimages -j` rather than `pdftoppm` to pull the page's already-scanned
JPEG directly: ~130x faster in direct testing against the real DI PDFs (0.05s
vs 6.4s/page) since it copies the embedded image bytes rather than
re-rendering the whole page content stream (which, for these PDFs, also
includes an invisible per-glyph-positioned OCR text layer). Falls back to
pdftoppm only for the rare page where pdfimages yields nothing usable (a
non-JPEG-encoded image, or a genuinely blank page).
"""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from config import PDF_DIR

_VOLUME_PATTERN = "Diplomatarium_Islandicum___Bindi_*.pdf"


def resolve_pdf_path(volume: int) -> Path | None:
    """Mirrors 01_extract_text.py's infer_volume_number() matching (a
    trailing integer in the filename), reimplemented locally since it's a
    tiny, stateless helper -- not worth a cross-script import for."""
    for pdf_path in PDF_DIR.glob(_VOLUME_PATTERN):
        m = re.search(r"(\d+)\s*$", pdf_path.stem)
        if m and int(m.group(1)) == volume:
            return pdf_path
    return None


def extract_page_image(pdf_path: Path, page: int, cache_dir: Path) -> Path | None:
    """Returns a path to a cached JPEG of `page` (1-based), extracting it on
    first request. A page can have more than one embedded image object (a
    main scan plus a small stamp/marginal mark) -- picks the largest by file
    size. Returns None if no image could be extracted at all."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"p{page:04d}.jpg"
    if cached.exists():
        return cached

    with tempfile.TemporaryDirectory() as tmp:
        tmp_prefix = str(Path(tmp) / "page")
        subprocess.run(
            ["pdfimages", "-j", "-f", str(page), "-l", str(page), str(pdf_path), tmp_prefix],
            capture_output=True, text=True,
        )
        # Only trust actual JPEG output -- pdfimages -j falls back to .ppm/.pbm
        # for a non-JPEG-encoded image, which wouldn't display correctly if
        # just renamed to .jpg without a real re-encode.
        jpegs = sorted(Path(tmp).glob("page-*.jpg"), key=lambda p: p.stat().st_size, reverse=True)
        if jpegs:
            shutil.move(str(jpegs[0]), str(cached))
            return cached

        # Fallback: full re-rasterization (slow, ~6s/page, but always
        # produces a real JPEG regardless of the page's internal image
        # encoding).
        subprocess.run(
            ["pdftoppm", "-jpeg", "-r", "300", "-f", str(page), "-l", str(page),
             str(pdf_path), tmp_prefix],
            capture_output=True, text=True,
        )
        jpegs = sorted(Path(tmp).glob("page-*.jpg"), key=lambda p: p.stat().st_size, reverse=True)
        if jpegs:
            shutil.move(str(jpegs[0]), str(cached))
            return cached

    return None
