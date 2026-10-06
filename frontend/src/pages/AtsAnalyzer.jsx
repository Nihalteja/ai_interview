import React, { useEffect, useState } from 'react';
import { api } from '../api/client.js';
import ResultsPage from './ResultsPage.jsx';
import InterviewPage from './InterviewPage.jsx';
import { CheckToggle, JobRequirements, ResumeProfile, Section } from '../components/ui.jsx';

// Two views, one component. The input view collects the resume and the job
// description; "Check ATS Score" scores them and switches to ResultsPage.
// All state lives here so going back never loses anything.
export default function AtsAnalyzer() {
  const [view, setView] = useState('input');       // 'input' | 'results' | 'interview'

  const [resumeFile, setResumeFile] = useState(null);
  const [resume, setResume] = useState(null);       // candidate profile
  const [resumeText, setResumeText] = useState(''); // raw text, for keyword matching
  const [resumeName, setResumeName] = useState('');

  const [jdText, setJdText] = useState('');
  const [jd, setJd] = useState(null);               // JD analysis
  const [ats, setAts] = useState(null);             // deterministic score
  const [llm, setLlm] = useState(null);             // AI review of that score

  // Detail blocks start collapsed — the "Check" buttons open them.
  const [showResume, setShowResume] = useState(false);
  const [showJd, setShowJd] = useState(false);

  const [uploading, setUploading] = useState(false);
  const [analyzingJd, setAnalyzingJd] = useState(false);
  const [scoring, setScoring] = useState(false);
  const [error, setError] = useState('');

  // Is a GROQ_API_KEY configured on the backend?
  const [llmInfo, setLlmInfo] = useState({ llm_available: false, llm_model: '' });
  const [useLlm, setUseLlm] = useState(true);

  useEffect(() => {
    api.health().then(setLlmInfo).catch(() => {});
  }, []);

  // Any change upstream invalidates the score below it.
  const clearScore = () => { setAts(null); setLlm(null); };

  const handleResumeUpload = async () => {
    if (!resumeFile) return;
    setError('');
    setUploading(true);
    try {
      const res = await api.analyzeResume(resumeFile);
      setResume(res.resume);
      setResumeText(res.resume_text);
      setResumeName(res.filename);
      setShowResume(false);
      clearScore();
    } catch (e) {
      setError(e.message || 'Resume upload failed.');
    } finally {
      setUploading(false);
    }
  };

  const handleAnalyzeJd = async () => {
    if (!jdText.trim()) return;
    setError('');
    setAnalyzingJd(true);
    try {
      setJd(await api.analyzeJob(jdText));
      setShowJd(false);
      clearScore();
    } catch (e) {
      setError(e.message || 'Could not analyse the job description.');
    } finally {
      setAnalyzingJd(false);
    }
  };

  const handleScore = async () => {
    setError('');
    setScoring(true);
    try {
      const res = await api.calculateAts({ resume, resumeText, jdText, useLlm, jdAnalysis: jd });
      setJd(res.job_description);
      setAts(res.ats);
      setLlm(res.llm_review);
      setView('results');
      window.scrollTo(0, 0);
    } catch (e) {
      setError(e.message || 'ATS scoring failed.');
    } finally {
      setScoring(false);
    }
  };

  const goTo = (next) => { setView(next); window.scrollTo(0, 0); };

  if (view === 'interview') {
    return (
      <InterviewPage
        jdText={jdText}
        jd={jd}
        resume={resume}
        onExit={() => goTo(ats ? 'results' : 'input')}
      />
    );
  }

  if (view === 'results' && ats) {
    return (
      <ResultsPage
        ats={ats}
        resume={resume}
        jd={jd}
        llm={llm}
        onBack={() => goTo('input')}
        onStartInterview={() => goTo('interview')}
      />
    );
  }

  const resumeReady = !!resume;
  const jdReady = !!jdText.trim();
  const aiOn = useLlm && llmInfo.llm_available;

  return (
    <div className="page-gradient">
      <div className="page-narrow">
        <h1 style={{ fontSize: 38, margin: '10px 0', letterSpacing: '-0.03em' }}>
          Resume <span className="grad-text">ATS Analyser</span>
        </h1>
        <p style={{ color: 'var(--muted)', marginBottom: 30, fontSize: 15.5, lineHeight: 1.6, maxWidth: 620 }}>
          Upload a resume, paste a job description, and get a transparent ATS
          match score with the exact skills you match and miss.
        </p>

        {error && <div className="alert alert-danger" style={{ marginBottom: 20 }}>{error}</div>}

        {/* ── 1 · Resume ─────────────────────────────────────────────── */}
        <Section step="1" title="Resume" done={resumeReady}>
          <div className="flex" style={{ gap: 10, flexWrap: 'wrap', alignItems: 'center' }}>
            <label htmlFor="resume-input" className="btn btn-sm" style={{ cursor: 'pointer' }}>
              📄 Choose PDF
              <input
                id="resume-input"
                type="file"
                accept=".pdf,.txt"
                style={{ display: 'none' }}
                onChange={(e) => setResumeFile(e.target.files?.[0] || null)}
              />
            </label>
            <span className="muted" style={{ fontSize: 13, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {resumeFile ? resumeFile.name : resumeName ? `${resumeName} (analysed)` : 'no file selected'}
            </span>
            {resumeFile && (
              <button className="btn btn-primary btn-sm" disabled={uploading} onClick={handleResumeUpload} style={{ marginLeft: 'auto' }}>
                {uploading ? <><span className="spinner" /> Reading…</> : 'Upload & Extract'}
              </button>
            )}
          </div>

          {resume && (
            <>
              <div className="alert alert-success" style={{ marginTop: 14, display: 'flex', gap: 8 }}>
                <span>✓</span>
                <span>
                  Resume parsed — <strong>{resume.skills?.length || 0} skills</strong>,{' '}
                  {resume.experience?.length || 0} role(s), {resume.projects?.length || 0} project(s) found
                </span>
              </div>
              <CheckToggle open={showResume} onToggle={() => setShowResume(!showResume)} />
              {showResume && <ResumeProfile resume={resume} />}
            </>
          )}
        </Section>

        {/* ── 2 · Job description ────────────────────────────────────── */}
        <Section step="2" title="Job Description" done={jdReady}>
          <textarea
            className="input"
            rows={7}
            placeholder="Paste the job description here…"
            value={jdText}
            onChange={(e) => { setJdText(e.target.value); setJd(null); setShowJd(false); clearScore(); }}
          />
          <div style={{ marginTop: 12, display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
            <button className="btn" onClick={handleAnalyzeJd} disabled={!jdReady || analyzingJd}>
              {analyzingJd ? <><span className="spinner" /> Analysing…</> : 'Analyse JD'}
            </button>
            <span className="muted" style={{ fontSize: 13 }}>or upload a file:</span>
            <label className="btn btn-sm" style={{ cursor: 'pointer' }}>
              📎 Choose file
              <input
                type="file"
                accept=".pdf,.txt"
                style={{ display: 'none' }}
                onChange={async (e) => {
                  const f = e.target.files?.[0];
                  if (!f) return;
                  setError('');
                  try {
                    const res = await api.uploadJob(f);
                    setJdText(res.jd_text);
                    setJd(res.job_description);
                    setShowJd(false);
                    clearScore();
                  } catch (err) {
                    setError(err.message || 'Could not read that file.');
                  }
                }}
              />
            </label>
          </div>

          {jd && (
            <>
              <div className="alert alert-success" style={{ marginTop: 14, display: 'flex', gap: 8 }}>
                <span>✓</span>
                <span>
                  Analysed <strong>{jd.job_title || 'this role'}</strong> —{' '}
                  {jd.required_skills?.length || 0} required, {jd.preferred_skills?.length || 0} preferred,{' '}
                  {jd.keywords?.length || 0} keyword(s)
                </span>
              </div>
              <CheckToggle open={showJd} onToggle={() => setShowJd(!showJd)} />
              {showJd && <JobRequirements jd={jd} />}
            </>
          )}
        </Section>

        {/* ── 3 · Score ──────────────────────────────────────────────── */}
        <Section step="3" title="ATS Match Score" done={!!ats}>
          <div className="flex" style={{ gap: 14, flexWrap: 'wrap', alignItems: 'center' }}>
            <button className="btn btn-primary btn-lg" disabled={!resumeReady || !jdReady || scoring} onClick={handleScore}>
              {scoring
                ? <><span className="spinner" /> {aiOn ? 'Scoring & AI review…' : 'Scoring…'}</>
                : 'Check ATS Score →'}
            </button>
            <label
              className="muted"
              style={{ fontSize: 13, display: 'flex', alignItems: 'center', gap: 7, cursor: llmInfo.llm_available ? 'pointer' : 'not-allowed' }}
              title={llmInfo.llm_available
                ? `The deterministic result is reviewed by ${llmInfo.llm_model}, which decides the final score.`
                : 'Add GROQ_API_KEY to the .env file and restart the backend to enable this.'}
            >
              <input
                type="checkbox"
                checked={aiOn}
                disabled={!llmInfo.llm_available}
                onChange={(e) => { setUseLlm(e.target.checked); clearScore(); }}
              />
              AI review {llmInfo.llm_available ? `(${llmInfo.llm_model})` : '— set GROQ_API_KEY in .env'}
            </label>
          </div>
          <p className="muted" style={{ fontSize: 12.5, marginTop: 10, marginBottom: 0 }}>
            {!resumeReady || !jdReady
              ? `Add ${!resumeReady && !jdReady ? 'a resume and a job description' : !resumeReady ? 'a resume' : 'a job description'} first.`
              : 'Opens the full result page: score, breakdown, matched and missing skills.'}
          </p>
        </Section>
      </div>
    </div>
  );
}
