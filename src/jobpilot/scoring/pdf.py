"""Text-to-PDF conversion for tailored resumes and cover letters.

Parses the structured text resume format, renders via an HTML/CSS template,
and exports to PDF using headless Chromium via Playwright.
"""

import logging
from pathlib import Path

from jobpilot.config import TAILORED_DIR

log = logging.getLogger(__name__)

import re as _re

_BOLD_RE = _re.compile(r"\*\*(.+?)\*\*")


def _md_bold(text: str) -> str:
    """Convert **bold** markdown spans to <b>...</b>.

    Only the interview-stage resume (scoring/interview_resume.py) ever emits
    "**" -- the ATS prompt is instructed never to use markdown -- so this is a
    no-op on ordinary ATS-tailored resumes and safe to apply unconditionally
    everywhere resume text is dropped into the HTML template.
    """
    return _BOLD_RE.sub(r"<b>\1</b>", text or "")


# ── Resume Parser ────────────────────────────────────────────────────────

def parse_resume(text: str) -> dict:
    """Parse a structured text resume into sections.

    Expects a format with header lines (name, title, location, contact)
    followed by ALL-CAPS section headers (SUMMARY, TECHNICAL SKILLS, etc.).

    Args:
        text: Full resume text.

    Returns:
        {"name": str, "title": str, "location": str, "contact": str, "sections": dict}
    """
    lines = [line.rstrip() for line in text.strip().split("\n")]

    # Header: first few lines before SUMMARY
    header_lines: list[str] = []
    body_start = 0
    for i, line in enumerate(lines):
        if line.strip().upper() == "SUMMARY":
            body_start = i
            break
        if line.strip():
            header_lines.append(line.strip())

    name = header_lines[0] if len(header_lines) > 0 else ""
    title = header_lines[1] if len(header_lines) > 1 else ""
    # The header may have 3 or 4 lines depending on whether location is included
    location = ""
    contact = ""
    if len(header_lines) > 3:
        location = header_lines[2]
        contact = header_lines[3]
    elif len(header_lines) > 2:
        # Could be location or contact -- check for email/phone indicators
        if "@" in header_lines[2] or "|" in header_lines[2]:
            contact = header_lines[2]
        else:
            location = header_lines[2]

    # Split body into sections by ALL-CAPS headers
    sections: dict[str, str] = {}
    current_section: str | None = None
    current_lines: list[str] = []

    for line in lines[body_start:]:
        stripped = line.strip()
        # Detect section headers (all caps, no leading dash/bullet, longer than 3 chars)
        if (
            stripped
            and stripped == stripped.upper()
            and not stripped.startswith("-")
            and len(stripped) > 3
            and not stripped.startswith("\u2022")
        ):
            if current_section:
                sections[current_section] = "\n".join(current_lines).strip()
            current_section = stripped
            current_lines = []
        else:
            current_lines.append(line)

    if current_section:
        sections[current_section] = "\n".join(current_lines).strip()

    return {
        "name": name,
        "title": title,
        "location": location,
        "contact": contact,
        "sections": sections,
    }


def parse_skills(text: str) -> list[tuple[str, str]]:
    """Parse skills section into (category, value) pairs.

    Args:
        text: The TECHNICAL SKILLS section text.

    Returns:
        List of (category_name, skills_string) tuples.
    """
    skills: list[tuple[str, str]] = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if ":" in line:
            cat, val = line.split(":", 1)
            skills.append((cat.strip(), val.strip()))
    return skills


def parse_entries(text: str) -> list[dict]:
    """Parse experience/project entries from section text.

    Args:
        text: The EXPERIENCE or PROJECTS section text.

    Returns:
        List of {"title": str, "subtitle": str, "bullets": list[str]} dicts.
    """
    entries: list[dict] = []
    lines = text.strip().split("\n")
    current: dict | None = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- ") or stripped.startswith("\u2022 "):
            if current:
                current["bullets"].append(stripped[2:].strip())
        elif current is None or (
            not stripped.startswith("-")
            and not stripped.startswith("\u2022")
            and len(current.get("bullets", [])) > 0
        ):
            # New entry
            if current:
                entries.append(current)
            current = {"title": stripped, "subtitle": "", "bullets": []}
        elif current and not current["subtitle"]:
            current["subtitle"] = stripped
        else:
            if current:
                current["bullets"].append(stripped)

    if current:
        entries.append(current)

    return entries


# ── HTML Template ────────────────────────────────────────────────────────

def build_html(resume: dict) -> str:
    """Build professional resume HTML from parsed data.

    Args:
        resume: Parsed resume dict from parse_resume().

    Returns:
        Complete HTML string ready for PDF rendering.
    """
    sections = resume["sections"]

    # Skills
    skills_html = ""
    if "TECHNICAL SKILLS" in sections:
        skills = parse_skills(sections["TECHNICAL SKILLS"])
        rows = ""
        for cat, val in skills:
            rows += f'<div class="skill-row"><span class="skill-cat">{cat}:</span> {val}</div>\n'
        skills_html = f'<div class="section"><div class="section-title">Technical Skills</div>{rows}</div>'

    # Experience
    exp_html = ""
    if "EXPERIENCE" in sections:
        entries = parse_entries(sections["EXPERIENCE"])
        items = ""
        for e in entries:
            bullets = "".join(f"<li>{_md_bold(b)}</li>" for b in e["bullets"])
            subtitle = f'<div class="entry-subtitle">{e["subtitle"]}</div>' if e["subtitle"] else ""
            items += f'<div class="entry"><div class="entry-title">{e["title"]}</div>{subtitle}<ul>{bullets}</ul></div>'
        exp_html = f'<div class="section"><div class="section-title">Experience</div>{items}</div>'

    # Projects
    proj_html = ""
    if "PROJECTS" in sections:
        entries = parse_entries(sections["PROJECTS"])
        items = ""
        for e in entries:
            bullets = "".join(f"<li>{_md_bold(b)}</li>" for b in e["bullets"])
            subtitle = f'<div class="entry-subtitle">{e["subtitle"]}</div>' if e["subtitle"] else ""
            items += f'<div class="entry"><div class="entry-title">{e["title"]}</div>{subtitle}<ul>{bullets}</ul></div>'
        proj_html = f'<div class="section"><div class="section-title">Projects</div>{items}</div>'

    # Education
    edu_html = ""
    if "EDUCATION" in sections:
        edu_text = sections["EDUCATION"].strip()
        edu_html = f'<div class="section"><div class="section-title">Education</div><div class="edu">{edu_text}</div></div>'

    # Summary
    summary_html = ""
    if "SUMMARY" in sections:
        summary_html = f'<div class="section"><div class="section-title">Summary</div><div class="summary">{_md_bold(sections["SUMMARY"].strip())}</div></div>'

    # Positioning -- interview-stage resumes only (scoring/interview_resume.py).
    # This is the top-band line an F-pattern reader is guaranteed to read in
    # full, so it renders above the summary, visually distinct (larger,
    # accent color) rather than as just another section.
    positioning_html = ""
    if "POSITIONING" in sections:
        positioning_html = (
            f'<div class="positioning">{_md_bold(sections["POSITIONING"].strip())}</div>'
        )

    # Contact line parsing
    contact = resume["contact"]
    contact_parts = [p.strip() for p in contact.split("|")] if contact else []
    contact_html = " &nbsp;|&nbsp; ".join(contact_parts)

    # Location line (may be empty)
    location_html = f'<div class="location">{resume["location"]}</div>' if resume["location"] else ""

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
@page {{
    size: letter;
    margin: 0.35in 0.5in;
}}
* {{
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}}
body {{
    font-family: 'Calibri', 'Segoe UI', Arial, sans-serif;
    font-size: 10pt;
    line-height: 1.35;
    color: #1a1a1a;
}}
.header {{
    text-align: center;
    margin-bottom: 4px;
    padding-bottom: 4px;
    border-bottom: 1.5px solid #2a7ab5;
}}
.name {{
    font-size: 18pt;
    font-weight: 700;
    color: #1a3a5c;
    letter-spacing: 0.5px;
}}
.title {{
    font-size: 10.5pt;
    color: #3a6b8c;
    margin: 1px 0;
}}
.location {{
    font-size: 9pt;
    color: #555;
}}
.contact {{
    font-size: 9pt;
    color: #444;
    margin-top: 1px;
}}
.contact a {{
    color: #2c3e50;
    text-decoration: none;
}}
.section {{
    margin-top: 5px;
}}
.section-title {{
    font-size: 10pt;
    font-weight: 700;
    color: #1a3a5c;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    border-bottom: 1.5px solid #2a7ab5;
    padding-bottom: 1px;
    margin-bottom: 3px;
}}
.summary {{
    font-size: 9.5pt;
    color: #333;
    line-height: 1.4;
}}
.skill-row {{
    font-size: 9.5pt;
    margin: 0;
    line-height: 1.35;
}}
.skill-cat {{
    font-weight: 600;
    color: #1a3a5c;
}}
.entry {{
    margin-bottom: 4px;
    break-inside: avoid;
}}
.entry-title {{
    font-weight: 600;
    font-size: 10pt;
    color: #1a3a5c;
}}
.entry-subtitle {{
    font-size: 9pt;
    color: #4a7a9b;
    font-style: italic;
    margin-bottom: 1px;
}}
ul {{
    margin-left: 14px;
    padding: 0;
}}
li {{
    font-size: 9.5pt;
    margin-bottom: 1px;
    line-height: 1.35;
}}
.edu {{
    font-size: 10pt;
}}
.positioning {{
    font-size: 10.5pt;
    font-weight: 600;
    font-style: italic;
    color: #1a3a5c;
    text-align: center;
    margin: 4px 0 2px 0;
}}
b {{
    color: #16324f;
}}
</style>
</head>
<body>
<div class="header">
    <div class="name">{resume['name']}</div>
    <div class="title">{resume['title']}</div>
    {location_html}
    <div class="contact">{contact_html}</div>
</div>
{positioning_html}
{summary_html}
{skills_html}
{exp_html}
{proj_html}
{edu_html}
</body>
</html>"""


# ── DOCX Renderer ────────────────────────────────────────────────────────
# Real ATS parsers (Workday, Taleo, iCIMS, etc.) extract structured text far
# more reliably from DOCX than from PDF -- PDF text layout/columns routinely
# scrambles on extraction, silently corrupting what the ATS sees even when
# the file looks fine to a human. DOCX is what actually gets uploaded during
# apply (see apply/prompt.py); PDF is kept for human preview only.

def render_docx(resume: dict, output_path: str) -> None:
    """Render parsed resume data to a DOCX file.

    Args:
        resume: Parsed resume dict from parse_resume().
        output_path: Path to write the .docx file.
    """
    from docx import Document
    from docx.shared import Pt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    HEADING_COLOR = RGBColor(0x1A, 0x3A, 0x5C)
    SUBTITLE_COLOR = RGBColor(0x4A, 0x7A, 0x9B)

    doc = Document()
    for section in doc.sections:
        section.top_margin = Pt(25)
        section.bottom_margin = Pt(25)
        section.left_margin = Pt(36)
        section.right_margin = Pt(36)
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10)

    def heading(text: str, size: int = 11) -> None:
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(6)
        p.paragraph_format.space_after = Pt(2)
        run = p.add_run(text.upper())
        run.bold = True
        run.font.size = Pt(size)
        run.font.color.rgb = HEADING_COLOR

    def add_runs_with_bold(paragraph, text: str, size: int = 10) -> None:
        """Split text on **bold** markers into separate runs.

        Only interview-stage resumes (scoring/interview_resume.py) ever emit
        "**" -- a no-op split on ordinary ATS-tailored text, which never
        contains it.
        """
        pos = 0
        for m in _BOLD_RE.finditer(text):
            if m.start() > pos:
                paragraph.add_run(text[pos:m.start()]).font.size = Pt(size)
            run = paragraph.add_run(m.group(1))
            run.bold = True
            run.font.size = Pt(size)
            pos = m.end()
        if pos < len(text):
            paragraph.add_run(text[pos:]).font.size = Pt(size)

    def bullet(text: str) -> None:
        p = doc.add_paragraph(style="List Bullet")
        p.paragraph_format.space_after = Pt(0)
        add_runs_with_bold(p, text, size=10)

    # Header
    name_p = doc.add_paragraph()
    name_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    name_run = name_p.add_run(resume["name"])
    name_run.bold = True
    name_run.font.size = Pt(16)
    name_run.font.color.rgb = HEADING_COLOR

    if resume["title"]:
        title_p = doc.add_paragraph()
        title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        title_run = title_p.add_run(resume["title"])
        title_run.font.size = Pt(10.5)
        title_run.font.color.rgb = SUBTITLE_COLOR

    contact_bits = [resume["location"], resume["contact"]]
    contact_line = "  |  ".join(b for b in contact_bits if b)
    if contact_line:
        contact_p = doc.add_paragraph()
        contact_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        contact_p.add_run(contact_line).font.size = Pt(9)

    sections = resume["sections"]

    if "POSITIONING" in sections:
        pos_p = doc.add_paragraph()
        pos_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        pos_run = pos_p.add_run(sections["POSITIONING"].strip().replace("**", ""))
        pos_run.italic = True
        pos_run.bold = True
        pos_run.font.size = Pt(10.5)
        pos_run.font.color.rgb = HEADING_COLOR

    if "SUMMARY" in sections:
        heading("Summary")
        add_runs_with_bold(doc.add_paragraph(), sections["SUMMARY"].strip())

    if "TECHNICAL SKILLS" in sections:
        heading("Technical Skills")
        for cat, val in parse_skills(sections["TECHNICAL SKILLS"]):
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(0)
            cat_run = p.add_run(f"{cat}: ")
            cat_run.bold = True
            p.add_run(val)

    for section_name, heading_text in (("EXPERIENCE", "Experience"), ("PROJECTS", "Projects")):
        if section_name in sections:
            heading(heading_text)
            for e in parse_entries(sections[section_name]):
                p = doc.add_paragraph()
                p.paragraph_format.space_after = Pt(0)
                p.add_run(e["title"]).bold = True
                if e["subtitle"]:
                    sp = doc.add_paragraph()
                    sp.paragraph_format.space_after = Pt(1)
                    sub_run = sp.add_run(e["subtitle"])
                    sub_run.italic = True
                    sub_run.font.size = Pt(9)
                    sub_run.font.color.rgb = SUBTITLE_COLOR
                for b in e["bullets"]:
                    bullet(b)

    if "EDUCATION" in sections:
        heading("Education")
        doc.add_paragraph(sections["EDUCATION"].strip())

    doc.save(output_path)


# ── PDF Renderer ─────────────────────────────────────────────────────────

def render_pdf(html: str, output_path: str) -> None:
    """Render HTML to PDF using Playwright's headless Chromium.

    Args:
        html: Complete HTML string.
        output_path: Path to write the PDF file.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="networkidle")
        page.pdf(
            path=output_path,
            format="Letter",
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            print_background=True,
        )
        browser.close()


def count_pdf_pages(pdf_path: str) -> int:
    """Count pages in a rendered PDF.

    Used by the tailoring retry loop (see scoring/tailor.py) to enforce the
    "must fit 1 page" instruction in code, the same way fabrication and
    banned words are enforced in code rather than trusted to the prompt --
    LLMs routinely ignore length instructions once bullets pile up, so this
    catches a resume that silently spilled to page 2 with nobody the wiser.

    Args:
        pdf_path: Path to a PDF file on disk.

    Returns:
        Number of pages. Returns 1 (fail open) if the PDF can't be parsed,
        since a page-count check should never block an otherwise-good resume
        over a corrupt read.
    """
    try:
        from pypdf import PdfReader
        return len(PdfReader(pdf_path).pages)
    except Exception:
        log.debug("Page count failed for %s, assuming 1 page", pdf_path, exc_info=True)
        return 1


def render_pdf_from_text(text: str) -> tuple[str, int]:
    """Render tailored resume text to a temp PDF and return (html, page_count).

    Thin convenience wrapper combining parse_resume -> build_html -> render_pdf
    -> count_pdf_pages, used by the tailoring retry loop to check page-fit
    without leaving a permanent file behind for every retry attempt.

    Args:
        text: Assembled resume text (see tailor.assemble_resume_text).

    Returns:
        (html, page_count)
    """
    import tempfile
    import os as _os

    resume = parse_resume(text)
    html = build_html(resume)
    fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    _os.close(fd)
    try:
        render_pdf(html, tmp_path)
        pages = count_pdf_pages(tmp_path)
    finally:
        try:
            _os.unlink(tmp_path)
        except OSError:
            pass
    return html, pages


# ── Public API ───────────────────────────────────────────────────────────

def convert_to_pdf(
    text_path: Path, output_path: Path | None = None, html_only: bool = False
) -> Path:
    """Convert a text resume/cover letter to PDF.

    Args:
        text_path: Path to the .txt file to convert.
        output_path: Optional override for the output path. Defaults to same
            name with .pdf extension.
        html_only: If True, output HTML instead of PDF.

    Returns:
        Path to the generated PDF (or HTML) file.
    """
    text_path = Path(text_path)
    text = text_path.read_text(encoding="utf-8")
    resume = parse_resume(text)
    html = build_html(resume)

    if html_only:
        out = output_path or text_path.with_suffix(".html")
        out = Path(out)
        out.write_text(html, encoding="utf-8")
        log.info("HTML generated: %s", out)
        return out

    out = output_path or text_path.with_suffix(".pdf")
    out = Path(out)
    render_pdf(html, str(out))
    log.info("PDF generated: %s", out)
    return out


def convert_cover_letter_to_docx(text_path: Path, output_path: Path | None = None) -> Path:
    """Convert a plain-paragraph cover letter (.txt) to DOCX.

    Cover letters have no section-header structure, so they use a simple
    paragraph-per-blank-line renderer instead of the resume parser.

    Args:
        text_path: Path to the .txt file to convert.
        output_path: Optional override for the output path. Defaults to same
            name with .docx extension.

    Returns:
        Path to the generated DOCX file.
    """
    from docx import Document
    from docx.shared import Pt

    text_path = Path(text_path)
    text = text_path.read_text(encoding="utf-8")

    doc = Document()
    for section in doc.sections:
        section.top_margin = Pt(50)
        section.bottom_margin = Pt(50)
        section.left_margin = Pt(60)
        section.right_margin = Pt(60)
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)

    for para in text.strip().split("\n\n"):
        para = para.strip()
        if para:
            p = doc.add_paragraph(para)
            p.paragraph_format.space_after = Pt(10)

    out = output_path or text_path.with_suffix(".docx")
    out = Path(out)
    doc.save(str(out))
    log.info("DOCX generated: %s", out)
    return out


def convert_to_docx(text_path: Path, output_path: Path | None = None) -> Path:
    """Convert a text resume/cover letter to DOCX -- the format actually
    uploaded during apply (see apply/prompt.py), since ATS parsers handle
    DOCX more reliably than PDF.

    Args:
        text_path: Path to the .txt file to convert.
        output_path: Optional override for the output path. Defaults to same
            name with .docx extension.

    Returns:
        Path to the generated DOCX file.
    """
    text_path = Path(text_path)
    text = text_path.read_text(encoding="utf-8")
    resume = parse_resume(text)

    out = output_path or text_path.with_suffix(".docx")
    out = Path(out)
    render_docx(resume, str(out))
    log.info("DOCX generated: %s", out)
    return out


def batch_convert(limit: int = 50) -> int:
    """Convert .txt files in TAILORED_DIR that don't have corresponding PDFs.

    Scans for .txt files (excluding _JOB.txt and _REPORT.json), checks if a
    .pdf with the same stem already exists, and converts any that are missing.

    Args:
        limit: Maximum number of files to convert.

    Returns:
        Number of PDFs generated.
    """
    if not TAILORED_DIR.exists():
        log.warning("Tailored directory does not exist: %s", TAILORED_DIR)
        return 0

    txt_files = sorted(TAILORED_DIR.glob("*.txt"))
    # Exclude _JOB.txt and _CL.txt files from resume conversion
    # (they get their own conversion calls)
    candidates = [
        f for f in txt_files
        if not f.name.endswith("_JOB.txt")
    ]

    # Filter to those missing a PDF and/or DOCX
    to_convert: list[Path] = []
    for f in candidates:
        if not f.with_suffix(".pdf").exists() or not f.with_suffix(".docx").exists():
            to_convert.append(f)
        if len(to_convert) >= limit:
            break

    if not to_convert:
        log.info("All text files already have PDF/DOCX.")
        return 0

    log.info("Converting %d files to PDF/DOCX...", len(to_convert))
    converted = 0
    for f in to_convert:
        try:
            if not f.with_suffix(".pdf").exists():
                convert_to_pdf(f)
            if not f.with_suffix(".docx").exists():
                convert_to_docx(f)
            converted += 1
        except Exception as e:
            log.error("Failed to convert %s: %s", f.name, e)

    log.info("Done: %d/%d files converted in %s", converted, len(to_convert), TAILORED_DIR)
    return converted
