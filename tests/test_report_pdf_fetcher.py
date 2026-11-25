"""PDF rendering loads only inline data and local static files."""

import os
import sys
import types
from pathlib import Path

import pytest

from app.services import report_pdf
from app.services.report_pdf import STATIC_ROOT, pdf_resource_allowed

STYLESHEET = (STATIC_ROOT / "css/reporting.css").as_uri()


@pytest.mark.parametrize("url", [
    "data:image/png;base64,iVBORw0KGgo=",
    "DATA:text/plain,hello",
    STYLESHEET,
])
def test_allowed_resources(url):
    assert pdf_resource_allowed(url)


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",
    "https://example.com/logo.png",
    "ftp://example.com/file",
    "file:///etc/passwd",
    Path(report_pdf.__file__).resolve().as_uri(),
    (STATIC_ROOT / "css/../../core/config.py").as_uri(),
    (STATIC_ROOT / "css/missing.css").as_uri(),
    "file://remote-host" + str(STATIC_ROOT / "css/reporting.css"),
    "javascript:alert(1)",
    "",
    None,
])
def test_refused_resources(url):
    assert not pdf_resource_allowed(url)


@pytest.fixture
def fake_weasyprint(monkeypatch):
    """A stand-in library that records which fetcher each object received."""
    seen = {}

    class URLFetcher:
        def fetch(self, url, headers=None):
            seen.setdefault("fetched", []).append(url)
            return "fetched"

    class HTML:
        def __init__(self, string=None, base_url=None, url_fetcher=None):
            seen["html_fetcher"] = url_fetcher
            seen["base_url"] = base_url

        def write_pdf(self, stylesheets=None):
            seen["stylesheets"] = stylesheets
            return b"%PDF-fake"

    class CSS:
        def __init__(self, filename=None, url_fetcher=None):
            self.url_fetcher = url_fetcher

    package = types.ModuleType("weasyprint")
    package.HTML, package.CSS = HTML, CSS
    urls = types.ModuleType("weasyprint.urls")
    urls.URLFetcher = URLFetcher
    package.urls = urls
    monkeypatch.setitem(sys.modules, "weasyprint", package)
    monkeypatch.setitem(sys.modules, "weasyprint.urls", urls)
    return seen


def test_html_and_stylesheet_share_the_restrictive_fetcher(fake_weasyprint, monkeypatch, tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "tiny.html").write_text("<p>{{ title }}</p>", encoding="utf-8")
    monkeypatch.setattr(report_pdf, "ROOT", tmp_path)

    output = report_pdf._render("tiny.html", title="Report")

    fetcher = fake_weasyprint["html_fetcher"]
    assert output == b"%PDF-fake"
    assert fetcher is not None
    assert [sheet.url_fetcher for sheet in fake_weasyprint["stylesheets"]] == [fetcher]
    assert fake_weasyprint["base_url"].startswith(str(STATIC_ROOT))


def test_restrictive_fetcher_refuses_before_the_library_fetches(fake_weasyprint):
    fetcher = report_pdf.local_only_fetcher()

    with pytest.raises(report_pdf.BlockedPdfResource):
        fetcher.fetch("http://127.0.0.1:8080/admin")
    with pytest.raises(report_pdf.BlockedPdfResource):
        fetcher.fetch("file:///etc/passwd")
    assert "fetched" not in fake_weasyprint

    assert fetcher.fetch(STYLESHEET) == "fetched"
    assert fake_weasyprint["fetched"] == [STYLESHEET]


def _real_weasyprint():
    try:
        from weasyprint.urls import URLFetcher  # noqa: F401
    except Exception:  # noqa: BLE001 - missing package or system libraries
        if os.environ.get("REPORTLUME_REQUIRE_PDF") == "1":
            pytest.fail("WeasyPrint is required here but cannot be imported")
        pytest.skip("WeasyPrint is not installed")


def test_real_library_refuses_network_and_outside_files():
    _real_weasyprint()
    fetcher = report_pdf.local_only_fetcher()

    for url in ("http://127.0.0.1:9/x.png", "file:///etc/hostname"):
        with pytest.raises(report_pdf.BlockedPdfResource):
            fetcher.fetch(url)
    response = fetcher.fetch(STYLESHEET)
    try:
        assert response.read()
    finally:
        response.close()


def test_real_pdf_skips_an_external_image():
    _real_weasyprint()
    from weasyprint import HTML

    markup = '<p>Report</p><img src="http://127.0.0.1:9/tracker.png">'
    calls = []

    class Recording(type(report_pdf.local_only_fetcher())):
        def fetch(self, url, headers=None):
            calls.append(url)
            return super().fetch(url, headers)

    pdf = HTML(string=markup, url_fetcher=Recording()).write_pdf()
    assert pdf[:5] == b"%PDF-"
    assert "http://127.0.0.1:9/tracker.png" in calls
