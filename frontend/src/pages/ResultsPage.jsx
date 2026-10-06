import React from 'react';
import {
  Bar, COMPONENT_LABELS, Field, Grid, JobRequirements, ListCard, Panel, ResumeProfile,
} from '../components/ui.jsx';

// The score page. Reached by clicking "Check ATS Score" on the input page;
// `onBack` returns there with all inputs still in place.
export default function ResultsPage({ ats, resume, jd, llm, onBack, onStartInterview }) {
  // When the AI review ran, its score is the final one. The engine score
  // stays on screen next to it so both are always visible.
  const aiUsed = !!llm?.used;
  const finalScore = aiUsed ? llm.final_ats_score : ats.ats_score;
  const finalVerdict = aiUsed ? (llm.verdict || ats.verdict) : ats.verdict;
  const strong = finalScore >= 60;

  return (
    <div className="page-gradient">
      <div className="page-narrow">
        <button className="btn btn-sm" onClick={onBack} style={{ marginBottom: 20 }}>
          ← Back to resume &amp; job description
        </button>

        <h1 style={{ fontSize: 32, margin: '0 0 6px', letterSpacing: '-0.03em' }}>
          ATS <span className="grad-text">Result</span>
        </h1>
        <p className="muted" style={{ fontSize: 14, marginBottom: 26 }}>
          {resume?.name ? `${resume.name} · ` : ''}{jd?.job_title || 'this role'}
        </p>

        {/* ── score + component bars ─────────────────────────────────── */}
        <div className="card" style={{ padding: 24, marginBottom: 22 }}>
          <div style={{ display: 'flex', gap: 26, alignItems: 'center', flexWrap: 'wrap' }}>
            <div
              className="score-ring"
              style={{ '--pct': finalScore, '--ring-color': strong ? 'var(--success)' : 'var(--danger)' }}
            >
              <div className="score-ring__inner">
                <div className="score-ring__num">{finalScore}%</div>
                <div className="score-ring__label">{aiUsed ? 'Final Score' : 'ATS Match'}</div>
              </div>
            </div>
            <div style={{ flex: 1, minWidth: 240 }}>
              <span className={`badge ${strong ? 'badge-success' : 'badge-danger'}`}>{finalVerdict}</span>
              {aiUsed && (
                <span className="badge" style={{ marginLeft: 8 }}>
                  AI reviewed · engine said {llm.deterministic_ats_score}%
                  {llm.adjustment !== 0 && ` (${llm.adjustment > 0 ? '+' : ''}${llm.adjustment})`}
                </span>
              )}
              <p style={{ margin: '12px 0 0', fontSize: 14, color: 'var(--text2)', lineHeight: 1.6 }}>
                {aiUsed && llm.summary ? llm.summary : ats.summary}
              </p>
              <div style={{ marginTop: 16 }}>
                {Object.entries(ats.breakdown).map(([key, c]) => (
                  <Bar key={key} label={COMPONENT_LABELS[key] || key} component={c} weight={ats.weights[key]} />
                ))}
              </div>
            </div>
          </div>
        </div>

        {/* ── next step: the mock interview ──────────────────────────── */}
        {onStartInterview && (
          <div className="card" style={{ padding: 24, marginBottom: 22 }}>
            <h3 style={{ fontSize: 17, margin: '0 0 8px' }}>Mock interview</h3>
            <p style={{ fontSize: 13.5, color: 'var(--text2)', lineHeight: 1.7, margin: '0 0 14px' }}>
              Questions written from this job description, plus general engineering
              questions. Answer one correctly and the next digs deeper into the same
              topic and is worth more points; otherwise you move to a new topic.
            </p>
            <button className="btn btn-primary btn-lg" onClick={onStartInterview}>
              Start Interview →
            </button>
          </div>
        )}

        {llm && <LlmReview llm={llm} />}

        {/* ── the engine's arithmetic, spelled out ───────────────────── */}
        <div className="card" style={{ padding: 24, marginBottom: 22 }}>
          <h3 style={{ fontSize: 17, margin: '0 0 14px' }}>How the engine reached {ats.ats_score}%</h3>
          <ol style={{ margin: 0, paddingLeft: 20, fontSize: 13.5, color: 'var(--text2)', lineHeight: 1.75 }}>
            {ats.explanation.map((line, i) => <li key={i} style={{ marginBottom: 6 }}>{line}</li>)}
          </ol>
          <p className="muted" style={{ fontSize: 12, marginTop: 12, marginBottom: 0 }}>
            Weights: required skills {ats.weights.required_skills}% · preferred skills{' '}
            {ats.weights.preferred_skills}% · experience {ats.weights.experience}% · keywords{' '}
            {ats.weights.keywords}%. A component the job description never states is dropped and its
            weight is shared across the rest.
          </p>
        </div>

        {/* ── skill-by-skill comparison ──────────────────────────────── */}
        <div className="card" style={{ padding: 24, marginBottom: 22 }}>
          <h3 style={{ fontSize: 17, margin: '0 0 14px' }}>Skill match</h3>
          <Grid>
            <ListCard title={`Your resume skills (${resume?.skills?.length || 0})`} items={resume?.skills} tone="accent" />
            <ListCard title={`Required by the JD (${jd?.required_skills?.length || 0})`} items={jd?.required_skills} tone="danger" />
            <ListCard title={`Preferred by the JD (${jd?.preferred_skills?.length || 0})`} items={jd?.preferred_skills} tone="warn" />

            <ListCard title={`Matched required (${ats.matched_required_skills.length})`} items={ats.matched_required_skills} tone="success" />
            <ListCard title={`Missing required (${ats.missing_required_skills.length})`} items={ats.missing_required_skills} tone="danger" />
            <ListCard title={`Matched preferred (${ats.matched_preferred_skills.length})`} items={ats.matched_preferred_skills} tone="success" />
            <ListCard title={`Missing preferred (${ats.missing_preferred_skills.length})`} items={ats.missing_preferred_skills} tone="warn" />

            <ListCard title={`Matched keywords (${ats.matched_keywords.length})`} items={ats.matched_keywords} tone="success" />
            <ListCard title={`Missing keywords (${ats.missing_keywords.length})`} items={ats.missing_keywords} tone="warn" />
            <ListCard title="How to improve" items={ats.suggestions} tone="accent" />
          </Grid>

          {ats.match_detail?.length > 0 && (
            <Panel title="How each matched skill was credited">
              <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, color: 'var(--text2)', lineHeight: 1.7 }}>
                {ats.match_detail.map((d, i) => (
                  <li key={i}>
                    <strong>{d.skill}</strong> — {d.method === 'implied'
                      ? `credited indirectly, from “${d.via}” on your resume`
                      : 'found directly in your resume'}
                  </li>
                ))}
              </ul>
            </Panel>
          )}
        </div>

        {/* ── the two inputs, in full ────────────────────────────────── */}
        <div className="card" style={{ padding: 24, marginBottom: 22 }}>
          <h3 style={{ fontSize: 17, margin: 0 }}>Extracted resume</h3>
          {/* ResumeProfile renders the generated profile_summary itself, so
              only the candidate's own wording is added here. */}
          {resume?.summary && (
            <>
              <div className="field-label" style={{ marginTop: 14, marginBottom: 5 }}>
                Summary written on the resume
              </div>
              <p style={{ margin: 0, fontSize: 13.5, color: 'var(--text2)', lineHeight: 1.7, fontStyle: 'italic' }}>
                “{resume.summary}”
              </p>
            </>
          )}
          {resume && <ResumeProfile resume={resume} />}
        </div>

        <div className="card" style={{ padding: 24, marginBottom: 40 }}>
          <h3 style={{ fontSize: 17, margin: 0 }}>Job description analysis</h3>
          {jd && <JobRequirements jd={jd} />}
        </div>
      </div>
    </div>
  );
}

/* ── the AI's verdict on the engine's result ───────────────────────── */
function LlmReview({ llm }) {
  if (!llm.used) {
    return (
      <div className="alert alert-warn" style={{ marginBottom: 22, fontSize: 13 }}>
        <strong>AI review unavailable</strong> — {llm.error} The deterministic
        score above stands as the final score.
      </div>
    );
  }
  return (
    <div className="card" style={{ padding: 24, marginBottom: 22 }}>
      <h3 style={{ fontSize: 17, margin: '0 0 14px' }}>
        AI review — final score {llm.final_ats_score}%
      </h3>

      <div style={{ display: 'flex', gap: 22, flexWrap: 'wrap', marginBottom: 14 }}>
        <Field label="Engine score" value={`${llm.deterministic_ats_score}%`} />
        <Field label="AI final score" value={`${llm.final_ats_score}%`} />
        <Field
          label="Adjustment"
          value={llm.adjustment === 0 ? 'none' : `${llm.adjustment > 0 ? '+' : ''}${llm.adjustment} pts`}
        />
        <Field label="Model" value={llm.model} />
      </div>

      {llm.clamped && (
        <div className="alert alert-warn" style={{ fontSize: 12.5, marginBottom: 12 }}>
          The model proposed {llm.proposed_ats_score}%, which is more than
          ±{llm.max_adjustment} points from the engine score, so it was capped
          at {llm.final_ats_score}%.
        </div>
      )}

      {llm.score_reasoning && (
        <>
          <div className="field-label" style={{ marginBottom: 5 }}>Why the AI set this score</div>
          <p style={{ margin: '0 0 14px', fontSize: 13.5, color: 'var(--text2)', lineHeight: 1.7 }}>
            {llm.score_reasoning}
          </p>
        </>
      )}

      {llm.resume_summary && (
        <>
          <div className="field-label" style={{ marginBottom: 5 }}>AI summary of the resume</div>
          <p style={{ margin: '0 0 14px', fontSize: 13.5, color: 'var(--text2)', lineHeight: 1.7 }}>
            {llm.resume_summary}
          </p>
        </>
      )}

      <Grid>
        <ListCard title="Strengths" items={llm.strengths} tone="success" />
        <ListCard title="Weaknesses" items={llm.weaknesses} tone="danger" />
        <ListCard title="AI recommendations" items={llm.recommendations} tone="accent" />
      </Grid>
    </div>
  );
}
