"""Turn an uploaded file or a web page into the markdown the ingester chunks.

The ingester (`ingest.py`) understands one thing: markdown with headings. Every
other format the panel accepts -- PDF, Word, CSV, plain text, a website -- is
reduced to that here, so chunking, embedding and retrieval have exactly one
input shape to be correct about.

Two rules shape the extraction:

**Structure is kept where the source has it.** Headings become ``#`` lines,
table rows become one line each, list items keep their bullets. The chunker
splits on headings and keeps a section path per chunk, which is what lets the
agent cite "उत्पाद सूची, page 17" rather than "somewhere in the PDF".

**Nothing is fetched that was not asked for.** A website crawl stays on the
host it was given and stops at ``max_pages``. The agent's knowledge base is
curated, not scraped; the crawl is a convenience for a site the operator
already trusts.

Parsers are imported lazily so a worker without ``pypdf`` can still ingest a
text file, and the error names the package rather than failing on import.
"""

from __future__ import annotations

import csv
import io
import re
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
import structlog

from uaagro_domain.errors import ValidationError
from uaagro_domain.netsafety import ensure_reachable_target

log = structlog.get_logger(__name__)

#: Formats the panel's upload accepts, by extension. The content type is not
#: trusted: browsers send whatever the OS guesses, and a ``.csv`` arrives as
#: ``application/vnd.ms-excel`` from half of Windows.
SUPPORTED = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".csv": "csv",
    ".txt": "text",
    ".md": "markdown",
    ".markdown": "markdown",
}

#: Uploads larger than this are refused before extraction starts. A product
#: catalogue is a few megabytes; anything larger is a scan or a mistake.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

#: A website crawl is bounded twice: pages, and bytes per page.
DEFAULT_MAX_PAGES = 100
MAX_PAGE_BYTES = 2 * 1024 * 1024
CRAWL_TIMEOUT_S = 15.0
USER_AGENT = "UAAgro-KnowledgeBase/1.0 (+helpline knowledge import)"
#: Redirects are followed by hand so every hop is checked against the same
#: rule as the first address; this many is a loop.
MAX_REDIRECTS = 5


@dataclass(frozen=True, slots=True)
class Extracted:
    """One document's worth of markdown, plus what the panel shows about it."""

    markdown: str
    doc_type: str
    page_count: int | None = None
    title: str | None = None
    source: str | None = None


def doc_type_for(filename: str) -> str:
    """The format a filename implies, or a validation error naming the choice."""
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    try:
        return SUPPORTED[suffix]
    except KeyError:
        raise ValidationError(
            f"Cannot read {filename!r}.",
            remedy="Upload a PDF, a Word document (.docx), a spreadsheet saved as "
            "CSV, or a text or Markdown file.",
        ) from None


def extract_file(data: bytes, *, filename: str) -> Extracted:
    """Reduce an uploaded file to markdown."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValidationError(
            f"{filename!r} is {len(data) // (1024 * 1024)} MB.",
            remedy=f"Upload files under {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
        )
    doc_type = doc_type_for(filename)
    title = _title_from_filename(filename)
    match doc_type:
        case "pdf":
            text, pages = _pdf(data)
            return Extracted(text, "pdf", page_count=pages, title=title, source=filename)
        case "docx":
            return Extracted(_docx(data), "docx", title=title, source=filename)
        case "csv":
            text, rows = _csv(data)
            return Extracted(text, "csv", page_count=rows, title=title, source=filename)
        case _:
            text = data.decode("utf-8", errors="replace")
            return Extracted(_clean(text), doc_type, title=title, source=filename)


# --------------------------------------------------------------------------- #
# File formats
# --------------------------------------------------------------------------- #


def _pdf(data: bytes) -> tuple[str, int]:
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - declared dependency
        raise ValidationError(
            "PDF support is not installed on this worker.",
            remedy="Install the `pypdf` package (it is in the worker's dependencies).",
        ) from None
    reader = PdfReader(io.BytesIO(data))
    parts: list[str] = []
    for number, page in enumerate(reader.pages, start=1):
        text = _clean(page.extract_text() or "")
        if not text:
            continue
        # A heading per page gives the chunker a section boundary and gives
        # the agent something citable. Documents with their own headings keep
        # them as well; the page heading is one level above.
        parts.append(f"# Page {number}\n\n{text}")
    if not parts:
        raise ValidationError(
            "The PDF contains no readable text.",
            remedy="It is probably a scan. Export it with text (OCR) and upload again.",
        )
    return "\n\n".join(parts), len(reader.pages)


def _docx(data: bytes) -> str:
    try:
        import docx
    except ImportError:  # pragma: no cover - declared dependency
        raise ValidationError(
            "Word support is not installed on this worker.",
            remedy="Install the `python-docx` package (it is in the worker's dependencies).",
        ) from None
    document = docx.Document(io.BytesIO(data))
    lines: list[str] = []
    for paragraph in document.paragraphs:
        text = " ".join(paragraph.text.split())
        if not text:
            continue
        style = (paragraph.style.name or "") if paragraph.style is not None else ""
        level = _heading_level(style)
        lines.append(f"{'#' * level} {text}" if level else text)
    for table in document.tables:
        lines.append("")
        for row in table.rows:
            cells = [" ".join(cell.text.split()) for cell in row.cells]
            if any(cells):
                lines.append("| " + " | ".join(cells) + " |")
    text = "\n\n".join(lines)
    if not text.strip():
        raise ValidationError("The document is empty.", remedy="Check the file and upload again.")
    return text


def _heading_level(style_name: str) -> int:
    match = re.match(r"heading\s*(\d)", style_name.strip().lower())
    if match:
        return min(int(match.group(1)), 6)
    return 1 if style_name.strip().lower() == "title" else 0


def _csv(data: bytes) -> tuple[str, int]:
    """One line per row, labelled by column, so a row is a self-contained fact.

    A markdown table would keep the shape, but the chunker splits long tables
    mid-row and a chunk that reads "| 1250 | yes |" has lost its meaning.
    "Product: DAP · Price: 1250 · In stock: yes" survives any split.
    """
    text = data.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    rows = [row for row in rows if any(cell.strip() for cell in row)]
    if len(rows) < 2:
        raise ValidationError(
            "The spreadsheet has no data rows.",
            remedy="The first row must be the column names, followed by at least one row.",
        )
    header = [cell.strip() or f"column {i + 1}" for i, cell in enumerate(rows[0])]
    lines = []
    for row in rows[1:]:
        pairs = [
            f"{name}: {value.strip()}"
            for name, value in zip(header, row, strict=False)
            if value.strip()
        ]
        if pairs:
            lines.append(" · ".join(pairs))
    return "\n\n".join(lines), len(lines)


def _title_from_filename(filename: str) -> str:
    stem = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    stem = stem.rsplit(".", 1)[0] if "." in stem else stem
    return " ".join(stem.replace("_", " ").replace("-", " ").split()) or filename


def _clean(text: str) -> str:
    """Collapse whitespace without flattening paragraphs."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# --------------------------------------------------------------------------- #
# Websites
# --------------------------------------------------------------------------- #


class _Page(HTMLParser):
    """Reduce an HTML page to markdown-ish text, keeping headings and lists."""

    _SKIP = frozenset(
        {"script", "style", "noscript", "nav", "footer", "header", "svg", "form", "iframe"}
    )
    _BLOCK = frozenset(
        {"p", "div", "section", "article", "li", "tr", "br", "table", "ul", "ol"}
        | {"h1", "h2", "h3", "h4", "h5", "h6"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.links: list[str] = []
        self.title: str | None = None
        self._buffer: list[str] = []
        self._skip_depth = 0
        self._heading: int | None = None
        self._in_title = False
        self._cells: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
        elif tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._flush()
            self._heading = int(tag[1])
        elif tag == "tr":
            self._flush()
            self._cells = []
        elif tag in {"td", "th"} and self._cells is not None:
            self._flush_cell()
        elif tag == "li":
            self._flush()
            self._buffer.append("- ")
        elif tag in self._BLOCK:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._flush()
            self._heading = None
        elif tag == "tr" and self._cells is not None:
            self._flush_cell()
            cells = [c for c in self._cells if c]
            if cells:
                self.lines.append("| " + " | ".join(cells) + " |")
            self._cells = None
        elif tag in self._BLOCK:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title = (self.title or "") + data
            return
        self._buffer.append(data)

    def _flush_cell(self) -> None:
        if self._cells is None:
            return
        text = " ".join("".join(self._buffer).split())
        self._buffer.clear()
        if text:
            self._cells.append(text)

    def _flush(self) -> None:
        if self._cells is not None:
            self._flush_cell()
            return
        text = " ".join("".join(self._buffer).split())
        self._buffer.clear()
        if not text:
            return
        if self._heading:
            self.lines.append(f"{'#' * self._heading} {text}")
        else:
            self.lines.append(text)

    def markdown(self) -> str:
        self._flush()
        return "\n\n".join(self.lines)


def _same_site(base: str, candidate: str) -> str | None:
    """Resolve a link and keep it only if it stays on the site being crawled."""
    absolute = urljoin(base, candidate)
    parts = urlsplit(absolute)
    if parts.scheme not in {"http", "https"}:
        return None
    if parts.netloc.lower() != urlsplit(base).netloc.lower():
        return None
    # Drop fragments and the tracking half of query strings; two URLs that
    # differ only by "?utm_source=" are one page.
    path = parts.path or "/"
    if re.search(r"\.(pdf|jpg|jpeg|png|gif|webp|svg|zip|mp4|mp3|css|js)$", path, re.I):
        return None
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


async def _fetch_page(http: httpx.AsyncClient, start: str, page_url: str) -> str | None:
    """One page's HTML, or None when it is not there, not HTML, or not safe.

    Redirects are followed by hand: each destination must stay on the site
    and pass the public-address check, so a page that redirects to
    ``http://169.254.169.254/`` is dropped rather than fetched.
    """
    url = page_url
    for _ in range(MAX_REDIRECTS + 1):
        try:
            async with http.stream("GET", url) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    target = _same_site(start, location) if location else None
                    if target is None:
                        return None
                    host = urlsplit(target).hostname or ""
                    try:
                        ensure_reachable_target(host, purpose="the knowledge crawler")
                    except ValidationError:
                        log.info("extract.redirect_refused")
                        return None
                    url = target
                    continue
                if response.status_code != 200:
                    return None
                if "html" not in response.headers.get("content-type", ""):
                    return None
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_PAGE_BYTES:
                        chunks.append(chunk[: MAX_PAGE_BYTES - (size - len(chunk))])
                        break
                    chunks.append(chunk)
                encoding = response.encoding or "utf-8"
                return b"".join(chunks).decode(encoding, "replace")
        except httpx.HTTPError as exc:
            log.info("extract.page_unreachable", error=type(exc).__name__)
            return None
    return None


async def fetch_site(
    url: str, *, max_pages: int = DEFAULT_MAX_PAGES, client: httpx.AsyncClient | None = None
) -> Extracted:
    """Crawl one website into a single markdown document, page by page.

    Breadth first from the page given, staying on its host. Each page becomes
    a section headed by its title and URL, so retrieval can say which page an
    answer came from and the panel can link to it.
    """
    start = _same_site(url, url)
    if start is None:
        raise ValidationError(
            f"{url!r} is not a web address that can be read.",
            remedy="Give the full address, starting with https://",
        )
    # §17: the crawler runs inside the deployment. The address it is given,
    # and every address a redirect sends it to, must be public -- never the
    # database, the object store, another service, or the cloud's metadata
    # endpoint. Refused before the first byte is requested.
    ensure_reachable_target(urlsplit(start).hostname or "", purpose="the knowledge crawler")
    owned = client is None
    http = client or httpx.AsyncClient(
        timeout=CRAWL_TIMEOUT_S,
        follow_redirects=False,
        headers={"user-agent": USER_AGENT},
    )
    queue: deque[str] = deque([start])
    seen: set[str] = {start}
    sections: list[str] = []
    site_title: str | None = None
    try:
        while queue and len(sections) < max_pages:
            page_url = queue.popleft()
            fetched = await _fetch_page(http, start, page_url)
            if fetched is None:
                continue
            body = fetched
            page = _Page()
            page.feed(body)
            text = page.markdown()
            title = " ".join((page.title or "").split()) or page_url
            site_title = site_title or title
            if text.strip():
                sections.append(f"# {title}\n\nSource: {page_url}\n\n{text}")
            for link in page.links:
                resolved = _same_site(start, link)
                if resolved and resolved not in seen:
                    seen.add(resolved)
                    queue.append(resolved)
    finally:
        if owned:
            await http.aclose()

    if not sections:
        raise ValidationError(
            "No readable pages were found at that address.",
            remedy="Check the address opens in a browser and is not behind a login.",
        )
    return Extracted(
        "\n\n".join(sections),
        "web",
        page_count=len(sections),
        title=site_title,
        source=start,
    )


def extract_many(items: Iterable[Extracted]) -> str:
    """Join several extractions into one document, in order."""
    return "\n\n".join(item.markdown for item in items)


__all__ = (
    "DEFAULT_MAX_PAGES",
    "MAX_UPLOAD_BYTES",
    "SUPPORTED",
    "Extracted",
    "doc_type_for",
    "extract_file",
    "extract_many",
    "fetch_site",
)
