import React, { useEffect, useRef, useState } from 'react';
import { api } from '../api/client.js';
import { Field, Grid, ListCard, Panel } from '../components/ui.jsx';

// Typed-answer interview. Questions come from the job description; answering
// correctly unlocks a deeper follow-up on the same topic worth more points,
// and anything else moves to a new topic. The backend decides all of that —
// this page just renders whatever question it is handed.
export default function InterviewPage({ jdText, jd, resume, onExit }) {
  const [session, setSession] = useState(null);   // the /start response
  const [current, setCurrent] = useState(null);   // question awaiting an answer
  const [answer, setAnswer] = useState('');
  const [feedback, setFeedback] = useState(null); // grade for the last answer
  const [pending, setPending] = useState(null);   // next question, held until "Next"
  const [points, setPoints] = useState(0);
  const [summary, setSummary] = useState(null);
  const [starting, setStarting] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const answerRef = useRef(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await api.startInterview({ jdText, resume, jdAnalysis: jd, totalQuestions: 8 });
        if (cancelled) return;
        setSession(res);
        setCurrent(res.current);
      } catch (e) {
        if (!cancelled) setError(e.message || 'Could not start the interview.');
      } finally {
        if (!cancelled) setStarting(false);
      }
    })();
    return () => { cancelled = true; };
  }, [jdText, jd, resume]);

  const submitAnswer = async () => {
    if (submitting || !current) return;
    setError('');
    setSubmitting(true);
    try {
      const res = await api.answerInterview({ sessionId: session.session_id, answer });
      setFeedback({ ...res.feedback, went_deeper: res.went_deeper });
      if (res.done) {
        setSummary(res.summary);
        setCurrent(null);
        setPending(null);
      } else {
        setPending(res.current);
        setPoints(res.points_earned);
      }
    } catch (e) {
      setError(e.message || 'Could not submit that answer.');
    } finally {
      setSubmitting(false);
    }
  };

  const nextQuestion = () => {
    setCurrent(pending);
    setPending(null);
    setFeedback(null);
    setAnswer('');
    window.scrollTo(0, 0);
    answerRef.current?.focus();
  };

  const endEarly = async () => {
    try {
      const res = await api.endInterview(session.session_id);
      setSummary(res.summary);
      setCurrent(null);
      setPending(null);
      setFeedback(null);
    } catch (e) {
      setError(e.message || 'Could not end the interview.');
    }
  };

  // ── loading / failure ───────────────────────────────────────────────
  if (starting) {
    return (
      <Shell onExit={onExit}>
        <div className="card" style={{ padding: 40, textAlign: 'center' }}>
          <span className="spinner" />
          <p style={{ marginTop: 14, marginBottom: 0, color: 'var(--text2)' }}>
            Reading the job description and writing your questions…
          </p>
        </div>
      </Shell>
    );
  }
  if (error && !session) {
    return (
      <Shell onExit={onExit}>
        <div className="alert alert-danger">{error}</div>
      </Shell>
    );
  }

  // ── final summary ───────────────────────────────────────────────────
  if (summary) {
    return (
      <Shell onExit={onExit}>
        <InterviewSummary summary={summary} jobTitle={session?.job_title} onExit={onExit} />
      </Shell>
    );
  }

  const progressPct = current ? Math.round(((current.index - 1) / current.total) * 100) : 0;

  return (
    <Shell onExit={onExit}>
      {session?.plan_error && (
        <div className="alert alert-warn" style={{ marginBottom: 18, fontSize: 13 }}>
          {session.plan_error}
        </div>
      )}

      {/* progress header */}
      <div className="card" style={{ padding: '18px 22px', marginBottom: 18 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap', gap: 10, alignItems: 'center' }}>
          <div>
            <span className="field-label">
              {session?.job_title || 'Interview'} · question {current?.index} of {current?.total}
            </span>
            <div style={{ fontSize: 14, color: 'var(--text2)', marginTop: 3 }}>
              <strong>{points}</strong> points so far
            </div>
          </div>
          <button className="btn btn-sm" onClick={endEarly}>End interview</button>
        </div>
        <div style={{ height: 6, background: 'var(--surface)', borderRadius: 4, overflow: 'hidden', marginTop: 12 }}>
          <div style={{ width: `${progressPct}%`, height: '100%', background: 'var(--accent)', transition: 'width .3s' }} />
        </div>
      </div>

      {error && <div className="alert alert-danger" style={{ marginBottom: 18 }}>{error}</div>}

      {/* the question */}
      {current && (
        <div className="card" style={{ padding: 24, marginBottom: 18 }}>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center', marginBottom: 14 }}>
            <span className="badge">{current.topic}</span>
            <span className="badge">
              Level {current.level} of {current.max_level}
            </span>
            <span className={`badge ${current.level > 1 ? 'badge-success' : ''}`}>
              {current.points_possible} points
            </span>
            {current.source === 'general' && <span className="badge">general</span>}
            {current.is_deeper && (
              <span className="badge badge-success">↑ Deeper follow-up — you got the last one right</span>
            )}
          </div>

          <p style={{ fontSize: 17, lineHeight: 1.6, margin: '0 0 18px', color: 'var(--text)' }}>
            {current.question}
          </p>

          <textarea
            ref={answerRef}
            className="input"
            rows={8}
            placeholder="Type your answer…"
            value={answer}
            disabled={!!feedback}
            onChange={(e) => setAnswer(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) submitAnswer();
            }}
          />

          {!feedback && (
            <div style={{ marginTop: 12, display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
              <button className="btn btn-primary" disabled={submitting} onClick={submitAnswer}>
                {submitting ? <><span className="spinner" /> Grading…</> : 'Submit answer'}
              </button>
              <span className="muted" style={{ fontSize: 12.5 }}>
                Ctrl+Enter to submit. Submitting nothing counts as incorrect.
              </span>
            </div>
          )}
        </div>
      )}

      {/* grade for what was just answered */}
      {feedback && (
        <div className="card" style={{ padding: 24, marginBottom: 18 }}>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center', marginBottom: 12 }}>
            <span className={`badge ${feedback.correct ? 'badge-success' : 'badge-danger'}`}>
              {feedback.correct ? '✓ Correct' : '✗ Not correct'}
            </span>
            <span className="badge">{feedback.score} / 10 quality</span>
            <span className="badge">
              {feedback.points_awarded} of {feedback.points_possible} points
            </span>
          </div>

          {feedback.feedback && (
            <p style={{ fontSize: 14, lineHeight: 1.7, color: 'var(--text2)', margin: '0 0 12px' }}>
              {feedback.feedback}
            </p>
          )}

          {feedback.missed?.length > 0 && (
            <>
              <div className="field-label" style={{ marginBottom: 6 }}>Points you did not cover</div>
              <ul style={{ margin: '0 0 12px', paddingLeft: 18, fontSize: 13, color: 'var(--text2)', lineHeight: 1.7 }}>
                {feedback.missed.map((m, i) => <li key={i}>{m}</li>)}
              </ul>
            </>
          )}

          {pending && (
            <button className="btn btn-primary" onClick={nextQuestion}>
              {feedback.went_deeper
                ? `Next: deeper question on ${pending.topic} (${pending.points_possible} pts) →`
                : `Next question: ${pending.topic} →`}
            </button>
          )}
        </div>
      )}

      {/* what the interview will cover */}
      {session?.topics?.length > 0 && (
        <Panel title="Topics in this interview">
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 7 }}>
            {session.topics.map((t) => (
              <span key={t.topic} className="pill">
                {t.topic}{t.source === 'general' ? ' · general' : ''}
              </span>
            ))}
          </div>
          <p className="muted" style={{ fontSize: 12, marginTop: 12, marginBottom: 0 }}>
            Answer correctly and the next question digs deeper into the same topic and is
            worth more ({Object.values(session.level_points || {}).join(' → ')} points by
            level). Otherwise the interview moves to a new topic.
          </p>
        </Panel>
      )}
    </Shell>
  );
}

function Shell({ children, onExit }) {
  return (
    <div className="page-gradient">
      <div className="page-narrow">
        <button className="btn btn-sm" onClick={onExit} style={{ marginBottom: 20 }}>
          ← Back to ATS result
        </button>
        <h1 style={{ fontSize: 32, margin: '0 0 20px', letterSpacing: '-0.03em' }}>
          Mock <span className="grad-text">Interview</span>
        </h1>
        {children}
      </div>
    </div>
  );
}

function InterviewSummary({ summary, jobTitle, onExit }) {
  const pct = summary.percentage;
  const strong = pct >= 60;
  return (
    <>
      <div className="card" style={{ padding: 24, marginBottom: 18 }}>
        <div style={{ display: 'flex', gap: 26, alignItems: 'center', flexWrap: 'wrap' }}>
          <div className="score-ring" style={{ '--pct': pct, '--ring-color': strong ? 'var(--success)' : 'var(--danger)' }}>
            <div className="score-ring__inner">
              <div className="score-ring__num">{pct}%</div>
              <div className="score-ring__label">Interview</div>
            </div>
          </div>
          <div style={{ flex: 1, minWidth: 240 }}>
            <h3 style={{ fontSize: 18, margin: '0 0 12px' }}>{jobTitle || 'Interview'} — result</h3>
            <Grid>
              <Field label="Points" value={`${summary.points_earned} / ${summary.points_available}`} />
              <Field label="Correct" value={`${summary.correct_answers} of ${summary.questions_answered}`} />
              <Field label="Deepest level reached" value={`${summary.deepest_level_reached} of 3`} />
              <Field label="Topics covered" value={summary.topics_covered.length} />
            </Grid>
            <p className="muted" style={{ fontSize: 12.5, marginTop: 12, marginBottom: 0 }}>
              Points available grows as you unlock deeper questions, so reaching level 3
              raises both what you can earn and what you are measured against.
            </p>
          </div>
        </div>
      </div>

      <div className="card" style={{ padding: 24, marginBottom: 18 }}>
        <h3 style={{ fontSize: 17, margin: '0 0 14px' }}>Transcript</h3>
        {summary.transcript.map((t, i) => (
          <div
            key={i}
            style={{
              borderLeft: `3px solid ${t.correct ? 'var(--success)' : 'var(--danger)'}`,
              background: 'var(--surface)', borderRadius: 'var(--radius)',
              padding: '14px 16px', marginBottom: 12,
            }}
          >
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 8 }}>
              <span className="badge">{t.topic}</span>
              <span className="badge">Level {t.level}</span>
              <span className={`badge ${t.correct ? 'badge-success' : 'badge-danger'}`}>
                {t.points_awarded} / {t.points_possible} pts
              </span>
            </div>
            <p style={{ fontSize: 14, margin: '0 0 8px', color: 'var(--text)' }}><strong>Q:</strong> {t.question}</p>
            <p style={{ fontSize: 13, margin: '0 0 8px', color: 'var(--text2)', whiteSpace: 'pre-wrap' }}>
              <strong>Your answer:</strong> {t.answer || '(no answer given)'}
            </p>
            {t.feedback && (
              <p style={{ fontSize: 13, margin: 0, color: 'var(--text2)', fontStyle: 'italic' }}>{t.feedback}</p>
            )}
            {t.missed?.length > 0 && (
              <ListCard title="Not covered" items={t.missed} tone="warn" />
            )}
          </div>
        ))}
      </div>

      <button className="btn btn-primary" onClick={onExit} style={{ marginBottom: 40 }}>
        ← Back to ATS result
      </button>
    </>
  );
}
