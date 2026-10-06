// Thin fetch wrapper around the FastAPI backend (backend/main.py).
// The backend is stateless, so there is nothing to log in to and no
// session to keep — every call carries everything it needs.

// Empty by default: vite.config.js proxies /api to the backend in dev.
const BASE_URL = import.meta?.env?.VITE_API_BASE || '';

async function request(path, { method = 'GET', body, isForm = false } = {}) {
  const headers = {};
  if (!isForm && body !== undefined) headers['Content-Type'] = 'application/json';

  const res = await fetch(`${BASE_URL}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : isForm ? body : JSON.stringify(body),
  });

  if (!res.ok) {
    let message = `Request failed (${res.status})`;
    try {
      const data = await res.json();
      message = data.detail || message;
    } catch {
      // response wasn't JSON — keep the default message
    }
    throw new Error(message);
  }
  return res.json();
}

export const api = {
  // { ok, llm_available, llm_model }
  health: () => request('/api/health'),

  // PDF -> { filename, resume, resume_text }
  analyzeResume: (file) => {
    const form = new FormData();
    form.append('file', file);
    return request('/api/analyze-resume', { method: 'POST', body: form, isForm: true });
  },

  // JD text -> { job_title, required_skills, preferred_skills, keywords, ... }
  analyzeJob: (jdText) =>
    request('/api/analyze-job', { method: 'POST', body: { jd_text: jdText } }),

  // JD file -> { jd_text, job_description }
  uploadJob: (file) => {
    const form = new FormData();
    form.append('file', file);
    return request('/api/analyze-job/upload', { method: 'POST', body: form, isForm: true });
  },

  // resume profile + JD -> { job_description, ats, llm_review }
  // jdAnalysis is passed back so the backend doesn't re-run the JD through
  // the LLM, and the score matches exactly what was displayed.
  calculateAts: ({ resume, resumeText, jdText, useLlm = true, jdAnalysis = null }) =>
    request('/api/calculate-ats', {
      method: 'POST',
      body: {
        resume, resume_text: resumeText, jd_text: jdText,
        use_llm: useLlm, jd_analysis: jdAnalysis,
      },
    }),

  // ── interview ──────────────────────────────────────────────────────
  startInterview: ({ jdText, resume = null, jdAnalysis = null, totalQuestions = 8 }) =>
    request('/api/interview/start', {
      method: 'POST',
      body: {
        jd_text: jdText, resume, jd_analysis: jdAnalysis,
        total_questions: totalQuestions,
      },
    }),

  answerInterview: ({ sessionId, answer }) =>
    request('/api/interview/answer', {
      method: 'POST',
      body: { session_id: sessionId, answer },
    }),

  endInterview: (sessionId) =>
    request(`/api/interview/${sessionId}/end`, { method: 'POST' }),
};
