"""Tests for html_report_tool: HTML->HTML+PDF+multi-page-PNG rendering.

weasyprint/pypdfium2 rendering is exercised for real (both are project
dependencies, no HTTP boundary to mock), using a temporary reports directory
so no test pollutes ``~/.vibe-trading/agent_reports``.
"""

from __future__ import annotations

import json

import pytest

from src.tools import html_report_tool
from src.tools.html_report_tool import RenderHtmlReportTool

pytest.importorskip("weasyprint")
pytest.importorskip("pypdfium2")


@pytest.fixture(autouse=True)
def _isolated_reports_dir(tmp_path, monkeypatch):
    """Redirect ``reports_dir()`` to a temp directory for every test here."""
    out_dir = tmp_path / "agent_reports"
    out_dir.mkdir()
    monkeypatch.setattr(html_report_tool, "reports_dir", lambda: out_dir)
    return out_dir


def _multi_page_html(pages: int = 3) -> str:
    """Build simple HTML with explicit page breaks so weasyprint paginates."""
    sections = "".join(
        f'<section style="page-break-after: always;"><h1>Page {i + 1}</h1>'
        f"<p>{'内容 ' * 200}</p></section>"
        for i in range(pages)
    )
    return f"<html><head><meta charset='utf-8'></head><body>{sections}</body></html>"


class TestSinglePage:
    def test_single_page_report_renders_one_png(self, _isolated_reports_dir):
        tool = RenderHtmlReportTool()
        out = json.loads(tool.execute(html="<html><body><h1>hi</h1></body></html>", report_id="single-page-test"))

        assert out["status"] == "ok"
        assert out["png_path"] is not None
        assert out["png_paths"] == [out["png_path"]]
        assert (_isolated_reports_dir / "single-page-test.png").is_file()


class TestMultiPage:
    def test_multi_page_report_renders_multiple_pngs_in_order(self, _isolated_reports_dir):
        tool = RenderHtmlReportTool()
        out = json.loads(tool.execute(html=_multi_page_html(3), report_id="multi-page-test"))

        assert out["status"] == "ok"
        assert len(out["png_paths"]) >= 2  # weasyprint may fold short pages; at least 2 expected
        assert out["png_path"] == out["png_paths"][0]
        for path in out["png_paths"]:
            assert path.rstrip("/").split("/")[-1].startswith("multi-page-test")

        # Page 1 has no suffix; page 2+ carry -p<N>.
        assert out["png_paths"][0].endswith("multi-page-test.png")
        if len(out["png_paths"]) > 1:
            assert "-p2.png" in out["png_paths"][1]

    def test_page_cap_limits_png_count(self, _isolated_reports_dir):
        tool = RenderHtmlReportTool()
        out = json.loads(tool.execute(html=_multi_page_html(12), report_id="capped-test"))

        assert out["status"] == "ok"
        assert len(out["png_paths"]) <= 6


class TestValidation:
    def test_missing_html_returns_error(self):
        out = json.loads(RenderHtmlReportTool().execute())
        assert out["status"] == "error"

    def test_invalid_report_id_returns_error(self):
        out = json.loads(RenderHtmlReportTool().execute(html="<p>x</p>", report_id="bad id!"))
        assert out["status"] == "error"
