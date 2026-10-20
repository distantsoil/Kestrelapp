"""File-to-markdown conversion for the document importer (SPEC-MODULES 1.5, PROMPTS.md P7).

The operator hands over an existing resume or capability statement as .md, .txt, .docx, .pdf, or
legacy .doc. This module turns it into the markdown that P7 decomposes. Two properties matter more
than fidelity of layout:

  - the operator's WORDING is kept verbatim. Nothing here rewrites, reflows words, or "cleans up"
    prose, because imports._verbatim later compares every proposed variant against this text and
    a reworded source would make honest bullets look invented (hard rule 2);
  - nothing from the file is ever executed. python-docx and pypdf parse bytes; the only external
    process is LibreOffice for legacy .doc, run headless on a copy in a throwaway directory.

Structure is a best effort: DOCX headings, list styles, bold runs, and tables map to markdown;
PDF text extraction loses structure by nature, and that is reported as a warning so the operator
reviews what was read before proposing anything."""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from kestrel.errors import UserFacingError
from kestrel.llm.profiles import no_console_window

log = structlog.get_logger()

MAX_BYTES = 10 * 1024 * 1024  # hard limit, checked before any parser sees the bytes
SUPPORTED = (".md", ".txt", ".docx", ".pdf", ".doc")
_SOFFICE_TIMEOUT_SECONDS = 120

_PDF_WARNING = (
    "PDF text extraction loses structure (headings, columns, bullet nesting, tables). Review the "
    "extracted text before proposing; bullets were detected from leading glyphs only."
)
_DOC_HELP = (
    "Legacy .doc files need LibreOffice to convert. Save the file as .docx or .pdf and import "
    "that, or install LibreOffice so `soffice` is on PATH."
)


class ConvertError(UserFacingError, ValueError):
    """Base for every conversion failure the operator should see as a message, never a 500."""


class UnsupportedFormat(ConvertError):
    """The extension is not one the importer reads, or .doc without LibreOffice available."""


class FileTooLarge(ConvertError):
    """Over MAX_BYTES. Checked before parsing so a hostile file never reaches a parser."""


class NoExtractableText(ConvertError):
    """A PDF with no text layer (scanned). OCR is deliberately not part of this stack."""


@dataclass
class ConvertResult:
    markdown: str
    warnings: list[str] = field(default_factory=list)
    source_format: str = ""  # "md" | "txt" | "docx" | "pdf" | "doc"


def convert_to_markdown(
    path_or_bytes: str | Path | bytes, *, filename: str | None = None
) -> ConvertResult:
    """Convert a file (path, or raw bytes plus its filename) to markdown for P7.

    Raises UnsupportedFormat, FileTooLarge, or NoExtractableText (all ConvertError) with a message
    written for the operator. Any other parser failure is wrapped as ConvertError too."""
    if isinstance(path_or_bytes, (str, Path)):
        path = Path(path_or_bytes)
        name = filename or path.name
        if path.stat().st_size > MAX_BYTES:
            raise FileTooLarge(_too_large(name))
        data = path.read_bytes()
    else:
        if not filename:
            raise UnsupportedFormat("A filename with an extension is needed to convert bytes.")
        name = filename
        data = bytes(path_or_bytes)
    if len(data) > MAX_BYTES:
        raise FileTooLarge(_too_large(name))

    ext = Path(name).suffix.lower()
    if ext in (".md", ".txt"):
        text, warnings = _decode_text(data)
        return ConvertResult(markdown=text, warnings=warnings, source_format=ext[1:])
    if ext == ".docx":
        return _convert_docx(data, source_format="docx")
    if ext == ".pdf":
        return _convert_pdf(data)
    if ext == ".doc":
        return _convert_doc(data, name)
    raise UnsupportedFormat(
        f"Cannot import {name!r}: unsupported file type {ext or '(none)'!r}. "
        f"Supported: {', '.join(SUPPORTED)}."
    )


def _too_large(name: str) -> str:
    return f"{name!r} is over the {MAX_BYTES // (1024 * 1024)} MB import limit."


# ------------------------------------------------------------------------------ .md / .txt
def _decode_text(data: bytes) -> tuple[str, list[str]]:
    warnings: list[str] = []
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
        warnings.append("The file was not UTF-8; it was read as Windows-1252. Check accents.")
    return text.replace("\r\n", "\n").replace("\r", "\n"), warnings


# ------------------------------------------------------------------------------------- .docx
_HEADING_RE = re.compile(r"^heading\s*(\d)$", re.IGNORECASE)


def _convert_docx(data: bytes, *, source_format: str) -> ConvertResult:
    try:
        import docx  # python-docx; imported lazily so the CLI and web import nothing extra
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError as exc:  # pragma: no cover - dependency is declared in pyproject
        log.warning("convert_dependency_missing", library="python-docx", error=str(exc))
        raise ConvertError("python-docx is not installed.") from exc

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # a corrupt zip or a non-OOXML file wearing a .docx extension
        log.info("convert_docx_unreadable", error_type=type(exc).__name__, error=str(exc))
        raise ConvertError(
            f"Could not read the .docx file ({type(exc).__name__}). It may be damaged or not "
            "a Word document."
        ) from exc

    lines: list[str] = []
    warnings: list[str] = []
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            lines.extend(_docx_paragraph(Paragraph(child, document)))
        elif child.tag == qn("w:tbl"):
            lines.extend(_docx_table(Table(child, document), warnings))
            lines.append("")
    markdown = _squeeze_blank_lines("\n".join(lines))
    return ConvertResult(markdown=markdown, warnings=warnings, source_format=source_format)


def _docx_paragraph(p) -> list[str]:
    text = _docx_inline(p)
    if not text.strip():
        return [""]
    style = (p.style.name if p.style is not None else "") or ""
    m = _HEADING_RE.match(style.strip())
    if m:
        level = max(1, min(6, int(m.group(1))))
        return ["", "#" * level + " " + text.strip(), ""]
    if style.strip().lower() == "title":
        return ["", "# " + text.strip(), ""]
    if _docx_is_list(p, style):
        return ["- " + text.strip()]
    return [text]


def _docx_is_list(p, style: str) -> bool:
    if "list" in style.lower():
        return True
    # Direct numbering (a bullet applied from the toolbar rather than a named style).
    ppr = p._p.pPr
    return ppr is not None and ppr.numPr is not None


def _docx_inline(p) -> str:
    """Runs in order, bold runs wrapped in ** with the marks hugging the text (never the spaces).
    Hyperlink runs are included so no words are dropped."""
    pieces: list[tuple[str, bool]] = []
    for item in p.iter_inner_content():
        runs = getattr(item, "runs", None)
        if runs is None:  # a plain Run
            runs = [item]
        for run in runs:
            t = run.text
            if not t:
                continue
            pieces.append((t, bool(run.bold)))
    # Merge adjacent runs with the same boldness so "**Led** **IT**" does not fragment.
    merged: list[tuple[str, bool]] = []
    for t, bold in pieces:
        if merged and merged[-1][1] == bold:
            merged[-1] = (merged[-1][0] + t, bold)
        else:
            merged.append((t, bold))
    out: list[str] = []
    for t, bold in merged:
        if bold and t.strip():
            lead = t[: len(t) - len(t.lstrip())]
            trail = t[len(t.rstrip()) :]
            out.append(f"{lead}**{t.strip()}**{trail}")
        else:
            out.append(t)
    return "".join(out)


_NESTED_TABLE_WARNING = (
    "A table cell contains a nested table, which the importer does not read: its text is missing "
    "from the extracted markdown. Flatten that table in the source, or paste the missing text."
)


def _docx_table(table, warnings: list[str]) -> list[str]:
    """Rows as a markdown table. python-docx repeats one `_Cell` per grid column it spans, so a
    merged cell is skipped when its `_tc` is the previous cell's: the text appears once, not once
    per column (hard rule 2: nothing is duplicated into the operator's own wording)."""
    rows: list[list[str]] = []
    for row in table.rows:
        cells = []
        previous_tc = None
        for cell in row.cells:
            if cell._tc is previous_tc:
                continue
            previous_tc = cell._tc
            if cell.tables and _NESTED_TABLE_WARNING not in warnings:
                warnings.append(_NESTED_TABLE_WARNING)
            cell_text = " ".join(
                part.strip() for part in cell.text.replace("\r", "\n").split("\n") if part.strip()
            )
            cells.append(cell_text.replace("|", "\\|"))
        rows.append(cells)
    if not rows:
        return []
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["", "| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    out.extend("| " + " | ".join(r) + " |" for r in rows[1:])
    return out


# -------------------------------------------------------------------------------------- .pdf
_BULLET_RE = re.compile(r"^[ \t]*[•·●○◦▪■‣–][ \t]*")
_HYPHEN_BREAK_RE = re.compile(r"([A-Za-z]+)-\n([a-z]+)")
_WORD_RE = re.compile(r"[A-Za-z]+")
_HYPHEN_WARNING = (
    "Some words were split with a hyphen at a line end and the joined word does not appear "
    "elsewhere in the document, so the hyphen was kept (for example \"cross-functional\"). "
    "Check those words; a soft hyphen that should be removed reads as \"manage-ment\"."
)


def _convert_pdf(data: bytes) -> ConvertResult:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        log.warning("convert_dependency_missing", library="pypdf", error=str(exc))
        raise ConvertError("pypdf is not installed.") from exc

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:
                raise ConvertError("The PDF is password protected; remove the password.") from exc
        pages = [page.extract_text() or "" for page in reader.pages]
    except ConvertError:
        raise
    except Exception as exc:  # malformed PDF
        log.info("convert_pdf_unreadable", error_type=type(exc).__name__, error=str(exc))
        raise ConvertError(
            f"Could not read the PDF ({type(exc).__name__}). It may be damaged."
        ) from exc

    pages, dropped = _drop_page_furniture(pages)
    vocabulary = {w.lower() for w in _WORD_RE.findall("\n".join(pages))}
    kept_hyphen = False
    cleaned = []
    for p in pages:
        text, kept = _clean_pdf_page(p, vocabulary)
        kept_hyphen = kept_hyphen or kept
        if text.strip():
            cleaned.append(text)
    if not cleaned:
        raise NoExtractableText(
            "The PDF has no extractable text (it is probably a scanned image). Kestrel does not "
            "OCR; export the document from its source as .docx or a text PDF."
        )
    warnings = [_PDF_WARNING] + ([_HYPHEN_WARNING] if kept_hyphen else [])
    if dropped:
        shown = "; ".join(f'"{t[:60]}"' for t in dropped[:6])
        warnings.append(
            f"Removed page headers, footers and page numbers repeated across pages: {shown}. "
            "If one of these is resume content, import the .docx or paste the text instead."
        )
    return ConvertResult(markdown="\n\n".join(cleaned), warnings=warnings, source_format="pdf")


_PAGE_NO_RE = re.compile(r"^\s*(?:page\s*)?[-\u2013(]?\s*\d{1,3}\s*(?:(?:of|/)\s*\d{1,3})?"
                         r"\s*[-\u2013)]?\s*$", re.I)
_EDGE = 2  # lines at the top and bottom of a page that may be a running header or footer


def _drop_page_furniture(pages: list[str]) -> tuple[list[str], list[str]]:
    """Drop page numbers and running headers/footers (D-IMPORT-3). Conservative, because a line
    removed here is invisible to the import's coverage check:
      - a page number is a bare number or "Page n of m" in a page's top or bottom two lines;
      - a running header/footer is a non-bullet line, identical (digits included), in the top or
        bottom two lines of every page, on a document of two or more pages.
    Returns the pages and the distinct lines removed, which the caller lists to the operator."""
    split = [p.replace("\r\n", "\n").replace("\r", "\n").split("\n") for p in pages]

    def edges(lines: list[str]) -> set[int]:
        idx = [i for i, ln in enumerate(lines) if ln.strip()]
        return set(idx[:_EDGE] + idx[-_EDGE:])

    def edge_texts(lines: list[str]) -> set[str]:
        return {lines[i].strip() for i in edges(lines)
                if not _BULLET_RE.match(lines[i]) and not lines[i].lstrip().startswith("- ")}

    repeated: set[str] = set()
    if len(split) >= 2:
        repeated = set.intersection(*(edge_texts(ln) for ln in split))
    removed: list[str] = []
    out = []
    for lines in split:
        at_edge = edges(lines)
        kept = []
        for i, ln in enumerate(lines):
            t = ln.strip()
            if i in at_edge and t and (t in repeated or _PAGE_NO_RE.match(t)):
                if t not in removed:
                    removed.append(t)
                continue
            kept.append(ln)
        out.append("\n".join(kept))
    return out, removed


def _rejoin_wrapped_bullets(lines: list[str]) -> list[str]:
    """A bullet that wrapped in the PDF arrives as two lines. Rejoin only the unambiguous case: the
    next line is non-blank, is not itself a bullet, and starts lowercase, so a heading or a new
    paragraph after a bullet is never swallowed. The model can still map a missed wrap as one
    bullet over two lines (resume_role/v1 lines)."""
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if (out and out[-1].startswith("- ") and s and s[0].islower()
                and not _BULLET_RE.match(line)):
            out[-1] = f"{out[-1]} {s}"
            continue
        out.append(line)
    return out


def _join_hyphen_breaks(text: str, vocabulary: set[str]) -> tuple[str, bool]:
    """"manage-\nment" -> "management" ONLY when the joined word occurs elsewhere in the document
    unhyphenated (the vocabulary of every page), so a real hyphenated term split at a line end
    ("cross-\nfunctional") keeps its hyphen ("cross-functional") instead of becoming a word the
    operator never wrote. Only a lowercase continuation is considered either way. Returns the
    text and whether any hyphen was kept, so the caller can warn."""
    kept = False

    def _sub(m: re.Match[str]) -> str:
        nonlocal kept
        joined = m.group(1) + m.group(2)
        if joined.lower() in vocabulary:
            return joined
        kept = True
        return f"{m.group(1)}-{m.group(2)}"

    return _HYPHEN_BREAK_RE.sub(_sub, text), kept


def _clean_pdf_page(text: str, vocabulary: set[str] | None = None) -> tuple[str, bool]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text, kept = _join_hyphen_breaks(text, vocabulary or set())
    lines = []
    for line in text.split("\n"):
        line = line.rstrip()
        if _BULLET_RE.match(line) and _BULLET_RE.sub("", line).strip():
            line = "- " + _BULLET_RE.sub("", line).strip()
        lines.append(line)
    lines = _rejoin_wrapped_bullets(lines)
    return _squeeze_blank_lines("\n".join(lines)).strip(), kept


# --------------------------------------------------------------------------------------- .doc
def _soffice() -> str | None:
    return shutil.which("soffice") or shutil.which("libreoffice")


def _convert_doc(data: bytes, name: str) -> ConvertResult:
    exe = _soffice()
    if exe is None:
        raise UnsupportedFormat(f"Cannot import {name!r}: {_DOC_HELP}")
    with tempfile.TemporaryDirectory(prefix="kestrel-doc-") as tmp:
        tmp_path = Path(tmp)
        src = tmp_path / "input.doc"
        src.write_bytes(data)
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        # A private profile dir: a running desktop LibreOffice otherwise swallows the request and
        # exits 0 with no output. Headless conversion never runs document macros.
        profile = (tmp_path / "profile").as_uri()
        cmd = [
            exe, f"-env:UserInstallation={profile}", "--headless", "--norestore",
            "--convert-to", "docx", "--outdir", str(out_dir), str(src),
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=_SOFFICE_TIMEOUT_SECONDS, check=False,
                **no_console_window(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("convert_soffice_failed", error_type=type(exc).__name__, error=str(exc))
            raise ConvertError(
                f"LibreOffice could not convert {name!r} ({type(exc).__name__})."
            ) from exc
        produced = out_dir / "input.docx"
        if proc.returncode != 0 or not produced.exists():
            detail = (proc.stderr or proc.stdout or "").strip()[-300:]
            log.warning("convert_soffice_exit", exit_code=proc.returncode, detail=detail)
            raise ConvertError(
                f"LibreOffice could not convert {name!r} (exit {proc.returncode}). "
                "The details are in the log."
            )
        result = _convert_docx(produced.read_bytes(), source_format="doc")
    result.warnings.append("Converted from legacy .doc via LibreOffice; check the formatting.")
    return result


# ------------------------------------------------------------------------------------ helpers
_MANY_BLANKS = re.compile(r"\n{3,}")


def _squeeze_blank_lines(text: str) -> str:
    return _MANY_BLANKS.sub("\n\n", text).strip("\n") + ("\n" if text.strip() else "")
