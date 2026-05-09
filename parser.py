"""
parser.py - Document Parsing with Smart Chunking

Supports:
  - PDF: pdfplumber with table detection and page tagging
  - HTML: BeautifulSoup text extraction
  - DOCX: python-docx with table support
  - Smart boundary-aware chunking (respects section headers & page boundaries)
"""

import os
import io
import re
import time
from bs4 import BeautifulSoup
import fitz  # PyMuPDF - MUCH faster than pdfplumber
import docx
import logger as log


# ---------------------------------------------------------------------------
# Section header detection — used by smart_chunk
# ---------------------------------------------------------------------------
_SECTION_PATTERNS = re.compile(
    r"^("
    r"SECTION\s+\w+"           # SECTION 1
    r"|ARTICLE\s+\w+"          # ARTICLE II
    r"|PART\s+\w+"             # PART A
    r"|\d+\.\s+[A-Z]"          # 1. GENERAL TERMS
    r"|[IVX]+\.\s+[A-Z]"       # IV. SCOPE OF WORK
    r"|[A-Z][A-Z\s]{4,}$"      # All-caps headings (min 5 chars)
    r")",
    re.MULTILINE
)


# ---------------------------------------------------------------------------
# PDF Parser — pdfplumber with structured table tagging
# ---------------------------------------------------------------------------
def parse_pdf(file_content: bytes) -> str:
    """
    Extract text from PDF using PyMuPDF (fitz) for extreme speed.
    (Reduces 62-page parse time from 107 seconds to <1 second).
    """
    log.info(f"parse_pdf START | size={len(file_content):,} bytes", ctx="PARSER")
    t0 = time.perf_counter()
    sections = []
    
    with fitz.open(stream=file_content, filetype="pdf") as doc:
        page_count = len(doc)
        log.info(f"PDF opened | pages={page_count}", ctx="PARSER")

        for page_num in range(page_count):
            page = doc[page_num]
            text = page.get_text("text").strip()
            
            page_parts = [f"\n--- PAGE {page_num + 1} ---"]
            if text:
                page_parts.append(text)
                
            sections.append("\n".join(page_parts))
            
    result = "\n".join(sections)
    elapsed = (time.perf_counter() - t0) * 1000
    log.info(
        f"parse_pdf DONE | pages={page_count} "
        f"chars={len(result):,} elapsed={elapsed:.0f}ms",
        ctx="PARSER"
    )
    return result


# ---------------------------------------------------------------------------
# HTML Parser
# ---------------------------------------------------------------------------
def parse_html(file_content: bytes) -> str:
    """Extract and clean text from HTML file contents."""
    soup = BeautifulSoup(file_content, "html.parser")

    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.extract()

    text = soup.get_text(separator="\n")
    lines = (line.strip() for line in text.splitlines())
    chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
    return "\n".join(chunk for chunk in chunks if chunk)


# ---------------------------------------------------------------------------
# DOCX Parser
# ---------------------------------------------------------------------------
def parse_docx(file_content: bytes) -> str:
    """Extract text from DOCX including tables."""
    doc = docx.Document(io.BytesIO(file_content))
    parts = []

    for para in doc.paragraphs:
        if para.text.strip():
            parts.append(para.text)

    for table in doc.tables:
        parts.append("[TABLE]")
        for row in table.rows:
            cleaned = [cell.text.replace("\n", " ").strip() for cell in row.cells]
            parts.append(" | ".join(cleaned))
        parts.append("[/TABLE]")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Smart Boundary-Aware Chunker
# ---------------------------------------------------------------------------
def smart_chunk(text: str, target_chars: int = 25000, max_chunks: int = 4) -> list:
    """
    Boundary-aware chunker: respects section headers, page breaks, and tables.
    """
    log.info(
        f"smart_chunk START | text_len={len(text):,} target={target_chars:,} max={max_chunks}",
        ctx="CHUNK"
    )
    t0 = time.perf_counter()

    if len(text) <= target_chars:
        log.info("Text fits in single chunk — no splitting needed", ctx="CHUNK")
        return [text]

    lines = text.split("\n")
    chunks = []
    current_lines = []
    current_size = 0
    inside_table = False

    for line in lines:
        if line.startswith("[TABLE"):
            inside_table = True
        if line.startswith("[/TABLE"):
            inside_table = False
            current_lines.append(line)
            current_size += len(line)
            continue

        is_page_break = line.startswith("--- PAGE ")
        is_section_header = bool(_SECTION_PATTERNS.match(line.strip()))
        at_boundary = (is_page_break or is_section_header) and not inside_table

        if at_boundary and current_size >= target_chars * 0.6 and current_lines:
            chunks.append("\n".join(current_lines))
            current_lines = [line]
            current_size = len(line)
            continue

        if current_size >= target_chars and not inside_table and current_lines:
            chunks.append("\n".join(current_lines))
            current_lines = [line]
            current_size = len(line)
            continue

        current_lines.append(line)
        current_size += len(line) + 1

    if current_lines:
        chunks.append("\n".join(current_lines))

    # Hard enforcement: If any chunk is still somehow larger than target_chars * 1.5,
    # forcefully split it to guarantee we don't exceed API limits.
    final_chunks = []
    hard_limit = int(target_chars * 1.5)
    for c in chunks:
        if len(c) > hard_limit:
            # Force slice into smaller pieces
            for i in range(0, len(c), target_chars):
                final_chunks.append(c[i:i+target_chars])
        else:
            final_chunks.append(c)
    
    chunks = final_chunks

    # Removed max_chunks forced merging because it creates chunks 
    # that exceed the hard 6000 TPM limit of Groq's Free Tier.

    elapsed = (time.perf_counter() - t0) * 1000
    sizes = [len(c) for c in chunks]
    log.info(
        f"smart_chunk DONE | {len(chunks)} chunks | sizes={sizes} | elapsed={elapsed:.0f}ms",
        ctx="CHUNK"
    )
    return chunks


# ---------------------------------------------------------------------------
# Main parse_file dispatcher
# ---------------------------------------------------------------------------
def parse_file(file_name: str, file_content: bytes) -> str:
    """
    Determine the file type by extension and parse accordingly.

    Args:
        file_name:    The name of the file (to extract extension).
        file_content: The raw bytes of the file.

    Returns:
        Extracted text string.
    """
    ext = os.path.splitext(file_name)[1].lower()

    if ext in [".htm", ".html"]:
        return parse_html(file_content)
    elif ext == ".pdf":
        return parse_pdf(file_content)
    elif ext == ".docx":
        return parse_docx(file_content)
    else:
        try:
            return file_content.decode("utf-8")
        except UnicodeDecodeError:
            return f"Error: Unsupported or unreadable file type ({ext})"
