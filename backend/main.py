"""
main.py — FastAPI app for the resume/JD ATS analyser.
======================================================

Thin HTTP layer only: request models, validation, and calls into
services.py. All the actual logic lives in services.py.

Stateless — nothing is stored on the server between requests. The
frontend keeps the resume profile it got back and posts it again when it
wants a score, so there are no sessions, no user ids and no database.

Endpoints:
    POST /api/analyze-resume    PDF                        -> candidate profile
    POST /api/analyze-job       {jd_text}                  -> JD requirements
    POST /api/calculate-ats     {resume, resume_text, jd_text} -> ATS score
    POST /api/analyze           PDF + jd_text (one shot)   -> all three
    GET  /api/health

Run:
    uvicorn main:app --reload --port 8000
"""
from __future__ import annotations

import os
import uuid

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Load .env BEFORE services is imported, so GROQ_API_KEY is in the
# environment by the time anything reads it.
try:
    from dotenv import load_dotenv
    _here = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(os.path.dirname(_here), ".env"))  # project root
    load_dotenv(os.path.join(_here, ".env"))                    # backend/.env also works
except ImportError:
    pass

import services  # noqa: E402  (must come after load_dotenv)

MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "10")) * 1024 * 1024

app = FastAPI(title="Resume ATS Analyser")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────────────────
# Request models
# ─────────────────────────────────────────────────────────────────────────
class JobDescriptionBody(BaseModel):
    jd_text: str
    use_llm: bool = True   # let the LLM read the JD and split required/preferred


class AtsBody(BaseModel):
    resume: dict           # the profile returned by /api/analyze-resume
    jd_text: str
    resume_text: str = ""  # optional, but improves keyword matching
    use_llm: bool = True   # ask Groq for the final score + summary
    # The analysis already returned by /api/analyze-job. Passing it back
    # avoids paying for a second identical LLM call, and guarantees the
    # score is computed against exactly what the user was shown.
    jd_analysis: dict | None = None


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────
async def _read_upload(file: UploadFile) -> bytes:
    data = await file.read()
    if not data:
        raise HTTPException(400, "The uploaded file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, f"File is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    return data


def _extract_text(data: bytes, filename: str) -> str:
    try:
        return services.extract_pdf_text(data, filename)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Could not read that file: {e}")


# ─────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────
@app.post("/api/analyze-resume")
async def analyze_resume(file: UploadFile = File(...), use_llm: bool = Form(True)):
    """Resume PDF -> candidate profile (name, contact, skills, education,
    experience, projects, certifications).

    With `use_llm`, the PDF is re-read with its formatting preserved (bold
    runs, heading sizes) and handed to the model, which uses those visual
    cues the way a recruiter does — the bold line under EDUCATION is the
    institution, the bold line under PROJECTS is the project name. Anything
    it returns that isn't actually in the resume is rejected; see
    `resume.extraction`.

    `resume_text` comes back too — post it to /api/calculate-ats so
    keyword matching can run against the full text.
    """
    data = await _read_upload(file)
    filename = file.filename or ""
    resume_text = _extract_text(data, filename)

    resume_markup = ""
    if use_llm:
        try:
            resume_markup = services.extract_pdf_markup(data, filename)
        except Exception:
            resume_markup = ""  # fall back to plain text below

    return {
        "filename": file.filename,
        "resume": services.parse_resume_smart(resume_text, resume_markup, use_llm=use_llm),
        "resume_text": resume_text,
    }


@app.post("/api/analyze-job")
def analyze_job(body: JobDescriptionBody):
    """Job description text -> job title, required/preferred skills,
    keywords, minimum years of experience.

    With `use_llm` the JD text is read by the model, which decides what is
    mandatory versus nice-to-have and can name skills outside the built-in
    taxonomy. Anything it returns that is not literally in the JD is
    dropped; see `dropped_unverifiable` in the response.
    """
    if not body.jd_text.strip():
        raise HTTPException(400, "Job description cannot be empty.")
    return services.analyze_job_description(body.jd_text, use_llm=body.use_llm)


@app.post("/api/analyze-job/upload")
async def analyze_job_upload(file: UploadFile = File(...), use_llm: bool = Form(True)):
    """Same as /api/analyze-job, but the JD comes from a PDF/TXT file."""
    data = await _read_upload(file)
    jd_text = _extract_text(data, file.filename or "")
    return {
        "jd_text": jd_text,
        "job_description": services.analyze_job_description(jd_text, use_llm=use_llm),
    }


@app.post("/api/calculate-ats")
def calculate_ats(body: AtsBody):
    """Resume profile + job description -> ATS score, score breakdown,
    matched and missing skills.

    When a GROQ_API_KEY is configured and `use_llm` is true, the whole
    deterministic result is also handed to the LLM, which returns the final
    score and a written summary under `llm_review`. The deterministic score
    is always kept alongside it.
    """
    if not body.resume:
        raise HTTPException(400, "Analyse a resume first.")
    if not body.jd_text.strip():
        raise HTTPException(400, "Provide a job description first.")

    jd_analysis = body.jd_analysis or services.analyze_job_description(
        body.jd_text, use_llm=body.use_llm
    )
    ats = services.calculate_ats_score(body.resume, jd_analysis, body.resume_text)
    review = (
        services.llm_review(body.resume, jd_analysis, ats, body.resume_text, body.jd_text)
        if body.use_llm else None
    )
    return {"job_description": jd_analysis, "ats": ats, "llm_review": review}


@app.post("/api/analyze")
async def analyze_everything(
    file: UploadFile = File(...),
    jd_text: str = Form(...),
    use_llm: bool = Form(True),
):
    """One-shot version of the whole pipeline: upload the PDF and the job
    description together, get the profile, the requirements, the score and
    the LLM review back in a single response."""
    if not jd_text.strip():
        raise HTTPException(400, "Job description cannot be empty.")
    data = await _read_upload(file)
    try:
        return services.analyze(data, file.filename or "", jd_text, use_llm=use_llm)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ─────────────────────────────────────────────────────────────────────────
# Interview
# ─────────────────────────────────────────────────────────────────────────
# Interview sessions are the one piece of server-side state in this app: an
# interview is inherently a conversation, and keeping the plan on the server
# means the browser can't read the questions it hasn't been asked yet.
# In-memory only, so restarting the backend ends any interview in progress.
INTERVIEW_SESSIONS: dict[str, dict] = {}
MAX_SESSIONS = 200


class InterviewStartBody(BaseModel):
    jd_text: str
    resume: dict | None = None      # optional, lets questions lean on their background
    total_questions: int = 8
    jd_analysis: dict | None = None  # reuse the analysis from /api/analyze-job


class InterviewAnswerBody(BaseModel):
    session_id: str
    answer: str = ""


def _get_session(session_id: str) -> dict:
    session = INTERVIEW_SESSIONS.get(session_id)
    if not session:
        raise HTTPException(404, "That interview session no longer exists — please start a new one.")
    return session


@app.post("/api/interview/start")
def interview_start(body: InterviewStartBody):
    """Generate the question plan from the job description and serve the
    first question."""
    if not body.jd_text.strip():
        raise HTTPException(400, "A job description is needed to generate questions.")

    jd_analysis = body.jd_analysis or services.analyze_job_description(body.jd_text, use_llm=True)
    plan = services.build_interview_plan(body.jd_text, jd_analysis, body.resume)
    session = services.new_interview_session(plan, body.total_questions)

    question = services.next_question(session, last_correct=None)
    if not question:
        raise HTTPException(500, "No questions could be generated for this job description.")

    # Simple cap so a long-running server can't grow without bound.
    if len(INTERVIEW_SESSIONS) >= MAX_SESSIONS:
        for stale in list(INTERVIEW_SESSIONS)[: MAX_SESSIONS // 2]:
            INTERVIEW_SESSIONS.pop(stale, None)

    session_id = uuid.uuid4().hex
    INTERVIEW_SESSIONS[session_id] = session
    return {
        "session_id": session_id,
        "job_title": jd_analysis.get("job_title"),
        "topics": [{"topic": t["topic"], "source": t["source"]} for t in plan["topics"]],
        "plan_source": plan.get("plan_source"),
        "plan_error": plan.get("error"),
        "level_points": services.LEVEL_POINTS,
        "current": services.serve_question(session, question),
    }


@app.post("/api/interview/answer")
def interview_answer(body: InterviewAnswerBody):
    """Grade the typed answer, then apply the three progression rules:
    correct and a deeper question exists -> ask it (worth more points);
    otherwise -> a random new topic."""
    session = _get_session(body.session_id)
    if session["finished"]:
        raise HTTPException(400, "This interview is already finished.")
    current = session["current"]
    if not current:
        raise HTTPException(400, "There is no question awaiting an answer.")

    graded = services.grade_answer(
        current["question"], current["expected"], body.answer, current["level"]
    )
    session["points_earned"] += graded["points_awarded"]
    session["transcript"].append({
        "topic": current["topic"],
        "level": current["level"],
        "question": current["question"],
        "answer": (body.answer or "").strip(),
        "correct": graded["correct"],
        "score": graded["score"],
        "points_awarded": graded["points_awarded"],
        "points_possible": graded["points_possible"],
        "feedback": graded["feedback"],
        "missed": graded["missed"],
    })

    question = services.next_question(session, last_correct=graded["correct"])
    if not question:
        session["finished"] = True
        session["current"] = None
        return {"feedback": graded, "done": True, "summary": services.interview_summary(session)}

    return {
        "feedback": graded,
        "done": False,
        "went_deeper": question["is_deeper"],
        "current": services.serve_question(session, question),
        "points_earned": session["points_earned"],
    }


@app.get("/api/interview/{session_id}")
def interview_state(session_id: str):
    """Current progress — useful for debugging a session."""
    session = _get_session(session_id)
    return {
        "finished": session["finished"],
        "asked": len(session["asked"]),
        "total_questions": session["total_questions"],
        "points_earned": session["points_earned"],
        "points_available": session["points_available"],
        "summary": services.interview_summary(session) if session["finished"] else None,
    }


@app.post("/api/interview/{session_id}/end")
def interview_end(session_id: str):
    """Stop early and get the summary for what was answered so far."""
    session = _get_session(session_id)
    session["finished"] = True
    session["current"] = None
    return {"done": True, "summary": services.interview_summary(session)}


@app.get("/api/health")
def health():
    """Also reports whether the AI review is configured, so the frontend
    can show the toggle in the right state."""
    return {
        "ok": True,
        "llm_available": services.llm_available(),
        "llm_model": os.getenv("GROQ_MODEL", services.DEFAULT_LLM_MODEL),
    }
