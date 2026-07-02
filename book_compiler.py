"""
Book compiler.

Turn a folder of generated Markdown articles into a distributable book,
ready to upload to Amazon KDP (or any e-book store):

- **EPUB**  — reflowable e-book (Kindle, Apple Books, Kobo, ...). Pure Python,
  no system dependencies (uses ``EbookLib``).
- **PDF**   — print-ready interior with a real trim size (e.g. 6x9in), a table
  of contents with page numbers, and page numbering (uses ``weasyprint``).

Each Markdown file becomes one chapter. The YAML-ish front matter written by
``save_article_locally`` in the main script is parsed to recover the chapter
title, date (used to order chapters chronologically) and tags. Article-only
noise (the leading kicker line, embedded YouTube videos) is stripped so the
result reads like a book rather than a blog feed.

The heavy, dependency-carrying imports (``ebooklib``, ``weasyprint``) are done
lazily inside the exporters, so importing this module — and generating an EPUB —
never requires ``weasyprint``'s system libraries to be installed.
"""

from __future__ import annotations

import html
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import requests

# Markdown -> HTML (already a project dependency, reused for chapter bodies).
import markdown as md_lib


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class Chapter:
    """A single article turned into a book chapter."""
    title: str
    markdown: str
    date: datetime
    tags: List[str] = field(default_factory=list)
    source_path: Optional[str] = None


@dataclass
class Book:
    """A collection of chapters plus the metadata needed to publish it."""
    title: str
    author: str
    language: str
    chapters: List[Chapter]
    cover_image_path: Optional[str] = None
    subtitle: Optional[str] = None
    rights: Optional[str] = None


@dataclass
class ImageAsset:
    """A downloaded, locally-cached image referenced by one or more chapters."""
    url: str
    filename: str
    path: str
    media_type: str
    data: bytes


# ---------------------------------------------------------------------------
# Regular expressions (module-level so they compile once)
# ---------------------------------------------------------------------------
#: Grabs the URL from a Markdown image, ignoring any optional "title".
_IMAGE_URL_RE = re.compile(r"!\[[^\]]*\]\(\s*([^)\s]+)")

#: Matches a full Markdown image, optionally followed by an italic caption line.
_FIGURE_RE = re.compile(
    r'!\[(?P<alt>[^\]]*)\]\(\s*(?P<url>[^)\s]+)(?:\s+"[^"]*"|\s+\'[^\']*\')?\s*\)'
    r'(?:[ \t]*\n[ \t]*\*(?P<caption>[^\n*][^\n]*?)\*[ \t]*(?=\n|$))?'
)

#: A YouTube embed block (bare URL, optionally wrapped in "---" separators).
_YT_BLOCK_RE = re.compile(
    r"\n?[ \t]*---[ \t]*\n(?:[ \t]*\n)*"
    r"[ \t]*https?://(?:www\.)?(?:youtube\.com/(?:watch\?|embed/)|youtu\.be/)\S+[ \t]*\n"
    r"(?:[ \t]*\n)*[ \t]*---[ \t]*\n",
    re.IGNORECASE,
)

#: A bare YouTube URL sitting alone on its own line.
_YT_LINE_RE = re.compile(
    r"^[ \t]*https?://(?:www\.)?(?:youtube\.com/(?:watch\?|embed/)|youtu\.be/)\S+[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

#: A "bold only" line (used for the kicker printed above the chapter title).
_BOLD_ONLY_RE = re.compile(r"^\*\*[^*].*\*\*$")

#: Maps a Content-Type to a file extension for downloaded images.
_EXT_BY_MIME: Dict[str, str] = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/svg+xml": "svg",
}

# ---------------------------------------------------------------------------
# Book length model (used to hit a target page count, e.g. 90-110 pages)
# ---------------------------------------------------------------------------
#: Words that fit on one 6x9in trade-paperback page, calibrated against real
#: rendered output for this content (headings, figures and per-chapter page
#: breaks lower the effective density below the ~300 raw-text rule of thumb).
#: Override per book via ``words_per_page``.
DEFAULT_WORDS_PER_PAGE = 250

#: How much vertical space an image roughly "costs", expressed in words.
_IMAGE_PAGE_WORDS = 110

#: Pages reserved for front matter (title, copyright, table of contents).
_FRONT_MATTER_PAGES = 4

#: Trailing/inline "call to action" blocks read as blog noise when repeated in
#: every chapter of a book, so they are stripped. Any paragraph mentioning one
#: of these markers is removed. Override via ``cta_markers``.
DEFAULT_CTA_MARKERS: Tuple[str, ...] = (
    "ph7.me",
    "dev.to/",
)


# ---------------------------------------------------------------------------
# Front matter + chapter collection
# ---------------------------------------------------------------------------
def parse_front_matter(text: str) -> Tuple[Dict[str, str], str]:
    """
    Split a saved article into ``(metadata, body)``.

    The main script writes a lightweight ``---`` fenced block of ``key: value``
    lines. Returns an empty metadata dict (and the original text as body) when
    no front matter is present.
    """
    if not text.startswith("---"):
        return {}, text

    parts = text.split("---\n")
    if len(parts) < 3:
        return {}, text

    metadata: Dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip()

    body = "---\n".join(parts[2:]).strip()
    return metadata, body


def _parse_date(value: str) -> datetime:
    """Parse an ISO date from front matter, tolerating missing/invalid values."""
    if not value:
        return datetime.min
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        # Fall back to the date portion only (e.g. "2026-06-21").
        try:
            return datetime.fromisoformat(value[:10])
        except ValueError:
            return datetime.min


def _is_unpublished(filename: str) -> bool:
    """True for locally-saved drafts (``not_published_``/``notpublished`` marker)."""
    normalized = filename.lower().replace("_", "").replace("-", "")
    return "notpublished" in normalized


def _first_heading(body: str) -> Optional[str]:
    """Return the text of the first level-1 Markdown heading, if any."""
    match = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
    return match.group(1).strip() if match else None


def collect_chapters(source_dir: str, recursive: bool = False) -> List[Chapter]:
    """
    Read every eligible ``*.md`` file under ``source_dir`` into a ``Chapter``.

    - Skips unpublished drafts and hidden files.
    - Skips sub-directories unless ``recursive`` is True.
    - Titles come from the ``optimized_title`` front-matter field (falling back
      to the first heading, then the file name).
    - Chapters are returned sorted by date (oldest first) for a natural read.
    """
    if not os.path.isdir(source_dir):
        raise FileNotFoundError(f"Source directory not found: {source_dir}")

    chapters: List[Chapter] = []

    if recursive:
        walker = (
            os.path.join(root, name)
            for root, _dirs, files in os.walk(source_dir)
            for name in files
        )
    else:
        walker = (
            os.path.join(source_dir, name)
            for name in os.listdir(source_dir)
            if os.path.isfile(os.path.join(source_dir, name))
        )

    for file_path in walker:
        name = os.path.basename(file_path)
        if not name.lower().endswith(".md"):
            continue
        if name.startswith("."):
            continue
        if _is_unpublished(name):
            continue

        try:
            with open(file_path, "r", encoding="utf-8") as handle:
                raw = handle.read()
        except (OSError, UnicodeDecodeError) as exc:
            print(f"⚠ Skipping unreadable file {file_path}: {exc}")
            continue

        metadata, body = parse_front_matter(raw)
        if not body.strip():
            continue

        title = (
            metadata.get("optimized_title")
            or _first_heading(body)
            or os.path.splitext(name)[0]
        )
        tags = [t.strip() for t in metadata.get("tags", "").split(",") if t.strip()]

        chapters.append(
            Chapter(
                title=title,
                markdown=body,
                date=_parse_date(metadata.get("date", "")),
                tags=tags,
                source_path=file_path,
            )
        )

    chapters.sort(key=lambda c: c.date)
    return chapters


# ---------------------------------------------------------------------------
# Curation: dedupe, topic filtering, length targeting
# ---------------------------------------------------------------------------
def _normalize_title(title: str) -> str:
    """Lowercase, drop punctuation and collapse spaces for duplicate detection."""
    stripped = re.sub(r"[^\w\s]", " ", title.lower(), flags=re.UNICODE)
    return re.sub(r"\s+", " ", stripped).strip()


def deduplicate_chapters(chapters: List[Chapter]) -> List[Chapter]:
    """
    Drop chapters whose titles are effectively identical (keeping the first).

    Articles are often saved twice (e.g. accented vs. un-accented file names),
    which would otherwise repeat a whole chapter inside the book.
    """
    seen: set = set()
    unique: List[Chapter] = []
    for chapter in chapters:
        key = _normalize_title(chapter.title)
        if key in seen:
            continue
        seen.add(key)
        unique.append(chapter)
    return unique


def _normalize_term(term: str) -> str:
    """Normalise a tag/topic so ``self-improvement`` and ``self improvement`` match."""
    return re.sub(r"\s+", " ", re.sub(r"[-_]+", " ", term.lower())).strip()


def _chapter_matches(chapter: Chapter, topics_norm: List[str], match: str) -> bool:
    """True when a chapter's tags match the (already normalised) ``topics``."""
    tags_norm = [_normalize_term(t) for t in chapter.tags]

    def hits(topic: str) -> bool:
        return any(topic in tag or tag in topic for tag in tags_norm)

    if match == "all":
        return all(hits(topic) for topic in topics_norm)
    return any(hits(topic) for topic in topics_norm)


def filter_chapters_by_topic(
    chapters: List[Chapter],
    topics: Optional[List[str]] = None,
    exclude_topics: Optional[List[str]] = None,
    match: str = "any",
) -> List[Chapter]:
    """
    Keep only chapters whose tags line up with a single, coherent topic.

    - ``topics``: keep a chapter when its tags match (``match='any'`` by default,
      or ``'all'`` to require every topic).
    - ``exclude_topics``: drop a chapter when any tag matches one of these.

    Matching is case-insensitive and tolerant of ``-``/``_``/spacing, and works
    both ways (so ``productivity`` matches the tag ``productivity tips``).
    """
    result = chapters
    if topics:
        topics_norm = [_normalize_term(t) for t in topics if t.strip()]
        if topics_norm:
            result = [c for c in result if _chapter_matches(c, topics_norm, match)]
    if exclude_topics:
        exclude_norm = [_normalize_term(t) for t in exclude_topics if t.strip()]
        if exclude_norm:
            result = [c for c in result if not _chapter_matches(c, exclude_norm, "any")]
    return result


def _word_count(markdown_text: str) -> int:
    """Rough word count with Markdown punctuation stripped out."""
    text = re.sub(r"[#>*_`\[\]()!~\-]+", " ", markdown_text)
    return len(text.split())


def _chapter_pages(chapter: Chapter, words_per_page: int) -> int:
    """Estimate how many printed pages a chapter fills (min one, new page each)."""
    words = _word_count(chapter.markdown)
    images = len(_IMAGE_URL_RE.findall(chapter.markdown))
    effective = words + images * _IMAGE_PAGE_WORDS
    return max(1, math.ceil(effective / max(1, words_per_page)))


def estimate_pages(
    chapters: List[Chapter],
    words_per_page: int = DEFAULT_WORDS_PER_PAGE,
) -> int:
    """Estimate the finished page count (front matter + one page-break per chapter)."""
    body = sum(_chapter_pages(c, words_per_page) for c in chapters)
    return _FRONT_MATTER_PAGES + body if chapters else 0


def select_chapters_for_length(
    chapters: List[Chapter],
    *,
    min_pages: Optional[int] = None,
    max_pages: Optional[int] = None,
    words_per_page: int = DEFAULT_WORDS_PER_PAGE,
    max_chapters: Optional[int] = None,
) -> List[Chapter]:
    """
    Pick a run of chapters that lands close to a target page range.

    Chapters are added in order until ``max_pages`` would be exceeded (once
    ``min_pages`` is satisfied) or ``max_chapters`` is reached. When the target
    cannot be met with the available chapters, everything available is returned
    and the caller is expected to warn.
    """
    if min_pages is None and max_pages is None and max_chapters is None:
        return chapters

    selected: List[Chapter] = []
    pages = _FRONT_MATTER_PAGES
    for chapter in chapters:
        if max_chapters is not None and len(selected) >= max_chapters:
            break
        chapter_pages = _chapter_pages(chapter, words_per_page)
        min_met = min_pages is None or pages >= min_pages
        if (
            max_pages is not None
            and selected
            and pages + chapter_pages > max_pages
            and min_met
        ):
            break
        selected.append(chapter)
        pages += chapter_pages
    return selected


def tag_frequencies(source_dir: str, recursive: bool = False) -> List[Tuple[str, int]]:
    """
    Return ``(tag, count)`` pairs across a folder, most common first.

    Handy for discovering the coherent topics you can build a book around
    before committing to a ``topics`` filter.
    """
    counts: Dict[str, int] = {}
    for chapter in collect_chapters(source_dir, recursive=recursive):
        for tag in chapter.tags:
            key = tag.strip()
            if key:
                counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


# ---------------------------------------------------------------------------
# Markdown cleaning + rendering
# ---------------------------------------------------------------------------
def _strip_promo_sections(text: str, cta_markers: Tuple[str, ...]) -> str:
    """
    Remove promotional paragraphs (the same "listen to my podcast / join my
    mailing list" call to action every blog article ends with). Repeating it
    after each chapter would make the book feel like a feed, not a finished
    volume.

    Works wherever the promo sits (end of article or between sections): any
    blank-line-delimited paragraph mentioning a marker is dropped, then the
    horizontal rules left dangling around it are tidied away.
    """
    if not cta_markers:
        return text

    markers = tuple(m.lower() for m in cta_markers)
    paragraphs = re.split(r"\n[ \t]*\n", text)
    kept = [p for p in paragraphs if not any(m in p.lower() for m in markers)]

    tidy: List[str] = []
    for line in "\n\n".join(kept).split("\n"):
        if line.strip() == "---":
            previous = next(
                (item for item in reversed(tidy) if item.strip() != ""), None
            )
            if previous is None or previous.strip() == "---":
                continue  # drop a leading or duplicated horizontal rule
        tidy.append(line)

    while tidy and tidy[-1].strip() in ("", "---"):
        tidy.pop()

    return "\n".join(tidy).strip()


def _clean_markdown_for_book(
    markdown_text: str,
    cta_markers: Tuple[str, ...] = DEFAULT_CTA_MARKERS,
) -> str:
    """
    Remove blog-only artefacts so a chapter reads well in a book.

    - Strips embedded YouTube videos (bare URLs and their ``---`` wrappers).
    - Drops the leading "kicker" (a bold-only line printed above the title).
    - Removes promotional call-to-action paragraphs.
    - Leaves images, captions, quotes and the rest of the prose untouched.
    """
    text = _YT_BLOCK_RE.sub("\n", markdown_text)
    text = _YT_LINE_RE.sub("", text)
    text = _strip_promo_sections(text, cta_markers)

    # Drop a bold-only kicker line that appears before the first heading.
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            break  # reached the title without finding a kicker
        if _BOLD_ONLY_RE.match(stripped):
            del lines[index]
            break
    text = "\n".join(lines)

    # Collapse runs of 3+ blank lines left behind by the removals.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _inline_markdown(text: str) -> str:
    """Render a short span of Markdown (e.g. a caption) without the wrapping <p>."""
    rendered = md_lib.markdown(text, output_format="xhtml").strip()
    if rendered.startswith("<p>") and rendered.endswith("</p>"):
        rendered = rendered[3:-4]
    return rendered


def _render_chapter_html(chapter: Chapter, src_resolver: Callable[[str], str]) -> str:
    """
    Convert a chapter's Markdown to an XHTML body fragment.

    Images (with their optional italic caption) become ``<figure>`` elements so
    captions render as real ``<figcaption>`` text. ``src_resolver`` maps an
    original image URL to the reference used in the output (a local EPUB path or
    an absolute file URI for PDF).
    """
    def replace_figure(match: "re.Match[str]") -> str:
        url = match.group("url")
        alt = html.escape(match.group("alt") or "", quote=True)
        caption = match.group("caption")
        src = html.escape(src_resolver(url), quote=True)

        figure = f'\n\n<figure>\n<img src="{src}" alt="{alt}" />\n'
        if caption:
            figure += f"<figcaption>{_inline_markdown(caption.strip())}</figcaption>\n"
        figure += "</figure>\n\n"
        return figure

    cleaned = _FIGURE_RE.sub(replace_figure, chapter.markdown)
    return md_lib.markdown(
        cleaned,
        extensions=["extra", "sane_lists"],
        output_format="xhtml",
    )


# ---------------------------------------------------------------------------
# Image handling
# ---------------------------------------------------------------------------
def _extension_from_url(url: str) -> str:
    """Best-effort image extension guessed from the URL when no MIME is known."""
    lowered = url.lower()
    for ext in ("jpg", "jpeg", "png", "webp", "gif", "svg"):
        if f".{ext}" in lowered or f"fm={ext}" in lowered:
            return "jpg" if ext == "jpeg" else ext
    return "jpg"


def collect_image_urls(chapters: List[Chapter]) -> List[str]:
    """Return every unique remote image URL referenced across ``chapters`` (in order)."""
    seen = set()
    ordered: List[str] = []
    for chapter in chapters:
        for match in _IMAGE_URL_RE.finditer(chapter.markdown):
            url = match.group(1)
            if url.startswith(("http://", "https://")) and url not in seen:
                seen.add(url)
                ordered.append(url)
    return ordered


def _download_images(image_urls: List[str], assets_dir: str) -> Dict[str, ImageAsset]:
    """
    Download each image once into ``assets_dir``, returning a ``url -> asset`` map.

    Failures are non-fatal: a warning is printed and the URL is simply left out
    of the map (chapters then reference the remote URL directly).
    """
    os.makedirs(assets_dir, exist_ok=True)
    image_map: Dict[str, ImageAsset] = {}

    for index, url in enumerate(image_urls, start=1):
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()

            content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
            ext = _EXT_BY_MIME.get(content_type) or _extension_from_url(url)
            media_type = content_type or f"image/{'jpeg' if ext == 'jpg' else ext}"

            filename = f"img_{index:03d}.{ext}"
            path = os.path.join(assets_dir, filename)
            with open(path, "wb") as handle:
                handle.write(response.content)

            image_map[url] = ImageAsset(
                url=url,
                filename=filename,
                path=path,
                media_type=media_type,
                data=response.content,
            )
        except Exception as exc:  # noqa: BLE001 - want to continue on any failure
            print(f"⚠ Could not download image ({exc}): {url}")

    return image_map


# ---------------------------------------------------------------------------
# Front matter (title page + copyright page) shared by EPUB and PDF
# ---------------------------------------------------------------------------
def _copyright_text(book: Book) -> str:
    """Return the copyright-page body (author-supplied ``rights`` or a default)."""
    if book.rights:
        return book.rights
    year = datetime.now().year
    return (
        f"Copyright \u00a9 {year} {book.author}\n"
        "All rights reserved.\n\n"
        "No part of this book may be reproduced, distributed, or transmitted in "
        "any form without the prior written permission of the author, except for "
        "brief quotations embodied in reviews.\n\n"
        "First edition."
    )


def _title_page_html(book: Book) -> str:
    """Build the XHTML fragment for the title page."""
    parts = [f"<h1>{html.escape(book.title)}</h1>"]
    if book.subtitle:
        parts.append(f'<p class="subtitle">{html.escape(book.subtitle)}</p>')
    parts.append(f'<p class="author">{html.escape(book.author)}</p>')
    return f'<section class="title-page">{"".join(parts)}</section>'


def _copyright_page_html(book: Book) -> str:
    """Build the XHTML fragment for the copyright page."""
    lines = html.escape(_copyright_text(book)).replace("\n", "<br/>")
    return f'<section class="copyright-page"><p>{lines}</p></section>'


# ---------------------------------------------------------------------------
# EPUB export
# ---------------------------------------------------------------------------
_EPUB_CSS = """
body { font-family: Georgia, 'Times New Roman', serif; line-height: 1.6; }
h1 { font-size: 1.8em; margin: 1.2em 0 0.2em; }
h2 { font-size: 1.35em; margin: 1.4em 0 0.3em; }
h3 { font-size: 1.1em; color: #333; margin: 1.2em 0 0.3em; }
p { margin: 0 0 1em; text-align: justify; }
figure { margin: 1.5em 0; text-align: center; }
figure img { max-width: 100%; }
figcaption { font-size: 0.85em; color: #666; font-style: italic; margin-top: 0.4em; }
blockquote { margin: 1.5em 1em; padding-left: 1em; border-left: 3px solid #ccc; color: #444; font-style: italic; }
pre { background: #f4f4f4; padding: 1em; overflow-x: auto; font-size: 0.85em; }
code { font-family: 'Courier New', monospace; }
.title-page { text-align: center; margin-top: 25%; }
.title-page h1 { font-size: 2.2em; }
.title-page .subtitle { font-size: 1.2em; font-style: italic; color: #444; margin-top: 0.3em; }
.title-page .author { font-size: 1.1em; color: #333; margin-top: 2em; }
.copyright-page { font-size: 0.85em; color: #444; margin-top: 30%; }
""".strip()


def export_epub(book: Book, output_path: str, image_map: Dict[str, ImageAsset]) -> str:
    """Write ``book`` to an EPUB file at ``output_path`` and return that path."""
    from ebooklib import epub  # Lazy: pure-Python, but keep the module import light.

    epub_book = epub.EpubBook()
    slug = _slugify(book.title)
    epub_book.set_identifier(f"youtube-book-{slug}")
    epub_book.set_title(book.title)
    epub_book.set_language(book.language)
    epub_book.add_author(book.author)

    # Optional cover.
    if book.cover_image_path and os.path.isfile(book.cover_image_path):
        with open(book.cover_image_path, "rb") as handle:
            ext = os.path.splitext(book.cover_image_path)[1].lstrip(".") or "jpg"
            epub_book.set_cover(f"cover.{ext}", handle.read())

    # Shared stylesheet.
    css = epub.EpubItem(
        uid="style",
        file_name="style/book.css",
        media_type="text/css",
        content=_EPUB_CSS,
    )
    epub_book.add_item(css)

    # Register downloaded images once for the whole book.
    for asset in image_map.values():
        epub_book.add_item(
            epub.EpubItem(
                uid=f"img-{asset.filename}",
                file_name=f"images/{asset.filename}",
                media_type=asset.media_type,
                content=asset.data,
            )
        )

    def resolver(url: str) -> str:
        asset = image_map.get(url)
        return f"images/{asset.filename}" if asset else url

    def _front_matter_item(uid: str, file_name: str, body_html: str) -> "epub.EpubHtml":
        item = epub.EpubHtml(title=uid.title(), file_name=file_name, lang=book.language)
        item.content = (
            '<!DOCTYPE html>\n'
            f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{book.language}" '
            f'lang="{book.language}">\n'
            f"<head><title>{html.escape(book.title)}</title>"
            '<link rel="stylesheet" href="style/book.css" type="text/css"/></head>\n'
            f"<body>{body_html}</body>\n</html>"
        )
        item.add_item(css)
        epub_book.add_item(item)
        return item

    # KDP-style front matter: title page then copyright page.
    title_page = _front_matter_item("title", "title.xhtml", _title_page_html(book))
    copyright_page = _front_matter_item("copyright", "copyright.xhtml", _copyright_page_html(book))

    epub_chapters = []
    for index, chapter in enumerate(book.chapters):
        body_html = _render_chapter_html(chapter, resolver)
        item = epub.EpubHtml(
            title=chapter.title,
            file_name=f"chap_{index:02d}.xhtml",
            lang=book.language,
        )
        item.content = (
            '<!DOCTYPE html>\n'
            f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{book.language}" '
            f'lang="{book.language}">\n'
            f"<head><title>{html.escape(chapter.title)}</title>"
            '<link rel="stylesheet" href="style/book.css" type="text/css"/></head>\n'
            f"<body>{body_html}</body>\n</html>"
        )
        item.add_item(css)
        epub_book.add_item(item)
        epub_chapters.append(item)

    epub_book.toc = tuple(epub_chapters)
    epub_book.add_item(epub.EpubNcx())
    epub_book.add_item(epub.EpubNav())
    epub_book.spine = [title_page, copyright_page, "nav"] + epub_chapters

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    epub.write_epub(output_path, epub_book)
    return output_path


# ---------------------------------------------------------------------------
# PDF export
# ---------------------------------------------------------------------------
def _pdf_css(page_size: str) -> str:
    """Build the print stylesheet for a given KDP trim size (e.g. ``6in 9in``)."""
    return f"""
@page {{
    size: {page_size};
    margin: 0.75in 0.6in;
    @bottom-center {{ content: counter(page); font-family: Georgia, serif; font-size: 9pt; color: #666; }}
}}
@page :first {{ @bottom-center {{ content: none; }} }}

body {{ font-family: Georgia, 'Times New Roman', serif; font-size: 11pt; line-height: 1.5; color: #111; }}

.title-page {{ page-break-after: always; text-align: center; padding-top: 3in; }}
.title-page h1 {{ font-size: 2.4em; margin-bottom: 0.2em; }}
.title-page .subtitle {{ font-size: 1.3em; font-style: italic; color: #444; margin-bottom: 1em; }}
.title-page .author {{ font-size: 1.2em; color: #444; }}
.copyright-page {{ page-break-after: always; font-size: 9.5pt; color: #444; padding-top: 6in; }}

nav#toc {{ page-break-after: always; }}
nav#toc h2 {{ font-size: 1.6em; margin-bottom: 0.8em; }}
nav#toc ol {{ list-style: none; padding: 0; margin: 0; }}
nav#toc li {{ margin: 0.4em 0; }}
nav#toc a {{ text-decoration: none; color: #111; }}
nav#toc a::after {{ content: leader('.') ' ' target-counter(attr(href url), page); color: #666; }}

.chapter {{ page-break-before: always; }}
h1 {{ font-size: 1.8em; margin: 0 0 0.4em; }}
h2 {{ font-size: 1.35em; margin: 1.4em 0 0.3em; }}
h3 {{ font-size: 1.1em; color: #333; margin: 1.2em 0 0.3em; }}
p {{ margin: 0 0 0.8em; text-align: justify; }}
figure {{ margin: 1.4em 0; text-align: center; }}
figure img {{ max-width: 100%; }}
figcaption {{ font-size: 0.8em; color: #666; font-style: italic; margin-top: 0.3em; }}
blockquote {{ margin: 1.4em 1em; padding-left: 1em; border-left: 3px solid #ccc; color: #444; font-style: italic; }}
pre {{ background: #f4f4f4; padding: 0.8em; font-size: 0.8em; white-space: pre-wrap; word-wrap: break-word; }}
code {{ font-family: 'Courier New', monospace; }}
""".strip()


def export_pdf(
    book: Book,
    output_path: str,
    image_map: Dict[str, ImageAsset],
    page_size: str = "6in 9in",
) -> Optional[str]:
    """
    Write ``book`` to a print-ready PDF and return the path.

    ``weasyprint`` is imported lazily; if it (or its system libraries) is not
    available, a clear install hint is printed and ``None`` is returned so the
    rest of the compilation (e.g. the EPUB) still succeeds.
    """
    try:
        from weasyprint import HTML  # Lazy: pulls in pango/cairo system libs.
    except Exception as exc:  # noqa: BLE001 - ImportError or missing native libs
        print(
            "⚠ Skipping PDF export: weasyprint is unavailable "
            f"({exc}).\n"
            "  Install it with:  pip install weasyprint\n"
            "  On macOS you also need its native libs:  brew install pango"
        )
        return None

    def resolver(url: str) -> str:
        asset = image_map.get(url)
        return Path(asset.path).resolve().as_uri() if asset else url

    sections = [
        '<section class="title-page">'
        f"<h1>{html.escape(book.title)}</h1>"
        + (f'<p class="subtitle">{html.escape(book.subtitle)}</p>' if book.subtitle else "")
        + f'<p class="author">{html.escape(book.author)}</p>'
        "</section>",
        f'<section class="copyright-page"><p>'
        + html.escape(_copyright_text(book)).replace("\n", "<br/>")
        + "</p></section>",
    ]

    toc_items = "".join(
        f'<li><a href="#chap{index}">{html.escape(chapter.title)}</a></li>'
        for index, chapter in enumerate(book.chapters)
    )
    sections.append(f'<nav id="toc"><h2>Contents</h2><ol>{toc_items}</ol></nav>')

    for index, chapter in enumerate(book.chapters):
        body_html = _render_chapter_html(chapter, resolver)
        sections.append(f'<section class="chapter" id="chap{index}">{body_html}</section>')

    document = (
        "<!DOCTYPE html>\n"
        f'<html lang="{book.language}"><head><meta charset="utf-8">'
        f"<title>{html.escape(book.title)}</title>"
        f"<style>{_pdf_css(page_size)}</style></head>"
        f"<body>{''.join(sections)}</body></html>"
    )

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    HTML(string=document, base_url=os.getcwd()).write_pdf(output_path)
    return output_path


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _slugify(value: str) -> str:
    """Turn a title into a filesystem-safe, lowercase slug."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "book"


def compile_book(
    *,
    title: str,
    author: str,
    source_dir: str,
    output_dir: str,
    language: str = "en",
    formats: Tuple[str, ...] = ("epub", "pdf"),
    page_size: str = "6in 9in",
    embed_images: bool = True,
    recursive: bool = False,
    cover_image: Optional[str] = None,
    subtitle: Optional[str] = None,
    rights: Optional[str] = None,
    topics: Optional[List[str]] = None,
    exclude_topics: Optional[List[str]] = None,
    match: str = "any",
    min_pages: Optional[int] = None,
    max_pages: Optional[int] = None,
    words_per_page: int = DEFAULT_WORDS_PER_PAGE,
    max_chapters: Optional[int] = None,
    dedupe: bool = True,
    cta_markers: Tuple[str, ...] = DEFAULT_CTA_MARKERS,
) -> List[str]:
    """
    Compile a coherent, KDP-ready book from a folder of saved articles.

    The pipeline curates the raw articles into a single, consistent volume:

    1. Collect every publishable article as a chapter (chronological order).
    2. Drop duplicate chapters (``dedupe``).
    3. Keep only chapters on the book's topic (``topics``/``exclude_topics``).
    4. Clean each chapter (YouTube embeds, kicker, trailing promo CTA).
    5. Trim to a target page range (``min_pages``/``max_pages``/``max_chapters``).

    Returns the list of files that were successfully written. Images are
    downloaded once and embedded (EPUB) or referenced locally (PDF) unless
    ``embed_images`` is False.
    """
    print(f"\n\U0001F4DA Compiling book: {title!r}")
    print(f"   Source: {source_dir}  (recursive={recursive})")

    chapters = collect_chapters(source_dir, recursive=recursive)
    if not chapters:
        print(f"\u26A0 No publishable articles found in {source_dir}; nothing to compile.")
        return []

    if dedupe:
        before = len(chapters)
        chapters = deduplicate_chapters(chapters)
        if before != len(chapters):
            print(f"   Removed {before - len(chapters)} duplicate chapter(s).")

    if topics or exclude_topics:
        chapters = filter_chapters_by_topic(chapters, topics, exclude_topics, match)
        topic_label = ", ".join(topics) if topics else "(all)"
        print(f"   Topic filter [{topic_label}] -> {len(chapters)} matching chapter(s).")
        if not chapters:
            print("\u26A0 No chapters matched this topic; skipping book.")
            return []

    # Clean before measuring so the page estimate reflects the real body text.
    for chapter in chapters:
        chapter.markdown = _clean_markdown_for_book(chapter.markdown, cta_markers)

    chapters = select_chapters_for_length(
        chapters,
        min_pages=min_pages,
        max_pages=max_pages,
        words_per_page=words_per_page,
        max_chapters=max_chapters,
    )

    estimated = estimate_pages(chapters, words_per_page)
    print(f"   Selected {len(chapters)} chapter(s), ~{estimated} page(s) at {words_per_page} words/page.")
    if min_pages is not None and estimated < min_pages:
        print(
            f"\u26A0 Only ~{estimated} pages available (target was {min_pages}+). "
            "Add more topics, lower MIN_PAGES, or generate more articles."
        )

    slug = _slugify(title)
    image_map: Dict[str, ImageAsset] = {}
    if embed_images:
        image_urls = collect_image_urls(chapters)
        if image_urls:
            assets_dir = os.path.join(output_dir, ".book_assets", slug)
            print(f"   Downloading {len(image_urls)} image(s)...")
            image_map = _download_images(image_urls, assets_dir)

    book = Book(
        title=title,
        author=author,
        language=language,
        chapters=chapters,
        cover_image_path=cover_image,
        subtitle=subtitle,
        rights=rights,
    )

    os.makedirs(output_dir, exist_ok=True)
    output_base = os.path.join(output_dir, slug)
    written: List[str] = []

    for fmt in formats:
        fmt = fmt.lower().strip()
        try:
            if fmt == "epub":
                path = export_epub(book, f"{output_base}.epub", image_map)
                print(f"   ✓ EPUB written: {path}")
                written.append(path)
            elif fmt == "pdf":
                path = export_pdf(book, f"{output_base}.pdf", image_map, page_size=page_size)
                if path:
                    print(f"   ✓ PDF written:  {path}")
                    written.append(path)
            else:
                print(f"⚠ Unknown book format '{fmt}' (expected 'epub' or 'pdf'); skipping.")
        except Exception as exc:  # noqa: BLE001 - report and continue with other formats
            print(f"✗ Failed to write {fmt.upper()} for {title!r}: {exc}")

    return written
