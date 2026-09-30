"""Generic HTML report renderer: agent-authored HTML → HTML + PDF + PNG.

Unlike ``render_shadow_report`` (Shadow Account specific, fixed Jinja2
template), this tool lets any agent turn a free-form HTML string into a
stable, servable artifact for three surfaces:

* Web UI: the HTML file, viewable at ``GET /reports/{report_id}``.
* WeChat: the PNG (rendered inline as an image bubble) or the PDF
  (delivered as a generic file attachment) — see ``src/channels/weixin.py``.

Rendering pipeline: HTML string → weasyprint → PDF → pypdfium2 (first page)
→ PNG. Both weasyprint and pypdfium2 are already project dependencies (used
by ``shadow_account/reporter.py`` and ``doc_reader_tool.py`` respectively),
so this adds no new dependency.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

from src.agent.tools import BaseTool

logger = logging.getLogger(__name__)

_REPORT_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")

# weasyprint's HTML class, probed once per process and cached. Retrying a
# failed import is unsafe (see shadow_account/reporter.py's _try_render_pdf
# docstring — GNOME bug 705535), so this module follows the same
# import-once-cache-the-sentinel pattern rather than importing at module load.
_WEASYPRINT_HTML: Any = None  # weasyprint.HTML once probed; False if unavailable


def reports_dir() -> Path:
    """Return the generic agent-report directory (auto-created)."""
    d = Path.home() / ".vibe-trading" / "agent_reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _try_render_pdf(html: str, output_dir: Path, report_id: str) -> Path | None:
    """Render *html* to a PDF via weasyprint, returning the path or ``None``.

    Mirrors ``shadow_account/reporter.py``'s ``_try_render_pdf``: the
    weasyprint import is probed at most once per process, because retrying
    it after a failure can segfault (cffi/libgobject teardown ordering).
    """
    global _WEASYPRINT_HTML
    if _WEASYPRINT_HTML is False:
        return None
    if _WEASYPRINT_HTML is None:
        try:
            from weasyprint import HTML  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - import-level failure
            _WEASYPRINT_HTML = False
            logger.warning("weasyprint unavailable (%s); PDF/PNG output skipped.", exc)
            return None
        _WEASYPRINT_HTML = HTML

    pdf_path = output_dir / f"{report_id}.pdf"
    try:
        _WEASYPRINT_HTML(string=html).write_pdf(str(pdf_path))
    except Exception as exc:
        logger.warning("weasyprint render failed (%s); PDF/PNG output skipped.", exc)
        if pdf_path.exists():
            try:
                pdf_path.unlink()
            except OSError:
                pass
        return None
    return pdf_path


def _try_render_png(pdf_path: Path, output_dir: Path, report_id: str, *, scale: float = 2.0) -> Path | None:
    """Rasterize *pdf_path*'s first page to a PNG via pypdfium2.

    Kept for backward compatibility (``RenderHtmlReportTool`` still returns
    ``png_path`` pointing at page 1). ``scale`` controls output resolution
    (2.0 ~= 144 DPI, readable on a phone screen without an oversized file).
    Returns ``None`` on any failure — a missing PNG degrades gracefully to
    PDF/HTML delivery.
    """
    paths = _try_render_png_pages(pdf_path, output_dir, report_id, scale=scale, max_pages=1)
    return paths[0] if paths else None


def _try_render_png_pages(
    pdf_path: Path,
    output_dir: Path,
    report_id: str,
    *,
    scale: float = 2.0,
    max_pages: int = 6,
) -> list[Path]:
    """Rasterize up to ``max_pages`` pages of *pdf_path* to numbered PNGs.

    A single-page PNG (page 1 only) silently truncated any report whose
    content overflowed the first printed page — exactly the failure mode hit
    by the multi-candidate limit-up scan report. Rendering every page (capped
    at ``max_pages`` so a runaway report cannot balloon into dozens of image
    attachments) lets the caller attach the full report as a sequence of
    images instead of just its first page.

    Args:
        pdf_path: Source PDF (already rendered).
        output_dir: Directory to write ``<report_id>.png`` (page 1) and
            ``<report_id>-p<N>.png`` (page 2+) into.
        report_id: Base filename stem.
        scale: Render scale (2.0 ~= 144 DPI).
        max_pages: Hard cap on pages rasterized, so a very long report still
            produces a bounded number of image attachments.

    Returns:
        Ordered list of PNG paths (page 1 first), empty on any failure. A
        partial failure part-way through keeps the pages rendered so far
        rather than discarding them.
    """
    try:
        import pypdfium2 as pdfium  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - optional dependency
        logger.warning("pypdfium2 unavailable (%s); PNG output skipped.", exc)
        return []

    try:
        pdf = pdfium.PdfDocument(str(pdf_path))
        page_count = min(len(pdf), max_pages)
    except Exception as exc:
        logger.warning("pypdfium2 failed to open %s (%s); PNG output skipped.", pdf_path, exc)
        return []

    out_paths: list[Path] = []
    try:
        for index in range(page_count):
            png_path = (
                output_dir / f"{report_id}.png"
                if index == 0
                else output_dir / f"{report_id}-p{index + 1}.png"
            )
            try:
                page = pdf.get_page(index)
                bitmap = page.render(scale=scale)
                pil_image = bitmap.to_pil()
                pil_image.save(str(png_path))
                page.close()
            except Exception as exc:
                logger.warning(
                    "pypdfium2 rasterization failed on page %d of %s (%s); "
                    "stopping with %d page(s) rendered.",
                    index + 1, pdf_path, exc, len(out_paths),
                )
                break
            out_paths.append(png_path)
    finally:
        pdf.close()
    return out_paths


class RenderHtmlReportTool(BaseTool):
    """Render agent-authored HTML into a servable, shareable report artifact.

    Use this for any report the user should be able to view as a formatted
    web page (not a wall of chat text) and/or receive as an image/PDF over a
    messaging channel like WeChat. Write complete, self-contained HTML
    (inline <style>, no external assets) — the renderer does not fetch
    remote resources.
    """

    name = "render_html_report"
    description = (
        "Render an HTML string into a report artifact: a viewable web page "
        "(served at a stable URL), a PDF, and a sequence of PNG page "
        "snapshots (suitable for sending as image attachments over IM "
        "channels like WeChat instead of a long wall of text). Multi-page "
        "content renders as multiple PNGs (up to 6 pages) so the report is "
        "never truncated to just its first page — use 'png_paths' (in order) "
        "for a multi-image IM send, or 'png_path' (== png_paths[0]) for a "
        "single-image send. Keep the HTML CONCISE and information-dense "
        "(compact tables over long prose paragraphs) so it fits in as few "
        "pages as possible — a shorter report is more likely to be read "
        "than one requiring several images. Provide complete, "
        "self-contained HTML — inline <style>, no external stylesheets or "
        "images. Chinese text is supported."
    )
    is_readonly = False
    parameters = {
        "type": "object",
        "properties": {
            "html": {
                "type": "string",
                "description": (
                    "Complete, self-contained HTML document (or an HTML "
                    "fragment — a minimal <html><body> wrapper is added if "
                    "missing). Use inline <style> for formatting."
                ),
            },
            "report_id": {
                "type": "string",
                "description": (
                    "Stable id for this report, e.g. 'premarket-0902' or "
                    "'weekly-review-0830'. Letters, digits, '_' and '-' "
                    "only, max 64 chars. Reusing an id overwrites the "
                    "previous report under that id. Auto-generated if omitted."
                ),
            },
            "title": {
                "type": "string",
                "description": "Optional <title> to inject if the HTML has none.",
            },
        },
        "required": ["html"],
    }
    repeatable = True

    def execute(self, **kwargs: Any) -> str:
        """Render HTML to HTML+PDF+PNG artifacts and return their paths/URLs.

        Args:
            **kwargs: ``html`` (required), optional ``report_id``, ``title``.

        Returns:
            JSON envelope: ``status``, ``report_id``, ``html_path``,
            ``pdf_path`` (nullable), ``png_path`` (nullable), ``report_url``,
            ``engine``.
        """
        html = kwargs.get("html")
        if not html or not isinstance(html, str):
            return json.dumps({"status": "error", "error": "'html' is required and must be a non-empty string"})

        report_id = kwargs.get("report_id") or f"report-{uuid.uuid4().hex[:12]}"
        if not _REPORT_ID_RE.match(report_id):
            return json.dumps(
                {
                    "status": "error",
                    "error": (
                        f"report_id {report_id!r} must be 1-64 characters of "
                        "letters, digits, '_' or '-'"
                    ),
                }
            )

        if "<html" not in html.lower():
            title = kwargs.get("title") or report_id
            html = (
                "<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\">"
                f"<title>{title}</title></head><body>{html}</body></html>"
            )

        # Inject a CJK @font-face so weasyprint renders Chinese text
        # correctly instead of falling back to blank glyphs. No-op if no
        # CJK font is resolvable on this machine (see shadow_account/fonts.py).
        try:
            from src.shadow_account.fonts import cjk_css_font_face

            font_css = cjk_css_font_face()
        except Exception:  # pragma: no cover - font probing must never block rendering
            font_css = ""
        if font_css and "</head>" in html:
            html = html.replace(
                "</head>",
                f"<style>{font_css} body, * {{ font-family: 'Shadow CJK', sans-serif; }}</style></head>",
                1,
            )

        out_dir = reports_dir()
        html_path = out_dir / f"{report_id}.html"
        try:
            html_path.write_text(html, encoding="utf-8")
        except Exception as exc:
            return json.dumps({"status": "error", "error": f"failed to write HTML: {exc}"})

        pdf_path = _try_render_pdf(html, out_dir, report_id)
        png_paths = _try_render_png_pages(pdf_path, out_dir, report_id) if pdf_path else []
        png_path = png_paths[0] if png_paths else None

        engine = "weasyprint+pypdfium2" if png_path else ("weasyprint" if pdf_path else "html-only")

        return json.dumps(
            {
                "status": "ok",
                "report_id": report_id,
                "html_path": str(html_path),
                "pdf_path": str(pdf_path) if pdf_path else None,
                "png_path": str(png_path) if png_path else None,
                "png_paths": [str(p) for p in png_paths],
                "report_url": f"/reports/{report_id}",
                "engine": engine,
            },
            ensure_ascii=False,
        )
