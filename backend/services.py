"""
services.py — the entire ATS pipeline, in one file.
====================================================

Everything is deterministic: given the same resume PDF and the same job
description you always get the same numbers back. There is no LLM call
anywhere in this file, so nothing can hallucinate a skill or invent a
score, and every result can be traced back to a literal regex hit in the
text the user uploaded.

Pipeline (top to bottom in this file, same order as the flow):

    1. extract_pdf_text(bytes)          PDF  -> raw text
    2. parse_resume(text)               text -> candidate profile
    3. normalize_skill / find_skills    text -> canonical skill names
    4. analyze_job_description(text)    JD   -> requirements
    5. match_skills(...)                profile + JD -> matched / missing
    6. calculate_ats_score(...)         matches -> score + breakdown

Weighting used by calculate_ats_score (see WEIGHTS below):

    required skills 60% | preferred skills 20%
    experience      10% | keywords        10%
"""
from __future__ import annotations

import datetime
import io
import json
import os
import random
import re
from collections import Counter

# ═════════════════════════════════════════════════════════════════════════
# 1. PDF TEXT EXTRACTION
# ═════════════════════════════════════════════════════════════════════════

def _open_pdf(file_bytes: bytes):
    try:
        import pymupdf  # the modern import name; `fitz` is deprecated
    except ImportError:
        try:
            import fitz as pymupdf  # older PyMuPDF releases
        except ImportError as e:  # pragma: no cover - dependency missing
            raise ValueError("PyMuPDF is not installed (pip install pymupdf)") from e
    try:
        return pymupdf.open(stream=io.BytesIO(file_bytes).read(), filetype="pdf")
    except Exception as e:
        raise ValueError(f"That file could not be opened as a PDF: {e}") from e


def extract_pdf_text(file_bytes: bytes, filename: str = "") -> str:
    """Reads a PDF (PyMuPDF) or a plain-text file and returns its plain text.

    Raises ValueError with a human-readable message when the file can't
    be read — main.py turns that straight into a 400 response.
    """
    name = (filename or "").lower()

    if name.endswith((".txt", ".md")):
        return file_bytes.decode("utf-8", errors="ignore")

    doc = _open_pdf(file_bytes)
    pages = [page.get_text("text") for page in doc]
    doc.close()
    text = "\n\n".join(pages).strip()

    if len(text) < 50:
        raise ValueError(
            "Almost no text could be read from this PDF. It is probably a "
            "scanned image rather than a text PDF — please upload a "
            "text-based PDF."
        )
    return text


# PyMuPDF span flag bit 4 (value 16) means the glyphs are bold.
_BOLD_FLAG = 1 << 4


def extract_pdf_markup(file_bytes: bytes, filename: str = "") -> str:
    """Same text, but with the visual structure preserved as light markdown.

    Plain `get_text("text")` discards exactly the signals that tell a human
    which line is the college name and which is the degree. This walks the
    span tree instead and re-emits:

        **bold text**   for bold or semibold spans
        ## Heading      for lines set noticeably larger than body text

    That markup is what gets handed to the LLM extractor, so it can use
    "the bold line under Education is the institution" the way a person
    reading the page would. Falls back to plain text on any failure.
    """
    name = (filename or "").lower()
    if name.endswith((".txt", ".md")):
        return file_bytes.decode("utf-8", errors="ignore")

    try:
        doc = _open_pdf(file_bytes)
    except ValueError:
        raise

    try:
        # First pass: find the body font size (the most common one), so
        # "larger than body" is measured against this document, not a
        # hard-coded number.
        sizes: Counter[float] = Counter()
        pages_raw = []
        for page in doc:
            data = page.get_text("dict")
            pages_raw.append(data)
            for block in data.get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        if span.get("text", "").strip():
                            sizes[round(span.get("size", 0), 1)] += len(span["text"].strip())
        body_size = sizes.most_common(1)[0][0] if sizes else 10.0

        out: list[str] = []
        for data in pages_raw:
            for block in data.get("blocks", []):
                for line in block.get("lines", []):
                    parts: list[str] = []
                    max_size = 0.0
                    all_bold = True
                    has_text = False
                    for span in line.get("spans", []):
                        text = span.get("text", "")
                        if not text.strip():
                            continue
                        has_text = True
                        size = round(span.get("size", body_size), 1)
                        max_size = max(max_size, size)
                        font = (span.get("font") or "").lower()
                        bold = bool(span.get("flags", 0) & _BOLD_FLAG) or \
                            "bold" in font or "black" in font or "semibold" in font
                        all_bold = all_bold and bold
                        parts.append(f"**{text.strip()}**" if bold else text.strip())
                    if not has_text:
                        continue

                    text_line = " ".join(parts)
                    # Collapse "**A** **B**" into "**A B**" so a bold title
                    # split across spans reads as one bold phrase.
                    text_line = re.sub(r"\*\*\s*\*\*", " ", text_line)

                    # A short line set clearly larger than the body, or a
                    # short fully-bold ALL-CAPS line, is a section heading.
                    plain = re.sub(r"\*\*", "", text_line).strip()
                    is_heading = (
                        len(plain) < 60 and (
                            max_size >= body_size * 1.15
                            or (all_bold and plain.isupper())
                        )
                    )
                    out.append(f"## {plain}" if is_heading else text_line)
                out.append("")  # blank line between blocks
        doc.close()
    except Exception:
        # Never let markup extraction break an upload — fall back to plain.
        return extract_pdf_text(file_bytes, filename)

    markup = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    if len(re.sub(r"[#*\s]", "", markup)) < 50:
        raise ValueError(
            "Almost no text could be read from this PDF. It is probably a "
            "scanned image rather than a text PDF — please upload a "
            "text-based PDF."
        )
    return markup


# ═════════════════════════════════════════════════════════════════════════
# 2. SKILL TAXONOMY  (canonical name -> category + the spellings people use)
# ═════════════════════════════════════════════════════════════════════════
# This one table drives BOTH resume skill extraction and JD requirement
# extraction, so the two sides are always compared on equal footing.
# To widen coverage, add a row here — nothing else needs to change.

SKILL_TAXONOMY: dict[str, tuple[str, list[str]]] = {
    # languages
    "Python": ("languages", ["python", "python3"]),
    "Java": ("languages", ["java"]),
    "JavaScript": ("languages", ["javascript", "js", "es6"]),
    "TypeScript": ("languages", ["typescript", "ts"]),
    "C++": ("languages", [r"c\+\+", "cpp"]),
    "C#": ("languages", ["c#", "csharp"]),
    "C": ("languages", [r"\bc\b(?!\s*(?:\+\+|#))"]),
    "Go": ("languages", ["golang", r"(?-i:\bGo\b)"]),
    "Rust": ("languages", ["rust"]),
    "Ruby": ("languages", ["ruby"]),
    "PHP": ("languages", ["php"]),
    "Swift": ("languages", [r"(?-i:\bSwift\b)"]),
    "Kotlin": ("languages", ["kotlin"]),
    "SQL": ("languages", ["sql"]),
    "R": ("languages", [r"(?-i:\bR\b)(?:\s+language|\s+programming)?"]),
    "Scala": ("languages", ["scala"]),
    # frameworks
    "React": ("frameworks", [r"react\.?js", "reactjs", r"\breact\b"]),
    "Angular": ("frameworks", [r"angular\.?js", "angularjs", r"\bangular\b"]),
    "Vue": ("frameworks", [r"vue\.?js", "vuejs", r"\bvue\b"]),
    "Next.js": ("frameworks", [r"next\.?js", "nextjs"]),
    "Django": ("frameworks", ["django"]),
    "Flask": ("frameworks", ["flask"]),
    "FastAPI": ("frameworks", ["fastapi", "fast api"]),
    "Spring": ("frameworks", ["spring boot", "spring framework", r"\bspring\b"]),
    "Express": ("frameworks", [r"express\.?js", "expressjs"]),
    ".NET": ("frameworks", [r"\.net", "dotnet", r"asp\.net"]),
    "Node.js": ("frameworks", [r"node\.?js", "nodejs"]),
    # libraries
    "NumPy": ("libraries", ["numpy"]),
    "Pandas": ("libraries", ["pandas"]),
    "Redux": ("libraries", ["redux"]),
    "OpenCV": ("libraries", ["opencv"]),
    "Matplotlib": ("libraries", ["matplotlib"]),
    # databases
    "PostgreSQL": ("databases", ["postgresql", "postgres"]),
    "MySQL": ("databases", ["mysql"]),
    "MongoDB": ("databases", ["mongodb", "mongo"]),
    "Redis": ("databases", ["redis"]),
    "SQLite": ("databases", ["sqlite"]),
    "Oracle": ("databases", ["oracle db", "oracle database"]),
    "Elasticsearch": ("databases", ["elasticsearch", "elastic search"]),
    "DynamoDB": ("databases", ["dynamodb"]),
    # cloud
    "AWS": ("cloud", ["aws", "amazon web services"]),
    "Azure": ("cloud", ["azure", "microsoft azure"]),
    "GCP": ("cloud", ["gcp", "google cloud"]),
    "Firebase": ("cloud", ["firebase"]),
    "Heroku": ("cloud", ["heroku"]),
    "Vercel": ("cloud", ["vercel"]),
    # devops
    "Docker": ("devops", ["docker"]),
    "Kubernetes": ("devops", ["kubernetes", "k8s"]),
    "Jenkins": ("devops", ["jenkins"]),
    "Terraform": ("devops", ["terraform"]),
    "Ansible": ("devops", ["ansible"]),
    "GitHub Actions": ("devops", ["github actions"]),
    "Nginx": ("devops", ["nginx"]),
    "CI/CD": ("devops", ["ci/cd", "continuous integration", "continuous deployment"]),
    # ai / ml
    "TensorFlow": ("aiml", ["tensorflow"]),
    "PyTorch": ("aiml", ["pytorch"]),
    "scikit-learn": ("aiml", ["scikit-learn", "sklearn"]),
    "Keras": ("aiml", ["keras"]),
    "Machine Learning": ("aiml", ["machine learning", r"\bml\b"]),
    "Deep Learning": ("aiml", ["deep learning"]),
    "NLP": ("aiml", ["nlp", "natural language processing"]),
    "Computer Vision": ("aiml", ["computer vision"]),
    # generative ai
    "OpenAI API": ("genai", ["openai api", "openai"]),
    "LangChain": ("genai", ["langchain"]),
    "Hugging Face": ("genai", ["hugging ?face", "transformers"]),
    "RAG": ("genai", [r"\brag\b", "retrieval augmented generation"]),
    "Prompt Engineering": ("genai", ["prompt engineering"]),
    "Pinecone": ("genai", ["pinecone"]),
    "FAISS": ("genai", ["faiss"]),
    # apis
    "REST API": ("apis", ["rest api", "restful", r"\brest\b"]),
    "GraphQL": ("apis", ["graphql"]),
    "gRPC": ("apis", ["grpc"]),
    "WebSocket": ("apis", ["websocket"]),
    "Microservices": ("apis", ["microservices?", "micro services"]),
    # web
    "HTML": ("web", ["html5?"]),
    "CSS": ("web", ["css3?"]),
    "Tailwind CSS": ("web", ["tailwind ?css", r"\btailwind\b"]),
    "Bootstrap": ("web", ["bootstrap"]),
    # version control
    "Git": ("version_control", [r"\bgit\b"]),
    "GitHub": ("version_control", ["github"]),
    "GitLab": ("version_control", ["gitlab"]),
    # cs fundamentals
    "Data Structures": ("fundamentals", ["data structures?", r"\bdsa\b"]),
    "Algorithms": ("fundamentals", ["algorithms?"]),
    "OOP": ("fundamentals", [r"\boop\b", r"\boops\b", "object[\\s-]oriented"]),
    "Operating Systems": ("fundamentals", ["operating systems?"]),
    "DBMS": ("fundamentals", [r"\bdbms\b"]),
    "System Design": ("fundamentals", ["system design"]),
    # tools
    "Jira": ("tools", ["jira"]),
    "Figma": ("tools", ["figma"]),
    "Postman": ("tools", ["postman"]),
    "Selenium": ("tools", ["selenium"]),
    "Jest": ("tools", ["jest"]),
    "Pytest": ("tools", ["pytest"]),
    "JUnit": ("tools", ["junit"]),
    "Linux": ("tools", ["linux", "unix"]),
    "Agile": ("tools", [r"\bagile\b", "scrum"]),
}

# "If the candidate knows X, that is real evidence they know Y."
# Hand-written on purpose so every credited relationship is reviewable.
SKILL_IMPLIES: dict[str, list[str]] = {
    "MySQL": ["SQL"],
    "PostgreSQL": ["SQL"],
    "SQLite": ["SQL"],
    "Oracle": ["SQL"],
    "Next.js": ["React"],
    "Spring": ["Java"],
    "Django": ["Python"],
    "Flask": ["Python"],
    "FastAPI": ["Python"],
    "Keras": ["TensorFlow"],
    "PyTorch": ["Machine Learning", "Deep Learning"],
    "TensorFlow": ["Machine Learning", "Deep Learning"],
    "scikit-learn": ["Machine Learning"],
    "Kubernetes": ["Docker"],
    "GitHub Actions": ["CI/CD"],
    "Jenkins": ["CI/CD"],
}

# canonical skill -> one regex matching ANY of its spellings.
SKILL_PATTERNS: dict[str, re.Pattern] = {
    canonical: re.compile(
        r"(?<![\w.+#-])(?:" + "|".join(f"(?:{a})" for a in aliases) + r")(?![\w-])",
        re.IGNORECASE,
    )
    for canonical, (_category, aliases) in SKILL_TAXONOMY.items()
}

SKILL_CATEGORY = {name: cat for name, (cat, _a) in SKILL_TAXONOMY.items()}

STOPWORDS = set("""
a an the and or but if then else for while with without within into onto of
to in on at by from as is are was were be been being this that these those
it its you your our their they we i not no will would can could should
may might must have has had do does did job role position company team
responsibilities requirements experience years work working ability strong
excellent good knowledge skills including etc using use used per plus
preferred required must nice about across who what when where why more
than also such well able other others new using build built develop
developed developing looking join help support ensure across level
title position designation description qualifications familiarity exposure
desirable bonus grasp solid hiring candidate ideal opportunity benefits
salary apply please contact send resume email
""".split())

# Phrases that are never technical skills, even when they appear in a
# comma-separated "Skills:" line next to real ones.
SOFT_SKILLS = {
    "problem solving", "team player", "fast learner", "communication",
    "communication skills", "teamwork", "leadership", "adaptability",
    "time management", "critical thinking", "collaboration", "work ethic",
    "self motivated", "detail oriented", "creativity", "public speaking",
    "presentation skills", "decision making", "interpersonal skills",
    "multitasking", "analytical skills", "attention to detail",
    "flexibility", "negotiation", "mentoring", "customer service",
    "project management", "strategic thinking",
}

# A "skill" containing one of these is an organisation, not a technology.
ORG_WORD_RE = re.compile(
    r"\b(college|university|institute|polytechnic|academy|school|"
    r"corp(?:oration)?|company|pvt\.?\s*ltd\.?|inc\.?|llc)\b",
    re.IGNORECASE,
)

# Short all-caps strings that really are technical, so the "all-caps is
# probably a college initialism" rule below doesn't throw them away.
TECH_ACRONYMS = {
    "OOP", "OOPS", "DBMS", "DSA", "OS", "SDK", "GUI", "CLI", "IDE", "ORM",
    "MVC", "TDD", "JSON", "XML", "API", "ETL", "JVM", "JDK", "TCP", "HTTP",
    "HTTPS", "SSH", "SSL", "IOT", "UML", "NoSQL",
}


def normalize_skill(raw: str) -> tuple[str, str]:
    """One raw skill string -> (canonical name, category).

    "Python Programming" -> ("Python", "languages")
    "ML"                 -> ("Machine Learning", "aiml")
    "Snowflake"          -> ("Snowflake", "other")   # unknown but kept
    "Teamwork"           -> ("", "")                 # rejected

    Returning ("", "") means "this is not a technical skill".
    """
    cleaned = re.sub(r"\s+", " ", raw or "").strip(" .,:;-")
    if not cleaned:
        return "", ""

    # Known skill? Only accept it if the raw string is basically just that
    # skill (plus a word like "Programming"/"Developer"), so a whole
    # sentence containing "Python" doesn't collapse into "Python".
    if len(cleaned.split()) <= 4:
        for canonical, pattern in SKILL_PATTERNS.items():
            if pattern.search(cleaned):
                return canonical, SKILL_CATEGORY[canonical]

    # Unknown to the taxonomy — keep the candidate's own wording unless it
    # looks like noise.
    if len(cleaned) > 40 or len(cleaned.split()) > 4:
        return "", ""
    if cleaned.lower() in SOFT_SKILLS or ORG_WORD_RE.search(cleaned):
        return "", ""
    if re.fullmatch(r"[A-Z]{2,6}", cleaned) and cleaned not in TECH_ACRONYMS:
        return "", ""  # GRIET, IIT, NIT ... not a technology
    return cleaned, "other"


def find_skills(text: str) -> list[str]:
    """Every taxonomy skill literally mentioned in `text`, canonicalised."""
    return [name for name, pattern in SKILL_PATTERNS.items() if pattern.search(text or "")]


def categorize_skills(skills: list[str]) -> dict[str, list[str]]:
    """Group a flat skill list by taxonomy category (plain dict lookup)."""
    buckets: dict[str, list[str]] = {}
    for s in skills:
        buckets.setdefault(SKILL_CATEGORY.get(s, "other"), []).append(s)
    return buckets


# ═════════════════════════════════════════════════════════════════════════
# 3. SECTION SEGMENTATION  (shared by resume and JD parsing)
# ═════════════════════════════════════════════════════════════════════════

SECTION_HEADERS = {
    "summary": r"(professional\s+)?summary|objective|profile|about",
    "skills": r"(technical\s+)?skills|technologies|tech\s+stack|core competencies",
    "experience": r"(work\s+|professional\s+)?experience|employment(\s+history)?|internships?",
    "education": r"education|academic(\s+background)?",
    "projects": r"projects|personal projects|academic projects",
    "certifications": r"certificat(?:e|ion)s?|licenses?|courses",
    "achievements": r"achievements|awards|honou?rs",
}


def segment_sections(text: str) -> dict[str, str]:
    """Split free-form text into labelled sections by matching common
    header lines ("SKILLS", "Work Experience:", ...). Purely structural."""
    header_res = {
        key: re.compile(rf"^\s*(?:[A-Z\s]*)?(?:{pat})\s*:?\s*$", re.IGNORECASE)
        for key, pat in SECTION_HEADERS.items()
    }
    sections: dict[str, list[str]] = {k: [] for k in SECTION_HEADERS}
    current = None
    for line in (text or "").splitlines():
        stripped = line.strip()
        hit = None
        if stripped and len(stripped) < 40:
            hit = next((k for k, rx in header_res.items() if rx.match(stripped)), None)
        if hit:
            current = hit
            continue
        if current:
            sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


# ═════════════════════════════════════════════════════════════════════════
# 4. RESUME -> CANDIDATE PROFILE
# ═════════════════════════════════════════════════════════════════════════

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Loose candidate match; the digit count is what actually validates it.
PHONE_RE = re.compile(r"[+(]?\d[\d\s().\-]{7,18}\d")
LINKEDIN_RE = re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/in/[\w-]+", re.IGNORECASE)
GITHUB_RE = re.compile(r"(?:https?://)?(?:www\.)?github\.com/[\w-]+", re.IGNORECASE)

DEGREE_RE = re.compile(
    r"(Bachelor(?:'s)?|Master(?:'s)?|B\.?Tech|M\.?Tech|B\.?E\.?|M\.?E\.?|B\.?Sc\.?|"
    r"M\.?Sc\.?|MBA|Ph\.?D|B\.?A\.?|M\.?A\.?|B\.?C\.?A\.?|M\.?C\.?A\.?|Diploma)"
    r"[^\n,]{0,80}",
    re.IGNORECASE,
)
YEAR_RE = re.compile(r"(?:19|20)\d{2}")
DATE_RANGE_RE = re.compile(
    r"(?P<start>(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s*\d{4}|\d{4})"
    r"\s*(?:-|–|—|to)\s*"
    r"(?P<end>(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s*\d{4}"
    r"|\d{4}|present|current|now)",
    re.IGNORECASE,
)
BULLET_RE = re.compile(r"^\s*[\u2022\u25CF\u25AA\u00B7\-\*\u2013\u2014>]+\s*")
SKILL_SUBHEADER_RE = re.compile(
    r"^\s*(?:programming\s+languages?|languages?|frameworks?(?:\s*(?:/|and|&)\s*libraries)?|"
    r"libraries|databases?|tools?(?:\s*(?:/|and|&)\s*technologies)?|technologies|"
    r"technical\s+skills|platforms?|cloud|devops|methodologies|concepts|other)\s*[:\-]\s*",
    re.IGNORECASE,
)
NON_TECH_LINE_RE = re.compile(
    r"^\s*(?:soft\s+skills|interests?|hobbies|languages\s+known|extracurricular)",
    re.IGNORECASE,
)


def _contact_info(text: str) -> dict:
    email = EMAIL_RE.search(text)
    linkedin = LINKEDIN_RE.search(text)
    github = GITHUB_RE.search(text)
    # Look for a phone number only in the header block, so a year range or
    # a project metric further down can't be mistaken for one.
    header = "\n".join(text.splitlines()[:12])
    phone = ""
    for m in PHONE_RE.finditer(header):
        digits = re.sub(r"\D", "", m.group(0))
        if 10 <= len(digits) <= 13:
            phone = m.group(0).strip()
            break
    return {
        "email": email.group(0) if email else "",
        "phone": phone,
        "linkedin": linkedin.group(0) if linkedin else "",
        "github": github.group(0) if github else "",
    }


def _candidate_name(text: str) -> str:
    """The name is almost always the first real line of a resume."""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or len(line) > 60:
            continue
        if "@" in line or re.search(r"\d", line):
            continue
        if re.fullmatch(r"(?:[A-Za-z.'-]+\s*){1,4}", line):
            return line
        return ""
    return ""


def _skills_from_lines(section_text: str) -> list[str]:
    """Parse a Skills section: 'Languages: Python, Java | Tools: Git'."""
    found: list[str] = []
    for line in (section_text or "").splitlines():
        line = BULLET_RE.sub("", line).strip()
        if not line or NON_TECH_LINE_RE.match(line):
            continue
        line = SKILL_SUBHEADER_RE.sub("", line)
        for part in re.split(r"[,;|/\u2022\u00b7]|\s{3,}", line):
            name, _cat = normalize_skill(part)
            if name:
                found.append(name)
    return found


MONTHS = {m: i for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1)}


def _month_year(value: str) -> tuple[int, int] | None:
    year_m = YEAR_RE.search(value)
    if not year_m:
        return None
    month_m = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", value, re.IGNORECASE)
    return int(year_m.group(0)), MONTHS[month_m.group(1).lower()] if month_m else 1


def _years_between(start: str, end: str) -> float:
    """Duration in years, month-accurate when the dates name months, so a
    six-month internship counts as 0.5 rather than 0."""
    start_parts = _month_year(start)
    if not start_parts:
        return 0.0
    if re.search(r"present|current|now", end, re.IGNORECASE):
        today = datetime.date.today()
        end_parts = (today.year, today.month)
    else:
        end_parts = _month_year(end) or start_parts
    months = (end_parts[0] * 12 + end_parts[1]) - (start_parts[0] * 12 + start_parts[1])
    return round(max(0.0, months / 12), 1)


def _extract_experience(section_text: str) -> list[dict]:
    """One entry per date range. Walks the section line by line: a line
    containing a date range starts an entry, its title is whatever else is
    on that line (or the plain line just above it), and the bullets that
    follow are its highlights."""
    entries: list[dict] = []
    blocks: list[list[str]] = []
    current: dict | None = None
    pending_title = ""

    for line in (section_text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = DATE_RANGE_RE.search(stripped)
        if m:
            title_on_line = DATE_RANGE_RE.sub("", stripped).strip(" |,·-–—\t")
            current = {
                "title": (title_on_line or pending_title)[:120],
                "duration": f"{m.group('start')} - {m.group('end')}",
                "years": _years_between(m.group("start"), m.group("end")),
                "highlights": [],
                "skills": [],
            }
            entries.append(current)
            blocks.append([stripped])
            pending_title = ""
        elif BULLET_RE.match(stripped):
            if current:
                text = BULLET_RE.sub("", stripped)
                current["highlights"] = (current["highlights"] + [text])[:6]
                blocks[-1].append(text)
        else:
            # A plain line is the next entry's header, not part of this one.
            pending_title = stripped

    for entry, block in zip(entries, blocks):
        entry["skills"] = find_skills(" ".join(block))
    return entries


def _extract_projects(section_text: str) -> list[dict]:
    """A project starts on a non-bullet line; the bullets under it are its
    description, and any taxonomy skills in the block are its stack."""
    projects: list[dict] = []
    current: dict | None = None
    for line in (section_text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if BULLET_RE.match(stripped):
            if current:
                current["description"].append(BULLET_RE.sub("", stripped))
        else:
            current = {"name": stripped[:120], "description": [], "skills": []}
            projects.append(current)
    for p in projects:
        p["skills"] = find_skills(p["name"] + " " + " ".join(p["description"]))
    return projects[:10]


def _extract_education(section_text: str, full_text: str) -> list[dict]:
    source = section_text or full_text
    seen, entries = set(), []
    for m in DEGREE_RE.finditer(source):
        degree = m.group(0).strip()
        if degree.lower() in seen:
            continue
        seen.add(degree.lower())
        year = YEAR_RE.search(degree) or YEAR_RE.search(source[m.end():m.end() + 60])
        entries.append({"degree": degree, "year": year.group(0) if year else ""})
    return entries[:6]


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _profile_summary(profile: dict) -> str:
    """A readable one-paragraph summary built from the fields we just
    extracted — so there is always a summary to show, even when the
    resume has no Summary/Objective section of its own."""
    parts: list[str] = []

    who = profile["name"] or "This candidate"
    experience = profile["experience"]
    years = profile["total_experience_years"]
    if experience and years:
        parts.append(
            f"{who} — most recently {experience[0]['title'] or 'in an unnamed role'} — "
            f"with {years} yrs of dated experience across {_plural(len(experience), 'role')}."
        )
    elif experience:
        parts.append(
            f"{who}, with {_plural(len(experience), 'role')} listed but under a year of dated experience."
        )
    else:
        parts.append(f"{who}, with no dated work experience on the resume.")

    skills = profile["skills"]
    if skills:
        categories = [c.replace("_", " ") for c in profile["skills_by_category"]]
        parts.append(
            f"{_plural(len(skills), 'technical skill')} detected across "
            f"{_plural(len(categories), 'area')} ({', '.join(categories[:5])}), "
            f"including {', '.join(skills[:6])}."
        )

    if profile["education"]:
        top = profile["education"][0]
        year = f" ({top['year']})" if top["year"] else ""
        parts.append(f"Education: {top['degree']}{year}.")

    counts = []
    if profile["projects"]:
        counts.append(_plural(len(profile["projects"]), "project"))
    if profile["certifications"]:
        counts.append(_plural(len(profile["certifications"]), "certification"))
    if counts:
        parts.append(" and ".join(counts).capitalize() + " listed.")

    return " ".join(parts)


def parse_resume(resume_text: str) -> dict:
    """Raw resume text -> structured candidate profile."""
    text = resume_text or ""
    sections = segment_sections(text)

    # Skills are only looked for in Skills / Projects / Experience /
    # Certifications. Scanning the whole resume is what lets college names
    # and degree titles leak into the skill list.
    scoped = "\n".join(
        sections.get(k, "") for k in ("skills", "projects", "experience", "certifications")
    ).strip() or text

    skills: list[str] = []
    seen: set[str] = set()
    for name in find_skills(scoped) + _skills_from_lines(sections.get("skills", "")):
        if name.lower() not in seen:
            seen.add(name.lower())
            skills.append(name)

    experience = _extract_experience(sections.get("experience", ""))
    contact = _contact_info(text)

    profile = {
        "name": _candidate_name(text),
        "email": contact["email"],
        "phone": contact["phone"],
        "linkedin": contact["linkedin"],
        "github": contact["github"],
        "summary": sections.get("summary", "")[:600],
        "skills": skills,
        "skills_by_category": categorize_skills(skills),
        "education": _extract_education(sections.get("education", ""), text),
        "experience": experience,
        "total_experience_years": round(sum(e["years"] for e in experience), 1),
        "projects": _extract_projects(sections.get("projects", "")),
        "certifications": [
            BULLET_RE.sub("", l.strip())
            for l in sections.get("certifications", "").splitlines()
            if l.strip()
        ][:10],
    }
    # "summary" is whatever the resume itself said; "profile_summary" is
    # ours, built from the extracted fields, so there's always one.
    profile["profile_summary"] = _profile_summary(profile)
    return profile


# ═════════════════════════════════════════════════════════════════════════
# 5. JOB DESCRIPTION -> REQUIREMENTS
# ═════════════════════════════════════════════════════════════════════════

# Used both to name the role and, when a JD lists no skills at all, to
# fall back to the skills that role normally needs. Most specific first —
# the first match wins.
ROLE_PROFILES: list[tuple[str, re.Pattern, list[str]]] = [
    ("Full Stack Developer", re.compile(r"full[\s-]?stack", re.I),
     ["JavaScript", "React", "Node.js", "SQL", "HTML", "CSS", "Git", "REST API"]),
    ("Frontend Developer", re.compile(r"front[\s-]?end", re.I),
     ["JavaScript", "TypeScript", "React", "HTML", "CSS", "Git", "REST API"]),
    ("Backend Developer", re.compile(r"back[\s-]?end", re.I),
     ["Python", "Java", "SQL", "REST API", "Git", "Docker"]),
    ("Machine Learning Engineer", re.compile(r"machine[\s-]?learning engineer|\bml engineer\b", re.I),
     ["Python", "Machine Learning", "TensorFlow", "PyTorch", "SQL", "Git"]),
    ("Data Scientist", re.compile(r"data scientist", re.I),
     ["Python", "SQL", "Machine Learning", "Pandas", "NumPy", "scikit-learn"]),
    ("Data Engineer", re.compile(r"data engineer", re.I),
     ["Python", "SQL", "AWS", "PostgreSQL", "Git"]),
    ("AI / GenAI Engineer", re.compile(r"\bgen[\s-]?ai\b|\bllm\b|ai engineer", re.I),
     ["Python", "OpenAI API", "LangChain", "RAG", "Prompt Engineering", "Git"]),
    ("DevOps Engineer", re.compile(r"dev[\s-]?ops|site reliability|\bsre\b", re.I),
     ["Docker", "Kubernetes", "CI/CD", "AWS", "Terraform", "Linux", "Git"]),
    ("Cloud Engineer", re.compile(r"cloud engineer|cloud architect", re.I),
     ["AWS", "Azure", "GCP", "Docker", "Terraform", "Kubernetes"]),
    ("QA Engineer", re.compile(r"\bqa\b|quality assurance|test engineer|\bsdet\b", re.I),
     ["Selenium", "Pytest", "Jest", "Git", "REST API", "Postman"]),
    ("Android Developer", re.compile(r"android (?:developer|engineer)", re.I),
     ["Kotlin", "Java", "Git", "REST API"]),
    ("iOS Developer", re.compile(r"\bios (?:developer|engineer)\b", re.I),
     ["Swift", "Git", "REST API"]),
    ("Python Developer", re.compile(r"python (?:developer|engineer)", re.I),
     ["Python", "SQL", "REST API", "Git", "Docker"]),
    ("Java Developer", re.compile(r"java (?:developer|engineer)", re.I),
     ["Java", "Spring", "SQL", "REST API", "Git"]),
    ("Software Engineer", re.compile(r"software (?:engineer|developer)|\bswe\b|programmer", re.I),
     ["Python", "Java", "SQL", "Git", "Data Structures"]),
]

TITLE_LABEL_RE = re.compile(r"^\s*(?:job\s+title|position|role|designation)\s*[:\-]\s*(.+)$", re.I)
MIN_YEARS_RE = re.compile(r"(\d+)\s*\+?\s*(?:-\s*\d+\s*)?(?:to\s*\d+\s*)?year", re.IGNORECASE)

# Cues that mark a requirement as optional rather than mandatory.
PREFERRED_CUE_RE = re.compile(
    r"preferred|nice[\s-]to[\s-]have|good[\s-]to[\s-]have|desirable|desired|"
    r"\bbonus\b|\bplus\b|advantage|optional|familiarity with|exposure to|"
    r"\ba plus\b|would be great",
    re.IGNORECASE,
)
REQUIRED_CUE_RE = re.compile(
    r"requirements?|required|must[\s-]have|qualifications?|mandatory|essential|"
    r"minimum|responsibilities|what you.ll do|skills needed",
    re.IGNORECASE,
)
WORD_RE = re.compile(r"[A-Za-z][A-Za-z+#]*(?:[./\-][A-Za-z+#]+)*")


def _job_title(text: str) -> str:
    for line in text.splitlines()[:15]:
        m = TITLE_LABEL_RE.match(line)
        if m:
            return m.group(1).strip()[:80]
    for role, pattern, _skills in ROLE_PROFILES:
        if pattern.search(text):
            return role
    first = next((l.strip() for l in text.splitlines() if l.strip()), "")
    return first[:80]


def _split_required_preferred(text: str) -> tuple[list[str], list[str]]:
    """Walk the JD line by line, tracking whether we're inside a
    'Requirements' block or a 'Nice to have' block, and bucket the skills
    found on each line accordingly. Simple, and easy to eyeball when a
    skill lands in the wrong bucket."""
    required: list[str] = []
    preferred: list[str] = []
    mode = "required"

    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        is_header = len(stripped) < 70 and (stripped.endswith(":") or len(stripped.split()) <= 7)
        if is_header and PREFERRED_CUE_RE.search(stripped):
            mode = "preferred"
        elif is_header and REQUIRED_CUE_RE.search(stripped):
            mode = "required"

        # An inline cue ("Docker is a plus") only affects its own line.
        bucket = preferred if (mode == "preferred" or PREFERRED_CUE_RE.search(stripped)) else required
        bucket.extend(find_skills(stripped))

    required_unique = list(dict.fromkeys(required))
    preferred_unique = [s for s in dict.fromkeys(preferred) if s not in required_unique]
    return required_unique, preferred_unique


def _jd_keywords(text: str, skills: list[str], limit: int = 10) -> list[str]:
    """Frequent non-skill terms from the JD (single words and two-word
    phrases) — the 'extra' ATS keywords beyond the skill list.

    Phrases are only built within a single clause, so two unrelated
    sentences can't be glued into a nonsense phrase like "design solid".
    """
    skill_words = {w.lower() for s in skills for w in WORD_RE.findall(s)}

    def usable(word: str) -> bool:
        low = word.lower()
        return len(low) > 3 and low not in STOPWORDS and low not in skill_words

    scores: Counter[str] = Counter()
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Section headers ("Requirements:", "Nice to have") are structure,
        # not keywords.
        is_header = len(stripped) < 70 and (stripped.endswith(":") or len(stripped.split()) <= 3)
        if is_header and (REQUIRED_CUE_RE.search(stripped) or PREFERRED_CUE_RE.search(stripped)):
            continue
        for clause in re.split(r"[.;:!?()•,]", stripped):
            words = [w for w in WORD_RE.findall(clause)]
            for w in words:
                if usable(w):
                    scores[w.lower()] += 1
            # Two-word phrases score double so real phrases outrank loose words.
            for a, b in zip(words, words[1:]):
                if usable(a) and usable(b):
                    scores[f"{a.lower()} {b.lower()}"] += 2

    # Keep the best-scoring term per word family, so "backend engineer"
    # doesn't drag in "senior backend", "backend services", "backend"...
    keywords: list[str] = []
    used: set[str] = set()
    for term, score in scores.most_common():
        # A single word mentioned once is noise, not an ATS keyword.
        if score < 2:
            continue
        words = set(term.split())
        if words & used:
            continue
        keywords.append(term)
        used |= words
        if len(keywords) >= limit:
            break
    return [k.title() for k in keywords]


def analyze_job_description(jd_text: str, use_llm: bool = False) -> dict:
    """Raw JD text -> {job_title, required_skills, preferred_skills,
    keywords, min_years_experience}.

    With `use_llm` (and a Groq key configured) the JD text is also sent to
    the model, which reads the wording and decides what is mandatory vs
    nice-to-have. The deterministic pass below always runs first and is
    handed to the model as a baseline, so if the call fails the result is
    simply the deterministic one.
    """
    deterministic = _analyze_jd_deterministic(jd_text)
    if not use_llm or not llm_available():
        return deterministic
    return llm_extract_job_requirements(jd_text, deterministic)


def _analyze_jd_deterministic(jd_text: str) -> dict:
    """Regex + taxonomy JD extraction. No model involved."""
    text = jd_text or ""
    required, preferred = _split_required_preferred(text)

    # Some JDs only name a role ("Hiring a Backend Engineer, 3+ yrs") and
    # list no skills at all. Rather than score against an empty list, fall
    # back to that role's typical skills — but only when the JD itself
    # mentions none, so anything it does say always wins.
    skills_source = "explicit"
    if not required and not preferred:
        for role, pattern, role_skills in ROLE_PROFILES:
            if pattern.search(text):
                required, skills_source = list(role_skills), f"inferred from role: {role}"
                break

    years_m = MIN_YEARS_RE.search(text)
    return {
        "job_title": _job_title(text),
        "required_skills": required,
        "preferred_skills": preferred,
        "keywords": _jd_keywords(text, required + preferred),
        "min_years_experience": int(years_m.group(1)) if years_m else 0,
        "skills_source": skills_source,
    }


# ═════════════════════════════════════════════════════════════════════════
# 6. RESUME <-> JD MATCHING
# ═════════════════════════════════════════════════════════════════════════

def match_skills(required: list[str], resume_skills: list[str], resume_text: str = "") -> dict:
    """Compare one JD skill list against the resume. Two ways a skill can
    match, both auditable:

      exact   — the skill (any spelling) appears in the resume
      implied — a skill the candidate has stands in for it (MySQL -> SQL)

    Returns {"matched": [...], "missing": [...], "detail": [...]}.
    """
    owned_lower = {s.lower() for s in resume_skills}
    haystack = (resume_text or "") + "\n" + "\n".join(resume_skills)

    matched, missing, detail = [], [], []
    for skill in required:
        pattern = SKILL_PATTERNS.get(skill) or re.compile(re.escape(skill), re.IGNORECASE)
        if skill.lower() in owned_lower or pattern.search(haystack):
            matched.append(skill)
            detail.append({"skill": skill, "method": "exact"})
            continue
        via = next((o for o in resume_skills if skill in SKILL_IMPLIES.get(o, [])), None)
        if via:
            matched.append(skill)
            detail.append({"skill": skill, "method": "implied", "via": via})
        else:
            missing.append(skill)
    return {"matched": matched, "missing": missing, "detail": detail}


def match_keywords(keywords: list[str], resume_text: str) -> dict:
    text = (resume_text or "").lower()
    matched = [k for k in keywords if k.lower() in text]
    return {"matched": matched, "missing": [k for k in keywords if k not in matched]}


# ═════════════════════════════════════════════════════════════════════════
# 7. ATS SCORE  (fixed weights, no LLM, same input -> same output)
# ═════════════════════════════════════════════════════════════════════════

WEIGHTS = {
    "required_skills": 60,
    "preferred_skills": 20,
    "experience": 10,
    "keywords": 10,
}

COMPONENT_LABELS = {
    "required_skills": "Required skills",
    "preferred_skills": "Preferred skills",
    "experience": "Experience",
    "keywords": "Keyword match",
}


def _verdict(score: int, scorable: bool = True) -> str:
    if not scorable:
        return "Not scored — no requirements found in the job description"
    if score >= 75:
        return "Strong match"
    if score >= 60:
        return "Good match"
    if score >= 40:
        return "Partial match"
    return "Weak match"


def _explain_score(ats_score: int, breakdown: dict, notes: dict, scorable: bool,
                   matches: dict, detail: list[dict]) -> list[str]:
    """Line-by-line arithmetic behind the score, in plain English, so the
    number is never something the user has to take on trust."""
    if not scorable:
        return [
            "No score could be calculated: the job description contained no "
            "recognisable required skills, preferred skills, keywords or "
            "experience requirement to compare the resume against."
        ]

    lines: list[str] = []
    for name, comp in breakdown.items():
        label = COMPONENT_LABELS[name]
        if not comp["applicable"]:
            lines.append(
                f"{label} — 0 points: {notes[name]}, so this component's "
                f"{WEIGHTS[name]}% weight was shared out across the others."
            )
            continue
        line = (
            f"{label} — {comp['earned']} of {comp['max']} points "
            f"({round(comp['ratio'] * 100)}% of this component): {notes[name]}"
        )
        matched, missing = matches.get(name, ([], []))
        if matched:
            line += f". Credited: {', '.join(matched[:8])}"
        if missing:
            line += f". Not found: {', '.join(missing[:8])}"
        lines.append(line + ".")

    implied = [d for d in detail if d["method"] == "implied"]
    if implied:
        lines.append(
            "Credited indirectly: "
            + ", ".join(f"{d['skill']} (from {d['via']})" for d in implied[:6])
            + " — a skill on the resume is accepted as evidence for a related one."
        )

    earned = [c["earned"] for c in breakdown.values() if c["applicable"]]
    lines.append(
        "Total: " + " + ".join(str(e) for e in earned)
        + f" = {round(sum(earned), 1)}, rounded to {ats_score}%."
    )
    return lines


def calculate_ats_score(resume_profile: dict, jd_analysis: dict, resume_text: str = "") -> dict:
    """The whole comparison, scored.

    Each of the four components produces a ratio in [0, 1]. A component
    whose requirement the JD never states (no preferred skills, no years,
    no keywords) is marked not-applicable and its weight is dropped from
    the total, so the remaining components are rescaled to 100 instead of
    the score being silently capped.
    """
    resume_skills = resume_profile.get("skills") or []
    required = jd_analysis.get("required_skills") or []
    preferred = jd_analysis.get("preferred_skills") or []
    keywords = jd_analysis.get("keywords") or []

    req = match_skills(required, resume_skills, resume_text)
    pref = match_skills(preferred, resume_skills, resume_text)
    kw = match_keywords(keywords, resume_text + "\n" + "\n".join(resume_skills))

    # ── component ratios ────────────────────────────────────────────────
    ratios: dict[str, float | None] = {}
    notes: dict[str, str] = {}

    ratios["required_skills"] = len(req["matched"]) / len(required) if required else None
    notes["required_skills"] = (
        f"{len(req['matched'])}/{len(required)} required skills found"
        if required else "the job description lists no required skills"
    )

    ratios["preferred_skills"] = len(pref["matched"]) / len(preferred) if preferred else None
    notes["preferred_skills"] = (
        f"{len(pref['matched'])}/{len(preferred)} preferred skills found"
        if preferred else "the job description lists no preferred skills"
    )

    candidate_years = float(resume_profile.get("total_experience_years") or 0)
    required_years = int(jd_analysis.get("min_years_experience") or 0)
    relevant_years = round(sum(
        e.get("years", 0) for e in (resume_profile.get("experience") or [])
        if set(e.get("skills") or []) & set(required + preferred)
    ), 1)
    # If the JD stated nothing at all, experience alone must not produce a
    # score — there is nothing to score against.
    jd_is_empty = not (required or preferred or keywords or required_years)
    if jd_is_empty:
        ratios["experience"] = None
        notes["experience"] = "nothing to compare against"
    elif required_years > 0:
        ratios["experience"] = min(1.0, candidate_years / required_years)
        notes["experience"] = (
            f"{candidate_years} yrs on the resume vs {required_years} yrs asked for "
            f"({relevant_years} yrs of it in roles touching this JD's skills)"
        )
    elif candidate_years > 0:
        ratios["experience"] = 1.0
        notes["experience"] = f"{candidate_years} yrs of experience, no minimum stated in the JD"
    else:
        ratios["experience"] = 0.6
        notes["experience"] = "no dated work experience found on the resume"

    ratios["keywords"] = len(kw["matched"]) / len(keywords) if keywords else None
    notes["keywords"] = (
        f"{len(kw['matched'])}/{len(keywords)} job-description keywords appear in the resume"
        if keywords else "no keywords could be extracted from the job description"
    )

    # ── weighted score ──────────────────────────────────────────────────
    applicable = {k: r for k, r in ratios.items() if r is not None}
    total_weight = sum(WEIGHTS[k] for k in applicable) or 1
    scale = 100 / total_weight

    breakdown = {}
    for name, weight in WEIGHTS.items():
        ratio = ratios[name]
        breakdown[name] = {
            "earned": round(weight * ratio * scale, 1) if ratio is not None else 0.0,
            "max": round(weight * scale, 1) if ratio is not None else 0.0,
            "ratio": round(ratio, 3) if ratio is not None else None,
            "applicable": ratio is not None,
            "note": notes[name],
        }

    ats_score = min(100, round(sum(c["earned"] for c in breakdown.values())))
    scorable = bool(applicable)

    explanation = _explain_score(
        ats_score, breakdown, notes, scorable,
        {"required_skills": (req["matched"], req["missing"]),
         "preferred_skills": (pref["matched"], pref["missing"]),
         "keywords": (kw["matched"], kw["missing"])},
        req["detail"] + pref["detail"],
    )

    return {
        "ats_score": ats_score,
        "verdict": _verdict(ats_score, scorable=scorable),
        "summary": (
            f"{ats_score}% — {_verdict(ats_score, scorable)}"
            + (f" for \"{jd_analysis.get('job_title')}\"" if jd_analysis.get("job_title") else "")
            + f". Matched {len(req['matched'])} of {len(required)} required skills"
            + (f" and {len(pref['matched'])} of {len(preferred)} preferred skills" if preferred else "")
            + "."
        ),
        "explanation": explanation,
        "breakdown": breakdown,
        "weights": WEIGHTS,
        "matched_required_skills": req["matched"],
        "missing_required_skills": req["missing"],
        "matched_preferred_skills": pref["matched"],
        "missing_preferred_skills": pref["missing"],
        "match_detail": req["detail"] + pref["detail"],
        "matched_keywords": kw["matched"],
        "missing_keywords": kw["missing"],
        "matched_skills": req["matched"] + pref["matched"],
        "missing_skills": req["missing"] + pref["missing"],
        "suggestions": _suggestions(req["missing"], pref["missing"], kw["missing"], breakdown),
    }


def _suggestions(missing_required: list[str], missing_preferred: list[str],
                 missing_keywords: list[str], breakdown: dict) -> list[str]:
    """Plain, rule-based advice derived from the same numbers above."""
    out = []
    if missing_required:
        out.append(
            "Add evidence for the required skills you actually have: "
            + ", ".join(missing_required[:6]) + "."
        )
    if missing_preferred:
        out.append(
            "These preferred skills would strengthen the application: "
            + ", ".join(missing_preferred[:5]) + "."
        )
    if missing_keywords:
        out.append(
            "Work these job-description terms into your bullets where they are true: "
            + ", ".join(missing_keywords[:5]) + "."
        )
    exp = breakdown["experience"]
    if exp["applicable"] and exp["ratio"] is not None and exp["ratio"] < 1.0:
        out.append(
            "Your listed experience is short of what the role asks for — make sure every "
            "internship, freelance and project date range is on the resume."
        )
    if not out:
        out.append("This resume already covers the job description's stated requirements.")
    return out


# ═════════════════════════════════════════════════════════════════════════
# 8. WHOLE PIPELINE IN ONE CALL
# ═════════════════════════════════════════════════════════════════════════

def analyze(resume_bytes: bytes, filename: str, jd_text: str, use_llm: bool = True) -> dict:
    """PDF + job description -> profile, requirements and ATS score."""
    resume_text = extract_pdf_text(resume_bytes, filename)
    markup = extract_pdf_markup(resume_bytes, filename) if use_llm else ""
    profile = parse_resume_smart(resume_text, markup, use_llm=use_llm)
    jd_analysis = analyze_job_description(jd_text, use_llm=use_llm)
    ats = calculate_ats_score(profile, jd_analysis, resume_text)
    review = llm_review(profile, jd_analysis, ats, resume_text, jd_text) if use_llm else None
    return {
        "resume": profile,
        "resume_text": resume_text,
        "job_description": jd_analysis,
        "ats": ats,
        "llm_review": review,
    }


# ═════════════════════════════════════════════════════════════════════════
# 9. OPTIONAL LLM REVIEW  (Groq)
# ═════════════════════════════════════════════════════════════════════════
# Everything above runs with no model and no network. This last layer is a
# second opinion on top of it: the deterministic result is handed to an LLM
# which returns a final score and a written summary.
#
# Two rules keep the LLM honest:
#   1. It never computes the score from scratch — it sees the deterministic
#      score and its full breakdown, and may only adjust it.
#   2. Its adjustment is clamped to +/- MAX_LLM_ADJUSTMENT points, so a bad
#      model response can nudge the number but never invent one.
# If GROQ_API_KEY is unset, or the call fails, or the reply isn't valid
# JSON, this returns a dict with an "error" and the deterministic result
# stands untouched. The app never breaks because the LLM did.

DEFAULT_LLM_MODEL = "openai/gpt-oss-120b"

LLM_SYSTEM_PROMPT = """You are a senior technical recruiter doing a final \
review of an automated ATS analysis of one resume against one job description.

A deterministic engine has already extracted the skills and computed a score \
from fixed weights (required skills 60%, preferred skills 20%, experience 10%, \
keyword match 10%). You are NOT recalculating that score from scratch. Your job:

1. Sanity-check it against the actual resume and job description text. The \
engine matches literal keywords, so it can miss real evidence written in \
different words, or over-credit a skill that is only name-dropped.
2. Decide the FINAL score. Keep the engine's score unless the resume text \
genuinely justifies a change, and explain any change you make.
3. Write a clear summary a candidate can act on.

Be specific and honest. Never invent skills, employers, or experience that are \
not in the text you were given. Reply with JSON only."""


def llm_available() -> bool:
    """True when a Groq API key is configured (read at call time, so a .env
    loaded after import still counts)."""
    return bool(os.getenv("GROQ_API_KEY", "").strip())


def _groq_json(system_prompt: str, user_prompt: str, max_tokens: int = 1600,
               temperature: float = 0.2) -> str:
    """One Groq chat call that asks for JSON back. Raises on failure; each
    caller decides what to do about it."""
    from groq import Groq

    client = Groq(api_key=os.getenv("GROQ_API_KEY", "").strip())
    kwargs = dict(
        model=os.getenv("GROQ_MODEL", DEFAULT_LLM_MODEL),
        temperature=temperature,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    try:
        return client.chat.completions.create(
            response_format={"type": "json_object"}, **kwargs
        ).choices[0].message.content
    except Exception:
        # Not every Groq model supports JSON mode — retry without it and let
        # _parse_json_reply dig the object out of the reply.
        return client.chat.completions.create(**kwargs).choices[0].message.content


# ─────────────────────────────────────────────────────────────────────────
# 9a. AI JOB-DESCRIPTION EXTRACTION
# ─────────────────────────────────────────────────────────────────────────
# The regex pass can only find skills that are in SKILL_TAXONOMY, and it
# splits required vs preferred by looking for cue words like "nice to
# have". A model reads the actual sentences, so it catches skills the
# taxonomy has never heard of and judges intent from phrasing.
#
# The guardrail: every skill and keyword the model returns must literally
# appear in the JD text, or it is dropped and listed in
# `dropped_unverifiable`. That makes it impossible for the model to add a
# requirement the job description never stated.

JD_EXTRACT_SYSTEM_PROMPT = """You extract structured hiring requirements from \
a job description. Reply with JSON only.

Definitions you must follow exactly:

- required_skills: skills, tools or technical topics the job description \
treats as MANDATORY. Wording like "required", "must have", "strong X", \
"experience with X", "solid grasp of X", or an unqualified bullet in a \
Requirements section means mandatory.

- preferred_skills: skills that are NOT mandatory. Wording like "preferred", \
"nice to have", "good to have", "bonus", "a plus", "desirable", "familiarity \
with", "exposure to", or anything in a "Preferred qualifications" section. \
These are optional extras: having them makes a candidate stronger, and not \
having them is not disqualifying. Never put the same skill in both lists — if \
the JD states it as mandatory anywhere, it is required.

- keywords: important NON-skill terms and short phrases an ATS would scan \
for, such as "backend development", "REST API design", "distributed systems", \
"agile delivery". Do not repeat anything already listed as a skill.

- min_years_experience: the minimum years of experience stated, as an \
integer. 0 if the JD does not state one.

- job_title: the role title.

Hard rules:
1. Only list things the job description actually states. Never infer, expand \
or add anything that is not written in the text you were given.
2. Name skills in their common canonical form ("Python", not "Python 3.x \
programming"; "Machine Learning", not "ML experience").
3. One skill per list entry. No sentences."""


def _verify_in_text(items: list[str], text: str) -> tuple[list[str], list[str]]:
    """Keep only the entries that genuinely appear in the JD text — via the
    skill's taxonomy pattern if it has one, otherwise a literal
    case-insensitive match. Returns (kept, dropped)."""
    haystack = (text or "").lower()
    kept, dropped = [], []
    for item in items:
        pattern = SKILL_PATTERNS.get(item)
        found = pattern.search(text or "") if pattern else (item.lower() in haystack)
        (kept if found else dropped).append(item)
    return kept, dropped


def llm_extract_job_requirements(jd_text: str, baseline: dict) -> dict:
    """Send the JD text to the model and use its required/preferred/keyword
    split. Falls back to `baseline` (the deterministic result) on any
    failure, so this can never leave the caller without an analysis."""

    def failed(message: str) -> dict:
        return {**baseline, "analysis_source": "deterministic", "ai_error": message}

    user_prompt = (
        "Job description:\n\n" + (jd_text or "")[:8000] + "\n\n"
        "A keyword-matching pass over this text found the following. It only "
        "knows a fixed list of technologies and splits the two lists by cue "
        "words, so treat it as a hint, not the answer:\n"
        f"  required: {baseline['required_skills']}\n"
        f"  preferred: {baseline['preferred_skills']}\n\n"
        "Reply with ONLY this JSON object:\n"
        "{\n"
        '  "job_title": "<role title>",\n'
        '  "required_skills": ["<mandatory skill>"],\n'
        '  "preferred_skills": ["<optional / nice-to-have skill>"],\n'
        '  "keywords": ["<important non-skill term or phrase>"],\n'
        '  "min_years_experience": <integer>\n'
        "}"
    )

    try:
        raw = _groq_json(JD_EXTRACT_SYSTEM_PROMPT, user_prompt, max_tokens=1200)
    except Exception as e:
        return failed(f"The Groq request failed: {e}")
    try:
        data = _parse_json_reply(raw)
    except Exception as e:
        return failed(f"Could not read the model's reply: {e}")

    # Canonicalise every skill through the same normaliser the resume side
    # uses, otherwise "Python 3" from the JD would never match "Python"
    # from the resume.
    def canonical(values) -> list[str]:
        out: list[str] = []
        for raw_skill in _string_list(values, limit=40):
            name, _cat = normalize_skill(raw_skill)
            name = name or raw_skill.strip()
            if name and name not in out:
                out.append(name)
        return out

    required = canonical(data.get("required_skills"))
    preferred = [s for s in canonical(data.get("preferred_skills")) if s not in required]
    keywords = [
        k for k in _string_list(data.get("keywords"), limit=15)
        if k.lower() not in {s.lower() for s in required + preferred}
    ]

    # Guardrail: drop anything that isn't actually in the JD text.
    required, dropped_req = _verify_in_text(required, jd_text)
    preferred, dropped_pref = _verify_in_text(preferred, jd_text)
    keywords, dropped_kw = _verify_in_text(keywords, jd_text)

    if not required and not preferred:
        return failed("The model found no requirements in this job description.")

    try:
        min_years = int(data.get("min_years_experience") or 0)
    except (TypeError, ValueError):
        min_years = baseline["min_years_experience"]

    return {
        "job_title": str(data.get("job_title") or baseline["job_title"]).strip()[:80],
        "required_skills": required,
        "preferred_skills": preferred,
        "keywords": [k.title() if k.islower() else k for k in keywords],
        "min_years_experience": min_years,
        "skills_source": "explicit",
        "analysis_source": "ai",
        "model": os.getenv("GROQ_MODEL", DEFAULT_LLM_MODEL),
        "dropped_unverifiable": dropped_req + dropped_pref + dropped_kw,
        "deterministic_comparison": {
            "required_skills": baseline["required_skills"],
            "preferred_skills": baseline["preferred_skills"],
            "keywords": baseline["keywords"],
        },
    }


def _llm_payload(profile: dict, jd: dict, ats: dict, resume_text: str, jd_text: str) -> dict:
    """The facts handed to the model. Deliberately compact: the structured
    findings plus enough raw text to check them against."""
    return {
        "deterministic_ats_score": ats["ats_score"],
        "deterministic_verdict": ats["verdict"],
        "score_breakdown": {
            name: {"earned": c["earned"], "max": c["max"], "note": c["note"]}
            for name, c in ats["breakdown"].items()
        },
        "score_explanation": ats["explanation"],
        "job_description": {
            "job_title": jd["job_title"],
            "required_skills": jd["required_skills"],
            "preferred_skills": jd["preferred_skills"],
            "keywords": jd["keywords"],
            "min_years_experience": jd["min_years_experience"],
        },
        "matched_required_skills": ats["matched_required_skills"],
        "missing_required_skills": ats["missing_required_skills"],
        "matched_preferred_skills": ats["matched_preferred_skills"],
        "missing_preferred_skills": ats["missing_preferred_skills"],
        "candidate": {
            "name": profile["name"],
            "extracted_skills": profile["skills"],
            "total_experience_years": profile["total_experience_years"],
            "experience": [
                {"title": e["title"], "duration": e["duration"], "highlights": e["highlights"]}
                for e in profile["experience"]
            ],
            "education": profile["education"],
            "projects": [{"name": p["name"], "skills": p["skills"]} for p in profile["projects"]],
            "certifications": profile["certifications"],
        },
        "resume_text": (resume_text or "")[:6000],
        "job_description_text": (jd_text or "")[:4000],
    }


def _parse_json_reply(raw: str) -> dict:
    """Models sometimes wrap JSON in prose or a ```json fence. Pull out the
    first balanced object rather than trusting the whole string."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start == -1:
        raise ValueError("the model did not return any JSON")
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("the model returned an incomplete JSON object")


def _string_list(value, limit: int = 8) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()][:limit]


def llm_review(profile: dict, jd: dict, ats: dict,
               resume_text: str = "", jd_text: str = "") -> dict:
    """Ask Groq for the final score and summary. Always returns a dict —
    on any failure it carries an "error" and `final_ats_score` falls back
    to the deterministic score, so callers never need a try/except."""
    deterministic = ats["ats_score"]
    model = os.getenv("GROQ_MODEL", DEFAULT_LLM_MODEL)
    max_adjustment = int(os.getenv("MAX_LLM_ADJUSTMENT", "15"))

    def failed(message: str) -> dict:
        return {
            "used": False,
            "error": message,
            "model": model,
            "final_ats_score": deterministic,
            "deterministic_ats_score": deterministic,
            "adjustment": 0,
        }

    if not llm_available():
        return failed(
            "GROQ_API_KEY is not set, so the deterministic score is final. "
            "Add your key to the .env file to enable the AI review."
        )
    try:
        from groq import Groq
    except ImportError:
        return failed("The groq package is not installed (pip install groq).")

    payload = _llm_payload(profile, jd, ats, resume_text, jd_text)
    user_prompt = (
        "Here is the automated analysis to review:\n\n"
        + json.dumps(payload, indent=2, ensure_ascii=False)
        + f"\n\nThe deterministic engine scored this {deterministic}%. You may "
          f"adjust it by at most {max_adjustment} points in either direction.\n\n"
        "Reply with ONLY this JSON object:\n"
        "{\n"
        '  "final_ats_score": <integer 0-100>,\n'
        '  "verdict": "<short phrase, e.g. Strong match>",\n'
        '  "summary": "<2-4 sentences on how well this resume fits this role>",\n'
        '  "score_reasoning": "<why you kept or changed the engine score>",\n'
        '  "resume_summary": "<2-3 sentences summarising the candidate>",\n'
        '  "strengths": ["<specific, evidence-backed>"],\n'
        '  "weaknesses": ["<specific gap vs this JD>"],\n'
        '  "recommendations": ["<concrete resume change>"]\n'
        "}"
    )

    try:
        client = Groq(api_key=os.getenv("GROQ_API_KEY", "").strip())
        kwargs = dict(
            model=model,
            temperature=0.2,
            max_tokens=1600,
            messages=[
                {"role": "system", "content": LLM_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        try:
            response = client.chat.completions.create(
                response_format={"type": "json_object"}, **kwargs
            )
        except Exception:
            # Not every Groq model supports JSON mode — retry without it and
            # lean on _parse_json_reply instead.
            response = client.chat.completions.create(**kwargs)
        raw = response.choices[0].message.content
    except Exception as e:
        return failed(f"The Groq request failed: {e}")

    try:
        data = _parse_json_reply(raw)
    except Exception as e:
        return failed(f"Could not read the model's reply: {e}")

    # Clamp the score: the model adjusts, it does not author.
    try:
        proposed = int(round(float(data.get("final_ats_score", deterministic))))
    except (TypeError, ValueError):
        proposed = deterministic
    lower, upper = deterministic - max_adjustment, deterministic + max_adjustment
    final = max(0, min(100, max(lower, min(upper, proposed))))

    return {
        "used": True,
        "model": model,
        "final_ats_score": final,
        "deterministic_ats_score": deterministic,
        "proposed_ats_score": proposed,
        "adjustment": final - deterministic,
        "clamped": proposed != final,
        "max_adjustment": max_adjustment,
        "verdict": str(data.get("verdict") or ats["verdict"]).strip(),
        "summary": str(data.get("summary") or "").strip(),
        "score_reasoning": str(data.get("score_reasoning") or "").strip(),
        "resume_summary": str(data.get("resume_summary") or "").strip(),
        "strengths": _string_list(data.get("strengths")),
        "weaknesses": _string_list(data.get("weaknesses")),
        "recommendations": _string_list(data.get("recommendations")),
    }


# ─────────────────────────────────────────────────────────────────────────
# 9b. AI RESUME EXTRACTION (format-aware)
# ─────────────────────────────────────────────────────────────────────────
# The regex parser in section 4 works off section headers and a fixed skill
# taxonomy. It cannot tell that the bold line under EDUCATION is the college
# and the line under it is the degree, and it silently drops any technology
# it has never heard of.
#
# This pass hands the model the *marked-up* resume (see extract_pdf_markup)
# so it can use the same visual cues a recruiter does. Everything it returns
# is then checked back against the resume text: an entity that does not
# actually appear is flagged `verified: false`, and an unverifiable skill is
# dropped outright, since skills are what drive the ATS score.

RESUME_EXTRACT_SYSTEM_PROMPT = """You are an intelligent resume information \
extraction system. Reply with JSON only.

Extract entities by understanding the document's structure, formatting, \
headings and context. Do NOT rely on keyword matching.

The resume is given to you as markdown-style text where the original visual \
formatting has been preserved:
  **bold text**  was bold or emphasized in the PDF
  ## Heading     was a section heading

Use those signals. In most resumes:
- bold text under Education is the institution name
- bold text under Projects is the project name
- bold text under Experience is the company name or the job title
- bold text under Certifications is the certification name
- bold text under Skills is a skill category label

RULES

1. EDUCATION — for each entry extract institution, degree, specialization, \
dates and score. The institution is normally the bold line. Never record a \
degree ("Bachelor of Technology") as the institution.

2. PROJECTS — the project name is the bold or title-like line, not a \
technology and not a sentence lifted from the description. Preserve the name \
exactly as written, including any dash or subtitle. Also extract description, \
technologies and achievements.

3. EXPERIENCE — separate company, role, location, start and end dates, \
responsibilities and technologies. Use the section context and formatting to \
decide whether a bold line is the company or the role.

4. SKILLS — only real technologies: languages, frameworks, databases, tools, \
cloud platforms and technical concepts. Never include school or college \
names, company names, project names, job titles, soft skills, or verbs like \
"developed", "implemented", "engineered".

5. CERTIFICATIONS — the exact certification title (usually the bold text), \
plus issuer, date and credential URL. Do not swap the issuer for the title.

VALIDATION — before answering:
- every entity must actually appear in the resume; never invent one
- preserve names exactly as written; do not normalize or rename
- do not merge separate entities
- prefer a formatted title over a frequently repeated word
- when unsure, still return the entity but give it a low confidence score

Confidence is 0.0-1.0 and reflects how sure you are that the entity is \
correctly identified and correctly classified."""

_SKILL_BUCKETS = ["programming_languages", "frameworks", "databases", "tools",
                  "cloud", "ai_ml", "other"]


def _appears_in(value: str, haystack_lower: str) -> bool:
    """Is this entity really in the resume? Compares on letters and digits
    only, so '**Node.js**' vs 'Node . js' spacing differences don't cause a
    false negative."""
    if not value:
        return False
    squeeze = lambda s: re.sub(r"[^a-z0-9]+", "", s.lower())  # noqa: E731
    needle = squeeze(value)
    return bool(needle) and needle in squeeze(haystack_lower)


def llm_extract_resume(resume_markup: str, resume_text: str = "") -> dict:
    """Format-aware resume extraction. Always returns a dict; on failure it
    carries `used: False` and an `error`, and the caller keeps the regex
    result."""
    model = os.getenv("GROQ_MODEL", DEFAULT_LLM_MODEL)

    def failed(message: str) -> dict:
        return {"used": False, "error": message, "model": model}

    if not llm_available():
        return failed("GROQ_API_KEY is not set, so the regex parser was used instead.")

    user_prompt = (
        "Resume (formatting preserved as markdown):\n\n"
        + (resume_markup or "")[:14000]
        + "\n\nReply with ONLY this JSON object:\n"
        "{\n"
        '  "name": "", "email": "", "phone": "",\n'
        '  "education": [{"institution": "", "degree": "", "specialization": "",\n'
        '                 "dates": "", "score": "", "confidence": 0.0}],\n'
        '  "experience": [{"company": "", "role": "", "location": "", "dates": "",\n'
        '                  "description": [], "technologies": [], "confidence": 0.0}],\n'
        '  "projects": [{"name": "", "description": "", "technologies": [],\n'
        '                "achievements": [], "confidence": 0.0}],\n'
        '  "skills": {"programming_languages": [], "frameworks": [], "databases": [],\n'
        '             "tools": [], "cloud": [], "ai_ml": [], "other": []},\n'
        '  "certifications": [{"name": "", "issuer": "", "date": "",\n'
        '                      "credential_url": "", "confidence": 0.0}]\n'
        "}"
    )

    try:
        data = _parse_json_reply(
            _groq_json(RESUME_EXTRACT_SYSTEM_PROMPT, user_prompt, max_tokens=4000, temperature=0.1)
        )
    except Exception as e:
        return failed(f"Resume extraction failed: {e}")

    # Verify everything against the real text, per the validation rules.
    source = (resume_markup or "") + "\n" + (resume_text or "")
    dropped: list[str] = []

    def conf(entry: dict) -> float:
        try:
            return max(0.0, min(1.0, float(entry.get("confidence", 0.8))))
        except (TypeError, ValueError):
            return 0.8

    education = []
    for e in (data.get("education") or [])[:8]:
        institution = str(e.get("institution") or "").strip()
        education.append({
            "institution": institution,
            "degree": str(e.get("degree") or "").strip(),
            "specialization": str(e.get("specialization") or "").strip(),
            "dates": str(e.get("dates") or "").strip(),
            "score": str(e.get("score") or "").strip(),
            "confidence": conf(e),
            "verified": _appears_in(institution, source),
        })

    experience = []
    for x in (data.get("experience") or [])[:10]:
        company = str(x.get("company") or "").strip()
        experience.append({
            "company": company,
            "role": str(x.get("role") or "").strip(),
            "location": str(x.get("location") or "").strip(),
            "dates": str(x.get("dates") or "").strip(),
            "description": _string_list(x.get("description"), limit=8),
            "technologies": _string_list(x.get("technologies"), limit=15),
            "confidence": conf(x),
            "verified": _appears_in(company, source),
        })

    projects = []
    for p in (data.get("projects") or [])[:12]:
        pname = str(p.get("name") or "").strip()
        projects.append({
            "name": pname,
            "description": str(p.get("description") or "").strip(),
            "technologies": _string_list(p.get("technologies"), limit=15),
            "achievements": _string_list(p.get("achievements"), limit=6),
            "confidence": conf(p),
            "verified": _appears_in(pname, source),
        })

    # Skills feed the ATS score, so an unverifiable one is dropped, not
    # merely flagged.
    raw_skills = data.get("skills") or {}
    skills: dict[str, list[str]] = {}
    for bucket in _SKILL_BUCKETS:
        kept = []
        for s in _string_list(raw_skills.get(bucket), limit=40):
            if not _appears_in(s, source):
                dropped.append(s)
                continue
            if s.lower() in SOFT_SKILLS or ORG_WORD_RE.search(s):
                dropped.append(s)
                continue
            kept.append(s)
        skills[bucket] = kept

    certifications = []
    for c in (data.get("certifications") or [])[:10]:
        cname = str(c.get("name") or "").strip()
        certifications.append({
            "name": cname,
            "issuer": str(c.get("issuer") or "").strip(),
            "date": str(c.get("date") or "").strip(),
            "credential_url": str(c.get("credential_url") or "").strip(),
            "confidence": conf(c),
            "verified": _appears_in(cname, source),
        })

    return {
        "used": True,
        "model": model,
        "name": str(data.get("name") or "").strip(),
        "email": str(data.get("email") or "").strip(),
        "phone": str(data.get("phone") or "").strip(),
        "education": education,
        "experience": experience,
        "projects": projects,
        "skills": skills,
        "certifications": certifications,
        "dropped_unverifiable_skills": dropped,
    }


def _merge_ai_resume(profile: dict, ai: dict, resume_text: str) -> dict:
    """Fold the AI extraction into the profile shape the rest of the app
    already speaks, keeping the richer AI fields alongside.

    The regex result stays as the floor: a skill either parser found is
    kept, so turning the AI on never loses information.
    """
    merged = dict(profile)
    merged["extraction"] = {
        "source": "ai", "model": ai.get("model"),
        "dropped_unverifiable_skills": ai.get("dropped_unverifiable_skills", []),
    }

    # Contact details: prefer the regex hits, which are exact matches.
    merged["name"] = profile.get("name") or ai.get("name", "")
    merged["email"] = profile.get("email") or ai.get("email", "")
    merged["phone"] = profile.get("phone") or ai.get("phone", "")

    # Skills — union of both, canonicalised so JD matching still works.
    by_category: dict[str, list[str]] = {}
    flat: list[str] = []
    seen: set[str] = set()

    def add(raw: str, fallback_category: str) -> None:
        name, category = normalize_skill(raw)
        name = name or raw.strip()
        if not name or name.lower() in seen:
            return
        seen.add(name.lower())
        flat.append(name)
        by_category.setdefault(category or fallback_category, []).append(name)

    for bucket, items in (ai.get("skills") or {}).items():
        for item in items:
            add(item, bucket)
    for item in profile.get("skills") or []:
        add(item, "other")

    merged["skills"] = flat
    merged["skills_by_category"] = by_category
    merged["skills_detailed"] = ai.get("skills") or {}

    # Education / experience / projects / certifications: AI versions win
    # when it found any, since that is the whole point of this pass.
    if ai.get("education"):
        merged["education"] = [{
            "institution": e["institution"],
            "degree": " ".join(p for p in (e["degree"], e["specialization"]) if p).strip() or e["degree"],
            "year": e["dates"],
            "score": e["score"],
            "confidence": e["confidence"],
            "verified": e["verified"],
        } for e in ai["education"]]

    if ai.get("experience"):
        entries = []
        for x in ai["experience"]:
            title = " — ".join(p for p in (x["role"], x["company"]) if p) or x["company"] or x["role"]
            start, _, end = (x["dates"] or "").partition("-")
            entries.append({
                "title": title,
                "company": x["company"],
                "role": x["role"],
                "location": x["location"],
                "duration": x["dates"],
                "years": _years_between(start.strip(), end.strip() or start.strip()),
                "highlights": x["description"],
                "skills": [s for s in (x["technologies"] or []) if s],
                "confidence": x["confidence"],
                "verified": x["verified"],
            })
        merged["experience"] = entries
        merged["total_experience_years"] = round(sum(e["years"] for e in entries), 1)

    if ai.get("projects"):
        merged["projects"] = [{
            "name": p["name"],
            "description": [p["description"]] if p["description"] else [],
            "skills": p["technologies"],
            "achievements": p["achievements"],
            "confidence": p["confidence"],
            "verified": p["verified"],
        } for p in ai["projects"]]

    if ai.get("certifications"):
        merged["certifications"] = [
            " — ".join(p for p in (c["name"], c["issuer"]) if p) or c["name"]
            for c in ai["certifications"]
        ]
        merged["certifications_detailed"] = ai["certifications"]

    merged["profile_summary"] = _profile_summary(merged)
    return merged


def parse_resume_smart(resume_text: str, resume_markup: str = "", use_llm: bool = True) -> dict:
    """The regex parser, then the AI pass on top when it is available.

    Always returns a usable profile: if the AI call fails, the regex result
    is returned unchanged with the reason in `extraction.error`.
    """
    profile = parse_resume(resume_text)
    if not use_llm or not llm_available():
        profile["extraction"] = {"source": "regex"}
        return profile

    ai = llm_extract_resume(resume_markup or resume_text, resume_text)
    if not ai.get("used"):
        profile["extraction"] = {"source": "regex", "error": ai.get("error")}
        return profile
    return _merge_ai_resume(profile, ai, resume_text)


# ═════════════════════════════════════════════════════════════════════════
# 10. INTERVIEWER
# ═════════════════════════════════════════════════════════════════════════
# The job description goes to the LLM once, up front, which returns a plan:
# a set of topics, each holding a chain of questions that get progressively
# deeper. Traversal through that plan is then plain Python, so the rules are
# exact and cost nothing:
#
#   answered correctly  + a deeper question exists  -> ask it (worth more)
#   answered correctly  + no deeper question left   -> random new topic
#   answered wrongly                                -> random new topic
#
# Only grading a typed answer costs an LLM call. Points rise with depth, so
# the way to score well is to keep going deeper on a topic.

# depth level -> points that question is worth
LEVEL_POINTS = {1: 10, 2: 20, 3: 30}
MAX_LEVEL = max(LEVEL_POINTS)

INTERVIEW_PLAN_SYSTEM_PROMPT = """You are a technical interviewer building a \
question plan for one candidate. Reply with JSON only.

Produce a set of TOPICS. Each topic holds a chain of questions on the same \
subject that get progressively deeper:

  level 1 — an opening question any competent candidate for this role should \
handle. Definitions, basic usage, "what is / when would you".
  level 2 — a follow-up that only makes sense if they got level 1 right. \
Trade-offs, failure modes, "why", "what happens when".
  level 3 — a genuinely hard question. Scale, edge cases, design decisions, \
debugging something subtle.

Each level must build on the one before it, so that asking level 2 to \
someone who answered level 1 well feels like a natural "go on, deeper".

Rules:
1. Cover the technical skills and responsibilities named in the job \
description first. Those topics get "source": "job_description".
2. Also include 2 general topics that any engineer should handle regardless \
of the role — fundamentals, problem solving, or how they work. Mark those \
"source": "general".
3. Questions must be answerable by typing a few sentences. No coding \
exercises, no "write the function", no diagrams.
4. For every question give "expected": the key points a good answer covers. \
This is used to grade the reply, so be concrete and specific.
5. One question per level. Every topic needs all three levels."""

INTERVIEW_GRADE_SYSTEM_PROMPT = """You grade one typed interview answer. \
Reply with JSON only.

You are given the question, the key points a good answer should cover, and \
what the candidate actually wrote.

Judge only whether the answer is technically correct and substantive enough \
for the question's difficulty. Be fair but not generous:

- "correct": true only if the answer is factually right AND actually engages \
with the question. An answer that is vague, evasive, restates the question, \
says "I don't know", or is right about something else entirely is not \
correct.
- "score": 0-10 for the quality of the answer, independent of correct/false.
- "feedback": one or two sentences the candidate can learn from. Address them \
as "you". If they got it wrong, say what the right answer is.
- "missed": the key points they did not cover. Empty list if they covered \
everything."""


def _fallback_plan(jd_analysis: dict) -> dict:
    """Used when no LLM is available. Builds a thin plan from the skills the
    deterministic JD pass already found, so the interview still runs."""
    skills = (jd_analysis.get("required_skills") or [])[:5]
    topics = [{
        "topic": skill,
        "source": "job_description",
        "questions": [
            {"level": 1, "question": f"What is {skill} and what have you used it for?",
             "expected": f"A clear description of {skill} and concrete first-hand usage."},
            {"level": 2, "question": f"What are the main trade-offs or limitations of {skill}?",
             "expected": f"Specific downsides of {skill} and when to choose something else."},
            {"level": 3, "question": f"Describe the hardest problem you have debugged involving {skill}.",
             "expected": f"A specific incident, the diagnosis, and what fixed it."},
        ],
    } for skill in skills]
    topics.append({
        "topic": "Problem solving",
        "source": "general",
        "questions": [
            {"level": 1, "question": "Walk me through how you approach a problem you have never seen before.",
             "expected": "A structured approach: clarify, break down, test assumptions."},
            {"level": 2, "question": "Tell me about a time your first approach was wrong. How did you notice?",
             "expected": "Self-awareness, a real example, and what signal revealed the mistake."},
            {"level": 3, "question": "How do you decide when a solution is good enough to ship?",
             "expected": "Weighing risk, cost of delay, reversibility, and testing."},
        ],
    })
    return {"topics": topics, "plan_source": "fallback"}


def build_interview_plan(jd_text: str, jd_analysis: dict, resume_profile: dict | None = None) -> dict:
    """One LLM call: job description -> topics, each with a level 1/2/3
    question chain. Falls back to a skills-derived plan on any failure."""
    if not llm_available():
        return {**_fallback_plan(jd_analysis),
                "error": "GROQ_API_KEY is not set, so generic questions were used instead."}

    context = {
        "job_title": jd_analysis.get("job_title"),
        "required_skills": jd_analysis.get("required_skills"),
        "preferred_skills": jd_analysis.get("preferred_skills"),
        "min_years_experience": jd_analysis.get("min_years_experience"),
        "job_description_text": (jd_text or "")[:6000],
    }
    if resume_profile:
        context["candidate_skills"] = resume_profile.get("skills")

    user_prompt = (
        "Build the interview plan for this role:\n\n"
        + json.dumps(context, indent=2, ensure_ascii=False)
        + "\n\nReply with ONLY this JSON object:\n"
        "{\n"
        '  "topics": [\n'
        "    {\n"
        '      "topic": "<short subject name>",\n'
        '      "source": "job_description" | "general",\n'
        '      "questions": [\n'
        '        {"level": 1, "question": "...", "expected": "..."},\n'
        '        {"level": 2, "question": "...", "expected": "..."},\n'
        '        {"level": 3, "question": "...", "expected": "..."}\n'
        "      ]\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Give 6 to 8 topics."
    )

    try:
        raw = _groq_json(INTERVIEW_PLAN_SYSTEM_PROMPT, user_prompt, max_tokens=4000, temperature=0.6)
        data = _parse_json_reply(raw)
    except Exception as e:
        return {**_fallback_plan(jd_analysis), "error": f"Question generation failed: {e}"}

    topics = []
    for entry in (data.get("topics") or []):
        questions = []
        for q in (entry.get("questions") or []):
            text = str(q.get("question") or "").strip()
            if not text:
                continue
            try:
                level = int(q.get("level") or len(questions) + 1)
            except (TypeError, ValueError):
                level = len(questions) + 1
            questions.append({
                "level": max(1, min(MAX_LEVEL, level)),
                "question": text,
                "expected": str(q.get("expected") or "").strip(),
            })
        questions.sort(key=lambda q: q["level"])
        if questions:
            topics.append({
                "topic": str(entry.get("topic") or "General").strip()[:80],
                "source": "general" if entry.get("source") == "general" else "job_description",
                "questions": questions,
            })

    if not topics:
        return {**_fallback_plan(jd_analysis), "error": "The model returned no usable questions."}
    return {"topics": topics, "plan_source": "ai", "model": os.getenv("GROQ_MODEL", DEFAULT_LLM_MODEL)}


def grade_answer(question: str, expected: str, answer: str, level: int) -> dict:
    """Grade one typed answer. An empty answer is marked wrong without
    spending a call."""
    points_possible = LEVEL_POINTS[level]
    answer = (answer or "").strip()

    if not answer:
        return {"correct": False, "score": 0, "points_awarded": 0,
                "points_possible": points_possible,
                "feedback": "No answer was given, so this counts as incorrect.",
                "missed": [expected] if expected else [], "graded_by": "rule"}

    if not llm_available():
        # No key: accept any substantial answer at half credit rather than
        # blocking the interview, and say plainly that it wasn't graded.
        substantial = len(answer.split()) >= 15
        return {
            "correct": substantial, "score": 5 if substantial else 2,
            "points_awarded": points_possible // 2 if substantial else 0,
            "points_possible": points_possible,
            "feedback": "Not graded — no GROQ_API_KEY is configured, so answers "
                        "are only checked for length.",
            "missed": [], "graded_by": "rule",
        }

    user_prompt = (
        f"Question (difficulty level {level} of {MAX_LEVEL}):\n{question}\n\n"
        f"Key points a good answer covers:\n{expected or '(not specified)'}\n\n"
        f"The candidate's answer:\n{answer[:3000]}\n\n"
        "Reply with ONLY this JSON object:\n"
        '{"correct": true|false, "score": <0-10>, "feedback": "...", "missed": ["..."]}'
    )
    try:
        data = _parse_json_reply(_groq_json(INTERVIEW_GRADE_SYSTEM_PROMPT, user_prompt, max_tokens=700))
    except Exception as e:
        # Never let a grading failure end the interview.
        return {"correct": False, "score": 0, "points_awarded": 0,
                "points_possible": points_possible,
                "feedback": f"This answer could not be graded ({e}), so it was not scored.",
                "missed": [], "graded_by": "error"}

    correct = bool(data.get("correct"))
    try:
        score = max(0, min(10, int(round(float(data.get("score", 0))))))
    except (TypeError, ValueError):
        score = 5 if correct else 0
    # Points are proportional to answer quality, capped by the level's value.
    points = round(points_possible * score / 10) if correct else 0

    return {
        "correct": correct,
        "score": score,
        "points_awarded": points,
        "points_possible": points_possible,
        "feedback": str(data.get("feedback") or "").strip(),
        "missed": _string_list(data.get("missed"), limit=5),
        "graded_by": "llm",
    }


def new_interview_session(plan: dict, total_questions: int = 8) -> dict:
    """Fresh interview state. `asked` records (topic index, level) pairs so a
    question is never repeated."""
    return {
        "plan": plan,
        "total_questions": max(1, min(30, total_questions)),
        "asked": [],          # [[topic_idx, level], ...]
        "transcript": [],
        "points_earned": 0,
        "points_available": 0,
        "current": None,      # the question awaiting an answer
        "finished": False,
    }


def _pick_deeper(session: dict) -> dict | None:
    """The next level down on the topic just answered, if there is one."""
    current = session["current"]
    if not current:
        return None
    topic = session["plan"]["topics"][current["topic_index"]]
    next_level = current["level"] + 1
    if next_level > MAX_LEVEL:
        return None
    question = next((q for q in topic["questions"] if q["level"] == next_level), None)
    if not question or [current["topic_index"], next_level] in session["asked"]:
        return None
    return {
        "topic_index": current["topic_index"], "topic": topic["topic"],
        "source": topic["source"], "level": next_level,
        "question": question["question"], "expected": question["expected"],
        "points_possible": LEVEL_POINTS[next_level], "is_deeper": True,
    }


def _pick_random(session: dict) -> dict | None:
    """An opening (level 1) question from a topic not yet touched. Random, so
    two runs of the same interview don't follow the same path."""
    topics = session["plan"]["topics"]
    used_topics = {t for t, _lvl in session["asked"]}
    candidates = [
        i for i, topic in enumerate(topics)
        if i not in used_topics and any(q["level"] == 1 for q in topic["questions"])
    ]
    if not candidates:
        # Every topic has been opened — allow any level-1 question not asked.
        candidates = [
            i for i, topic in enumerate(topics)
            if [i, 1] not in session["asked"] and any(q["level"] == 1 for q in topic["questions"])
        ]
    if not candidates:
        return None
    index = random.choice(candidates)
    topic = topics[index]
    question = next(q for q in topic["questions"] if q["level"] == 1)
    return {
        "topic_index": index, "topic": topic["topic"], "source": topic["source"],
        "level": 1, "question": question["question"], "expected": question["expected"],
        "points_possible": LEVEL_POINTS[1], "is_deeper": False,
    }


def next_question(session: dict, last_correct: bool | None) -> dict | None:
    """The three rules, in order. `last_correct=None` means this is the first
    question of the interview."""
    if len(session["asked"]) >= session["total_questions"]:
        return None
    if last_correct:                        # rule 1: go deeper if we can
        deeper = _pick_deeper(session)
        if deeper:
            return deeper
    return _pick_random(session)            # rules 2 and 3: new topic


def serve_question(session: dict, question: dict) -> dict:
    """Record a question as asked and return the candidate-facing view of it
    (never includes `expected` — that would hand over the answer)."""
    session["asked"].append([question["topic_index"], question["level"]])
    session["current"] = question
    session["points_available"] += question["points_possible"]
    return {
        "index": len(session["asked"]),
        "total": session["total_questions"],
        "topic": question["topic"],
        "source": question["source"],
        "level": question["level"],
        "max_level": MAX_LEVEL,
        "points_possible": question["points_possible"],
        "is_deeper": question["is_deeper"],
        "question": question["question"],
    }


def interview_summary(session: dict) -> dict:
    """Final tally. `points_available` is the sum of what was actually asked,
    which rises as the candidate unlocks deeper questions."""
    transcript = session["transcript"]
    correct = sum(1 for t in transcript if t["correct"])
    available = session["points_available"] or 1
    deepest = max((t["level"] for t in transcript), default=0)
    return {
        "questions_answered": len(transcript),
        "correct_answers": correct,
        "points_earned": session["points_earned"],
        "points_available": session["points_available"],
        "percentage": round(100 * session["points_earned"] / available),
        "deepest_level_reached": deepest,
        "topics_covered": sorted({t["topic"] for t in transcript}),
        "transcript": transcript,
    }
