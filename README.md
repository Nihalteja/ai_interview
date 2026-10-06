# Resume ATS Analyser

Upload a resume PDF, paste a job description, get a transparent ATS match
score. The extraction and scoring engine is fully deterministic — the same
resume and the same JD always produce the same score, and every extracted
skill traces back to a literal match in the uploaded text.

On top of that engine sits an **optional AI review layer** (Groq): the whole
deterministic result is handed to an LLM, which returns the final score and a
written summary. Without a key the app still works end to end — the
deterministic score is simply final. No database, no login.

## Structure

```
backend/
  main.py            FastAPI app — endpoints + request models only
  services.py        the entire pipeline: PDF text, resume parsing, skill
                     extraction/normalisation, JD analysis, matching,
                     scoring, and the optional Groq review layer
  requirements.txt

frontend/
  src/App.jsx                  renders the analyser
  src/pages/AtsAnalyzer.jsx    inputs + which view is showing
  src/pages/ResultsPage.jsx    the score page
  src/pages/InterviewPage.jsx  the mock interview
  src/components/ui.jsx        shared presentational bits
  src/api/client.js            fetch wrapper
  src/styles/global.css        design system
  package.json / vite.config.js / index.html / src/main.jsx

.env                 your Groq key goes here (gitignored)
.env.example         template
```

## Flow

```
Resume PDF → extract text → candidate profile → normalised skills
                                                        ↓
Job description → job title, required skills, preferred skills, keywords
                                                        ↓
                          compare → ATS score + breakdown → display
```

## ATS scoring

Fixed weights, computed in plain Python (`services.WEIGHTS`):

| Component | Weight |
|---|---|
| Required skills | 60% |
| Preferred skills | 20% |
| Experience | 10% |
| Keyword match | 10% |

Each component yields a ratio in `[0, 1]`. If the job description never
states a requirement (no preferred skills, no minimum years, no keywords)
that component is marked `applicable: false` and its weight is dropped from
the total, so the rest rescale to 100 rather than the score being silently
capped. `breakdown` returns the earned points, the max, the ratio and a
one-line explanation per component.

The score is never a bare number. Every result also carries:

- `summary` — one sentence: score, verdict, role, skills matched
- `explanation` — the arithmetic in plain English, one line per component
  plus the running total, so the user can see exactly why it is X%
- `match_detail` — how each matched skill was credited (`exact` or
  `implied`, and via which resume skill)
- `resume.profile_summary` — a generated summary of the candidate, so
  there is one even when the resume has no Summary section (the resume's
  own wording, if present, stays in `resume.summary`)

A required skill matches in one of two auditable ways:

- **exact** — the skill, in any spelling from `SKILL_TAXONOMY`, appears in
  the resume (`ML` → `Machine Learning`, `Python Programming` → `Python`)
- **implied** — a skill the candidate has stands in for it, via the
  hand-written `SKILL_IMPLIES` table (MySQL → SQL, FastAPI → Python)

## AI review layer (optional)

Put your Groq key in `.env` and restart the backend:

```
GROQ_API_KEY=gsk_your_key_here
GROQ_MODEL=openai/gpt-oss-120b
MAX_LLM_ADJUSTMENT=15
```

Model availability varies by key — list yours with
`GET https://api.groq.com/openai/v1/models`. `openai/gpt-oss-120b` (~3s) and
`qwen/qwen3.8-27b` (~2s) are both verified working; `groq/compound` rejects
the payload as too large.

The model receives everything the engine produced — the score, the full
breakdown, matched/missing required and preferred skills, the candidate
profile, plus the raw resume and JD text — and returns the **final score**,
a verdict, a summary, a resume summary, strengths, weaknesses and
recommendations, all under `llm_review` in the response.

Two rules keep it honest:

1. It never computes the score from scratch. It sees the deterministic score
   and may only **adjust** it, and it must explain any change.
2. That adjustment is clamped to ±`MAX_LLM_ADJUSTMENT` points. A bad model
   response can nudge the number but never invent one. If the model proposes
   something outside the band, the response reports `clamped: true` along
   with the raw `proposed_ats_score`.

The engine score stays visible next to the AI score in the UI, always.

If the key is missing, the request fails, or the reply isn't valid JSON,
`llm_review` comes back with `used: false` and an `error`, `final_ats_score`
falls back to the deterministic score, and the app carries on. `GET
/api/health` reports `llm_available` so the UI can show the toggle correctly.

## AI resume extraction (format-aware)

Plain PDF text extraction throws away the very signals that tell a reader
which line is the college and which is the degree. So with `use_llm`, the PDF
is re-read via `extract_pdf_markup`, which walks PyMuPDF's span tree and
re-emits the layout as light markdown — `**bold**` for bold/semibold runs,
`## Heading` for lines set noticeably larger than the document's body size
(measured per-document, not hard-coded). The model then reads those cues the
way a recruiter does.

Everything it returns is checked back against the resume: an entity that
doesn't appear is flagged `verified: false`, and an unverifiable **skill** is
dropped outright, since skills drive the ATS score. Each entity also carries
a `confidence`.

Measured on a resume with real bold runs:

| | regex parser | format-aware AI |
|---|---|---|
| Institution | *missing* — captured only the degree | `Gokaraju Rangaraju Institute of Engineering and Technology` |
| Projects | **6** — description sentences became project names | **2** — the actual bold titles, with their tech stacks |
| Experience | title was `"Acme Corp, Hyderabad"` | role / company / location split correctly |
| Certifications | 2 entries (title and issuer line separately) | 1, with issuer and date |

The regex result is the floor: skills found by either parser are kept, so
turning the AI on never loses information. If the call fails, the regex
profile is returned unchanged with the reason in `resume.extraction.error`.

## AI job-description extraction

With `use_llm`, the JD text itself goes to the model, which decides what is
mandatory versus nice-to-have and can name skills outside `SKILL_TAXONOMY`.
The deterministic pass still runs first and is handed over as a hint, so a
failed call just falls back to it (`analysis_source: "deterministic"`).

The guardrail: every skill and keyword the model returns must literally
appear in the JD, or it is dropped and listed in `dropped_unverifiable`. The
model cannot add a requirement the job description never stated.

This matters — on a real JD the regex pass put Kubernetes and Terraform in
**required** because "It would be great if you also brought" isn't a
recognised preferred cue, while the model correctly marked them preferred and
also picked up Airflow, Snowflake, dbt and Kafka, none of which are in the
taxonomy.

**Preferred skills are not mandatory.** They are the extras that raise a
candidate's standing when present. In scoring they hold 20% of the weight
(`services.WEIGHTS`), and if a JD names none, that weight is dropped and
redistributed rather than counted against the candidate.

## Interviewer

`POST /api/interview/start` sends the job description to the model once, and
gets back a plan: 6-8 topics (JD-derived plus 2 general engineering ones),
each holding a chain of three questions that get progressively deeper. After
that, traversal is plain Python — the rules are exact and cost nothing:

| Outcome | Next question |
|---|---|
| Correct, and a deeper question exists | that deeper question, **worth more points** |
| Correct, already at level 3 | a random new topic at level 1 |
| Incorrect | a random new topic at level 1 |

Points by depth: **level 1 = 10, level 2 = 20, level 3 = 30**
(`services.LEVEL_POINTS`). Points awarded scale with answer quality
(`points_possible × score/10`), and a wrong answer scores 0. So the way to
score well is to keep going deeper on one topic.

Answers are typed. Only grading costs an LLM call — one per answer. An empty
answer is marked incorrect without a call. If grading fails, the question is
skipped unscored rather than ending the interview.

`points_available` in the summary is the sum of what was actually asked, so
it grows as deeper questions are unlocked.

Sessions live in memory on the server (`INTERVIEW_SESSIONS` in `main.py`) —
that keeps the unasked questions out of the browser, but it also means
restarting the backend ends any interview in progress.

## API

| Endpoint | Input | Output |
|---|---|---|
| `POST /api/analyze-resume` | PDF (multipart `file`) | `{filename, resume, resume_text}` |
| `POST /api/analyze-job` | `{jd_text}` | job title, required/preferred skills, keywords, min years |
| `POST /api/analyze-job/upload` | PDF/TXT (multipart `file`) | `{jd_text, job_description}` |
| `POST /api/calculate-ats` | `{resume, resume_text, jd_text, use_llm}` | `{job_description, ats, llm_review}` |
| `POST /api/analyze` | PDF + `jd_text` + `use_llm` (multipart) | everything in one call |
| `GET /api/health` | — | `{ok, llm_available, llm_model}` |
| `POST /api/interview/start` | `{jd_text, resume?, jd_analysis?, total_questions}` | `{session_id, topics, level_points, current}` |
| `POST /api/interview/answer` | `{session_id, answer}` | `{feedback, done, went_deeper, current}` or `{feedback, done: true, summary}` |
| `GET /api/interview/{id}` | — | progress |
| `POST /api/interview/{id}/end` | — | `{done, summary}` — stop early |

The backend is stateless: the frontend holds the resume profile it got from
`/api/analyze-resume` and posts it back to `/api/calculate-ats`.

## Run it

**Backend** (port 8000):
```bash
cd backend
python -m venv venv
venv\Scripts\activate          # Windows;  source venv/bin/activate on macOS/Linux
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

**Frontend** (port 5173):
```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173.

## Extending it

- **More skills** — add a row to `SKILL_TAXONOMY` in `services.py`. Resume
  extraction, JD analysis and matching all read that one table, so nothing
  else needs to change.
- **New required/preferred cues** — `PREFERRED_CUE_RE` / `REQUIRED_CUE_RE`.
- **Different weights** — `services.WEIGHTS`.
- **AI interview / avatar modules** — not built yet. Add them as new
  endpoints in `main.py` plus one new module; the ATS pipeline above stays
  untouched.
