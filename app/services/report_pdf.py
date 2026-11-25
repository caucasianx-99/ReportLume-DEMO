"""PDF output from the same frozen snapshot and shared HTML report body.

The report and case-study exporters deliberately use separate templates,
but the same local print stylesheet.

PDF rendering never loads a network resource. Only inline data: URIs (the
report logo) and files inside the application's static folder are allowed;
every other URL is refused before WeasyPrint opens it.
"""

from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from jinja2 import (
    Environment,
    FileSystemLoader,
    select_autoescape,
)

from app.core.firestore_rest import FirestoreUnavailable


ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = (ROOT / "static").resolve()


class BlockedPdfResource(ValueError):
    """A PDF resource outside the local allow-list."""


def pdf_resource_allowed(url) -> bool:
    """True only for data: URIs and files under the static folder."""
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    scheme = parts.scheme.lower()
    if scheme == "data":
        return True
    if scheme != "file" or parts.netloc not in ("", "localhost"):
        return False
    try:
        path = Path(url2pathname(unquote(parts.path))).resolve()
    except (OSError, ValueError):
        return False
    return path.is_relative_to(STATIC_ROOT) and path.is_file()


def local_only_fetcher():
    """A WeasyPrint URL fetcher that refuses everything off the allow-list."""
    from weasyprint.urls import URLFetcher

    class LocalOnlyFetcher(URLFetcher):
        def fetch(self, url, headers=None):
            if not pdf_resource_allowed(url):
                raise BlockedPdfResource(
                    "PDF output loads only local static files."
                )
            return super().fetch(url, headers)

    return LocalOnlyFetcher()


def _render(template_name, **context):
    try:
        from weasyprint import HTML, CSS

        fetcher = local_only_fetcher()

    except (ImportError, OSError) as exc:
        raise FirestoreUnavailable(
            "PDF renderer is unavailable on this server."
        ) from exc

    env = Environment(
        loader=FileSystemLoader(
            str(ROOT / "templates")
        ),
        autoescape=select_autoescape(
            ["html", "xml"]
        ),
    )

    try:
        markup = env.get_template(
            template_name
        ).render(**context)

        # The stylesheet is provided locally, so PDF output does not
        # fetch external assets or depend on the request's proxy scheme.
        # The same restrictive fetcher is given to the HTML and the CSS.
        return HTML(
            string=markup,
            base_url=str(STATIC_ROOT) + "/",
            url_fetcher=fetcher,
        ).write_pdf(
            stylesheets=[
                CSS(
                    filename=str(
                        STATIC_ROOT / "css/reporting.css"
                    ),
                    url_fetcher=fetcher,
                )
            ]
        )

    except Exception as exc:
        raise FirestoreUnavailable(
            "PDF rendering failed."
        ) from exc


def pdf_bytes(snapshot):
    if (
        not isinstance(snapshot, dict)
        or not snapshot.get("period")
    ):
        raise FirestoreUnavailable(
            "Invalid report snapshot."
        )

    return _render(
        "report_document.html",
        snapshot=snapshot,
    )


def case_pdf_bytes(case, *, draft=False):
    if not isinstance(case, dict):
        raise FirestoreUnavailable(
            "Invalid case-study document."
        )

    return _render(
        "case_document.html",
        case=case,
        draft=draft,
    )
