"""Extract plain text, with page boundaries, from supported file types."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

SUPPORTED_SUFFIXES = frozenset({".pdf", ".html", ".htm", ".docx", ".txt", ".md"})


class ExtractionError(Exception):
    """A file could not be turned into text."""


@dataclass(frozen=True)
class Extracted:
    pages: list[str]
    title: str

    @property
    def has_text(self) -> bool:
        return any(page.strip() for page in self.pages)

    def normalized(self) -> str:
        return " ".join(" ".join(self.pages).split())

    def text_sha256(self) -> str:
        return hashlib.sha256(self.normalized().encode("utf-8")).hexdigest()


def extract(path: Path) -> Extracted:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ExtractionError(f"unsupported file type: {suffix or '(none)'}")
    try:
        if suffix == ".pdf":
            pages, meta_title = _pdf(path)
        elif suffix in {".html", ".htm"}:
            pages, meta_title = [html_to_text(path.read_text(encoding="utf-8", errors="replace"))], None
        elif suffix == ".docx":
            pages, meta_title = [_docx(path)], None
        else:
            pages, meta_title = [path.read_text(encoding="utf-8", errors="replace")], None
    except Exception as exc:
        raise ExtractionError(f"could not read {path.name}: {exc}") from exc
    return Extracted(pages=pages, title=_title(meta_title, pages, path))


def _title(meta_title: str | None, pages: list[str], path: Path) -> str:
    if meta_title and meta_title.strip():
        return meta_title.strip()[:200]
    for page in pages:
        for line in page.splitlines():
            cleaned = line.strip().lstrip("#").strip()
            if cleaned:
                return cleaned[:200]
    return path.stem


def _pdf(path: Path) -> tuple[list[str], str | None]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = [page.extract_text() or "" for page in reader.pages]
    metadata = reader.metadata
    return pages, (metadata.title if metadata is not None else None)


def _docx(path: Path) -> str:
    import docx

    document = docx.Document(str(path))
    return "\n\n".join(p.text for p in document.paragraphs if p.text.strip())


_BLOCK_TAGS = frozenset(
    {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "article", "pre", "blockquote"}
)
_SKIP_TAGS = frozenset({"script", "style", "head", "noscript"})


class _TextCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag == "br":
            self.parts.append("\n")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    collector = _TextCollector()
    collector.feed(markup)
    collector.close()
    text = "".join(collector.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
