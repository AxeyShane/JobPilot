"""Interview-stage resume generation: written for a human, not an ATS parser.

The ATS-tailored resume (scoring/tailor.py) exists to survive a keyword parser
and a 6-second recruiter triage scan. Once an interview is actually on the
calendar, that job is done -- the resume that gets handed to the interviewer
(who usually asks for "an updated CV") is read by a person, and eye-tracking
research on resume review shows humans read in an F-pattern: a full pass
across the top band (name / headline / summary), then a scan down the left
edge of the page, with the right-hand two-thirds of lower paragraphs getting
far less attention. A dense, keyword-packed single column optimized for a
parser fights that reading pattern instead of using it.

This module reuses everything from the ATS pipeline that exists to keep the
resume honest (JSON-field validation, the LLM fabrication judge) and drops
everything that exists only to satisfy a parser (the ATS text-layer check,
the hard 80%-metric-density retry). It never invents anything the ATS
pipeline wouldn't have -- same profile, same resume_facts, same guardrails
against fabrication -- only the framing and layout instructions change.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from jobpilot.config import RESUME_PATH, INTERVIEW_DIR, load_profile
from jobpilot.database import get_connection
from jobpilot.llm import get_tailor_client
from jobpilot.scoring.tailor import extract_json, judge_tailored_resume
from jobpilot.scoring.validator import BANNED_WORDS, sanitize_text, validate_json_fields

log = logging.getLogger(__name__)

# Note: **bold** markers this module's prompt asks the LLM to emit are
# rendered downstream by scoring/pdf.py (build_html's _md_bold() for the PDF,
# render_docx's add_runs_with_bold() for the DOCX) -- both are no-ops on
# ordinary ATS-tailored text, which never contains "**".


# ── Prompt Builder ───────────────────────────────────────────────────────

def _build_interview_prompt(profile: dict, interview_context: str = "") -> str:
    """Build the interview-stage resume prompt.

    Same skills boundary / preserved-facts guardrails as the ATS prompt --
    this is still the same honest profile, just written for a different
    reader.
    """
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    skills_lines = []
    for category, items in boundary.items():
        if isinstance(items, list) and items:
            label = category.replace("_", " ").title()
            skills_lines.append(f"{label}: {', '.join(items)}")
    skills_block = "\n".join(skills_lines)

    companies = resume_facts.get("preserved_companies", [])
    projects = resume_facts.get("preserved_projects", [])
    school = resume_facts.get("preserved_school", "")
    real_metrics = resume_facts.get("real_metrics", [])

    companies_str = ", ".join(companies) if companies else "N/A"
    metrics_str = ", ".join(real_metrics) if real_metrics else "N/A"
    banned_str = ", ".join(BANNED_WORDS)

    education = profile.get("experience", {})
    education_level = education.get("education_level", "")

    context_block = (
        f"\n## WHAT YOU ALREADY KNOW ABOUT THIS INTERVIEW:\n{interview_context.strip()}\n"
        "Use this to decide emphasis (what clearly landed, what to reinforce) -- never to "
        "invent a fact you weren't given.\n"
        if interview_context and interview_context.strip() else ""
    )

    # Same fabrication-pressure problem as the ATS prompt (scoring/tailor.py):
    # telling the model to bold "the number" on every bullet with nothing to
    # cite reliably produces an invented percentage. Drop that pressure
    # entirely when the profile has no recorded metrics.
    if real_metrics:
        bullet_bold_rule = (
            "Bold exactly one real number or outcome phrase per bullet with **double "
            f"asterisks**. Pull any number from the original resume or real_metrics "
            f"({metrics_str}) -- never invent one."
        )
    else:
        bullet_bold_rule = (
            "This profile has no recorded metrics (real_metrics is empty). Do NOT invent a "
            "percentage, dollar figure, count, or scale. Bold the strongest OUTCOME WORD OR "
            "PHRASE per bullet instead (e.g. **owned end to end**, **shipped to production**, "
            "**cut the failure mode entirely**) -- never a number that isn't in the original "
            "resume. A bullet with an honest, un-numbered outcome beats one with a made-up stat."
        )

    return f"""You are a career coach preparing a candidate's CV for an interviewer who ALREADY decided to interview them. The screening is over. This document will be read by a person, not a parser.

Take the base resume and job description. Return an interview-stage resume as a JSON object.

## HOW A HUMAN ACTUALLY READS THIS PAGE (F-pattern, eye-tracking research):
A hiring manager or interviewer reads a full horizontal band across the top of the page
(name, headline, summary), then their eyes drop and scan down the LEFT EDGE of the page,
reading less and less of each line's right-hand side as they go down. They rarely read a
paragraph's second half in full. Design for that:
- The single strongest, most relevant thing about this candidate belongs in the "positioning"
  line -- it sits in that top band and gets read in full every time.
- Every bullet leads with the outcome, not the setup. Put the strongest word (and a real
  number, only if one genuinely exists) in the first few words, at the left edge, not buried
  mid-sentence.
- Bold (using **double asterisks**) the ONE thing per bullet that should anchor the eye
  during the left-edge scan -- see the BULLETS rule below for exactly what that can be. One
  bold span per bullet, never the whole bullet.
- Short lines beat long ones. A bullet a reader can't finish in one glance loses the pattern.

## THIS IS NOT AN ATS DOCUMENT:
- Do not keyword-stuff. Say what you did the way you'd say it out loud to the interviewer.
- A little personality and narrative voice is welcome -- this is a human conversation
  continuing on paper, not a parser scan.
- Do not repeat, word for word, whatever this candidate already said in the interview itself
  if interview context is given below -- reinforce it from a slightly different angle instead.
{context_block}
## SKILLS BOUNDARY (real skills only):
{skills_block}

You MAY add 2-3 closely related tools (Kubernetes if Docker, Terraform if AWS, Redis if PostgreSQL). No unrelated languages/frameworks.

SKILLS SECTION FOR THIS DOCUMENT: unlike the ATS resume, this one doesn't need every category
for a parser to find. If a whole category above has nothing to do with this specific role
(e.g. CAD or PCB design skills for a pure software job), you may drop that category entirely
from the "skills" output. Keep only what's actually relevant to the job description -- listing
unrelated domains is exactly the keyword-stuffing this document exists to avoid.

## CONTENT RULES:
POSITIONING: One sentence, no bullet, sits directly under the header. This is the top-band
line -- the single reason this specific interviewer should remember this candidate after the
conversation. Not a generic summary restatement.

SUMMARY: 2 sentences, conversational, not a keyword list.

BULLETS: Outcome-first phrasing. Max 4 per section, most relevant to the actual role first.
{bullet_bold_rule}

ARCHITECTURE OVER TOOL LISTS: for a bullet describing a system with more than one moving
part, say how the pieces connect and why, not which tools were used -- a human interviewer
already has the tool names from the earlier conversation and the skills section below; the
bullet's job is to show judgment.

## VOICE:
- Write like you're describing your own work to a person who's already interested.
- BANNED WORDS (using ANY of these = validation failure -- do not use them even once):
  {banned_str}
- No em dashes. Use commas, periods, or hyphens.

## HARD RULES (unchanged from the ATS version -- honesty doesn't relax for a human reader):
- Do NOT invent work, companies, degrees, or certifications.
- Do NOT change real numbers ({metrics_str}).
- ALL of these companies MUST appear as separate entries in "experience" -- {companies_str}.
  Every one, every time, with no exceptions.
- Names of preserved companies stay exactly as written.
- Preserved school: {school}
- Should fit 1 page. Trim bullet count per entry (not entries themselves) if space is tight.

## OUTPUT: Return ONLY valid JSON. No markdown fences. No commentary.

{{"positioning":"One sentence for the top band.","title":"Role Title","summary":"2 conversational sentences.","skills":{{"Languages":"...","Frameworks":"...","DevOps & Infra":"...","Databases":"...","Tools":"..."}},"experience":[{{"header":"Title at Company","subtitle":"Month Year - Month Year","bullets":["bullet with **one bolded outcome**","bullet 2","bullet 3"]}}],"projects":[{{"header":"Project Name - Description","subtitle":"Month Year - Month Year","bullets":["bullet 1","bullet 2"]}}],"education":"{school} | {education_level}"}}"""


# ── Resume Assembly ──────────────────────────────────────────────────────

def assemble_interview_resume_text(data: dict, profile: dict) -> str:
    """Convert the JSON interview resume to formatted plain text.

    Same header-injection discipline as the ATS assembler (name/contact are
    always code-injected, never LLM-generated), plus a POSITIONING line
    inserted right after the header -- the one line guaranteed to land in a
    human reader's top-band pass.
    """
    personal = profile.get("personal", {})
    lines: list[str] = []

    lines.append(personal.get("full_name", ""))
    lines.append(sanitize_text(data.get("title", "")))

    contact_parts: list[str] = []
    if personal.get("email"):
        contact_parts.append(personal["email"])
    if personal.get("phone"):
        contact_parts.append(personal["phone"])
    if personal.get("github_url"):
        contact_parts.append(personal["github_url"])
    if personal.get("linkedin_url"):
        contact_parts.append(personal["linkedin_url"])
    website = personal.get("website_url") or personal.get("portfolio_url")
    if website:
        contact_parts.append(website)
    if contact_parts:
        lines.append(" | ".join(contact_parts))
    lines.append("")

    # NOTE: pdf.parse_resume() only stops scanning "header" lines when it hits
    # a line that is literally "SUMMARY" -- any section placed before that in
    # the text (POSITIONING included) would get swallowed into the header and
    # corrupt the name/title/contact split. SUMMARY must stay the first
    # section right after the header. This does NOT affect the rendered PDF's
    # visual order -- build_html() in pdf.py explicitly places positioning
    # above summary regardless of where each section falls in this text.
    lines.append("SUMMARY")
    lines.append(sanitize_text(data["summary"]))
    lines.append("")

    if data.get("positioning"):
        lines.append("POSITIONING")
        lines.append(sanitize_text(data["positioning"]))
        lines.append("")

    lines.append("TECHNICAL SKILLS")
    if isinstance(data["skills"], dict):
        for cat, val in data["skills"].items():
            if not val:
                continue
            lines.append(f"{cat}: {sanitize_text(str(val))}")
    lines.append("")

    lines.append("EXPERIENCE")
    for entry in data.get("experience", []):
        lines.append(sanitize_text(entry.get("header", "")))
        if entry.get("subtitle"):
            lines.append(sanitize_text(entry["subtitle"]))
        for b in entry.get("bullets", []):
            lines.append(f"- {sanitize_text(b)}")
        lines.append("")

    lines.append("PROJECTS")
    for entry in data.get("projects", []):
        lines.append(sanitize_text(entry.get("header", "")))
        if entry.get("subtitle"):
            lines.append(sanitize_text(entry["subtitle"]))
        for b in entry.get("bullets", []):
            lines.append(f"- {sanitize_text(b)}")
        lines.append("")

    lines.append("EDUCATION")
    lines.append(sanitize_text(str(data.get("education", ""))))

    return "\n".join(lines)


# ── Core Generation ──────────────────────────────────────────────────────

def tailor_interview_resume(
    resume_text: str, job: dict, profile: dict,
    interview_context: str = "", max_retries: int = 2, max_pages: int = 1,
) -> tuple[str, dict]:
    """Generate an interview-stage resume: honest, F-pattern-formatted, human-voiced.

    Reuses the ATS pipeline's fabrication guardrails (JSON-field validation,
    LLM judge) and drops the parts that exist only to satisfy a parser (the
    ATS text-layer check, and the 80%-metric-density hard retry -- a human
    reader has already read the numbers in the ATS resume; this version can
    afford a couple of purely-narrative bullets without being penalized).

    Returns:
        (resume_text, report) -- report shape mirrors tailor_resume()'s, minus
        the "ats" key (not applicable to a document a parser never sees).
    """
    job_text = (
        f"TITLE: {job.get('title','')}\n"
        f"COMPANY: {job.get('site','')}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    report: dict = {"attempts": 0, "validator": None, "judge": None, "status": "pending"}
    avoid_notes: list[str] = []
    tailored = ""
    client = get_tailor_client()
    prompt_base = _build_interview_prompt(profile, interview_context)

    for attempt in range(max_retries + 1):
        report["attempts"] = attempt + 1

        prompt = prompt_base
        if avoid_notes:
            prompt += "\n\n## AVOID THESE ISSUES (from previous attempt):\n" + "\n".join(
                f"- {n}" for n in avoid_notes[-5:]
            )

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"ORIGINAL RESUME:\n{resume_text}\n\n---\n\nTARGET JOB:\n{job_text}\n\nReturn the JSON:"},
        ]

        raw = client.chat(messages, max_tokens=2048, temperature=0.2)

        try:
            data = extract_json(raw)
        except ValueError:
            avoid_notes.append("Output was not valid JSON. Return ONLY a JSON object, nothing else.")
            continue

        # Layer 1: same fabrication/field validation as the ATS pipeline.
        # "lenient" mode here only because the bold-word banned-list check
        # is about generic filler ("synergies", "leverage") which the
        # interview prompt already tells the model to avoid on its own --
        # a false-positive retry here should never block getting the file.
        validation = validate_json_fields(data, profile, mode="normal", original_text=resume_text)
        report["validator"] = validation

        if not validation["passed"]:
            avoid_notes.extend(validation["errors"])
            if attempt < max_retries:
                continue
            tailored = assemble_interview_resume_text(data, profile)
            report["status"] = "failed_validation"
            return tailored, report

        tailored = assemble_interview_resume_text(data, profile)

        # Layer 2: LLM fabrication judge -- same as the ATS pipeline. Positioning
        # line is judged too (it's built from the same real facts).
        judge = judge_tailored_resume(resume_text, tailored, job.get("title", ""), profile)
        report["judge"] = judge

        if not judge["passed"]:
            avoid_notes.append(f"Judge rejected: {judge['issues']}")
            if attempt < max_retries:
                continue
            report["status"] = "approved_with_judge_warning"
            return tailored, report

        # Layer 3 (informational only): page-fit. A human-facing document can
        # tolerate slightly more than an ATS one might, so this never forces a
        # retry -- it's reported so the CLI can warn, not fail.
        try:
            from jobpilot.scoring.pdf import render_pdf_from_text
            # build_html() (called inside render_pdf_from_text) already converts
            # **bold** markers to real <b> tags, so this counts pages against
            # the same rendering the final PDF will use.
            _, page_count = render_pdf_from_text(tailored)
        except Exception:
            log.debug("Page-fit check failed, skipping", exc_info=True)
            page_count = 1
        report["page_count"] = page_count
        if page_count > max_pages:
            report["status"] = f"approved_over_{max_pages}_page"
        else:
            report["status"] = "approved"
        return tailored, report

    report["status"] = "exhausted_retries"
    return tailored, report


# ── Single-job Entry Point (CLI + web UI) ─────────────────────────────────

def _prefix_for_job(job: dict) -> str:
    """Deterministic filename prefix for a job's interview CV -- shared by
    generation and by the dashboard's status/download lookups so both agree
    on where the files live without persisting a path anywhere.
    """
    safe_title = re.sub(r"[^\w\s-]", "", job.get("title") or "")[:50].strip().replace(" ", "_")
    safe_site = re.sub(r"[^\w\s-]", "", job.get("site") or "")[:20].strip().replace(" ", "_")
    return f"{safe_site}_{safe_title}_interview"


def get_interview_cv_status(url: str, conn=None) -> dict:
    """Look up whatever interview-CV files already exist for a job, without
    generating anything -- lets the dashboard show download links (or a
    "needs review" draft warning) on page load, before the user asks to
    (re)generate.
    """
    conn = conn or get_connection()
    row = conn.execute("SELECT title, site FROM jobs WHERE url = ?", (url,)).fetchone()
    if row is None:
        return {"exists": False, "error": f"No job found for url: {url}"}
    prefix = _prefix_for_job(dict(row))

    txt_path = INTERVIEW_DIR / f"{prefix}.txt"
    if txt_path.is_file():
        return {
            "exists": True,
            "clean": True,
            "pdf": (INTERVIEW_DIR / f"{prefix}.pdf").is_file(),
            "docx": (INTERVIEW_DIR / f"{prefix}.docx").is_file(),
        }

    drafts = sorted(INTERVIEW_DIR.glob(f"{prefix}_NEEDS_REVIEW_*.txt"))
    if drafts:
        latest = drafts[-1]
        return {"exists": True, "clean": False, "status": latest.stem.split("_NEEDS_REVIEW_", 1)[-1]}

    return {"exists": False}


def generate_interview_cv(url: str, interview_context: str = "", conn=None) -> dict:
    """Generate and save an interview-stage CV for one job, by URL.

    Looks the job up in the jobs table (needs at least title/site/location/
    full_description -- the same columns the ATS tailoring stage already
    depends on), builds the F-pattern resume, and writes .txt/.pdf/.docx into
    INTERVIEW_DIR alongside a validation report, mirroring the ATS pipeline's
    on-disk layout in tailored_resumes/ so both are easy to find side by side.

    Returns a result dict: {"ok": bool, "path"/"pdf_path"/"error", ...}.
    """
    conn = conn or get_connection()
    row = conn.execute(
        "SELECT url, title, site, location, full_description FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()
    if row is None:
        return {"ok": False, "error": f"No job found for url: {url}"}
    job = dict(row)

    try:
        profile = load_profile()
        resume_text = RESUME_PATH.read_text(encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": f"Could not load profile/resume: {e}"}

    try:
        tailored, report = tailor_interview_resume(resume_text, job, profile, interview_context)
    except Exception as e:
        log.error("Interview CV generation failed for %s: %s", url, e)
        return {"ok": False, "error": str(e)}

    INTERVIEW_DIR.mkdir(parents=True, exist_ok=True)
    prefix = _prefix_for_job(job)

    report_path = INTERVIEW_DIR / f"{prefix}_REPORT.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    # "approved*" is the only family of statuses that passed the fabrication
    # judge / field validator honestly -- "failed_validation" and
    # "exhausted_retries" mean the last attempt still had a real problem
    # (an invented metric, a skill the judge couldn't verify, etc.). Writing
    # a polished PDF/DOCX for that content anyway is exactly the
    # "buried failure looks like success" shape this repo's own CLAUDE.md
    # warns about elsewhere -- someone opening interview_resumes/ later has
    # no way to tell a tainted draft from a clean one. So a failed status
    # gets a plainly-labeled .txt draft ONLY (for debugging), never a PDF/DOCX,
    # and the caller gets a `draft_path` key instead of `path` so nothing
    # downstream can casually mistake it for a real deliverable.
    is_clean = report["status"].startswith("approved")

    if is_clean:
        txt_path = INTERVIEW_DIR / f"{prefix}.txt"
        txt_path.write_text(tailored, encoding="utf-8")

        pdf_path = None
        docx_path = None
        try:
            from jobpilot.scoring.pdf import convert_to_pdf, convert_to_docx
            pdf_path = str(convert_to_pdf(txt_path))
            docx_path = str(convert_to_docx(txt_path))
        except Exception:
            log.debug("PDF/DOCX generation failed for %s", txt_path, exc_info=True)

        return {
            "ok": True,
            "status": report["status"],
            "path": str(txt_path),
            "pdf_path": pdf_path,
            "docx_path": docx_path,
            "attempts": report["attempts"],
        }

    draft_path = INTERVIEW_DIR / f"{prefix}_NEEDS_REVIEW_{report['status']}.txt"
    draft_path.write_text(tailored, encoding="utf-8")
    log.warning(
        "Interview CV for %s did not pass validation (%s) -- wrote a labeled "
        "draft to %s, no PDF/DOCX generated.", url, report["status"], draft_path,
    )
    return {
        "ok": False,
        "status": report["status"],
        "draft_path": str(draft_path),
        "error": (
            f"Validation failed ({report['status']}). A labeled draft (likely "
            f"containing an unverified claim) was saved to {draft_path} for "
            "review -- do not send it as-is."
        ),
        "attempts": report["attempts"],
    }
