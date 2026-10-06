import React from 'react';

// Small presentational bits shared by the input page and the results page.
// Nothing in here holds state or talks to the API.

export const COMPONENT_LABELS = {
  required_skills: 'Required skills',
  preferred_skills: 'Preferred skills',
  experience: 'Experience',
  keywords: 'Keywords',
};

const TONES = {
  success: 'var(--success)',
  danger: 'var(--danger)',
  warn: 'var(--warn)',
  accent: 'var(--accent)',
};

/** Numbered step card used on the input page. */
export function Section({ step, title, done, children }) {
  return (
    <div className="card" style={{ marginBottom: 22, padding: 24 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 11, marginBottom: 18 }}>
        <span style={{
          width: 30, height: 30, borderRadius: '50%',
          background: done ? 'var(--success)' : 'var(--accent)', color: '#fff',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: 13, fontWeight: 700, flexShrink: 0,
        }}>{done ? '✓' : step}</span>
        <h3 style={{ margin: 0, fontSize: 18 }}>{title}</h3>
      </div>
      {children}
    </div>
  );
}

/** "Check" / "Hide" toggle that reveals a block of detail. */
export function CheckToggle({ open, onToggle, label = 'Check', count }) {
  return (
    <button className="btn btn-sm" onClick={onToggle} style={{ marginTop: 12 }}>
      {open ? 'Hide' : label}{count != null && !open ? ` (${count})` : ''} {open ? '▴' : '▾'}
    </button>
  );
}

export function Panel({ title, children, style }) {
  return (
    <div style={{
      marginTop: 18, background: 'var(--surface)', border: '1px solid var(--border)',
      borderRadius: 'var(--radius)', padding: '17px 19px', ...style,
    }}>
      {title && <h4 style={{ fontSize: 14, marginBottom: 11 }}>{title}</h4>}
      {children}
    </div>
  );
}

export function Grid({ children, style }) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 14, ...style }}>
      {children}
    </div>
  );
}

export function Field({ label, value }) {
  return (
    <div>
      <div className="field-label">{label}</div>
      <div style={{ fontSize: 14, color: 'var(--text2)', wordBreak: 'break-word' }}>{value}</div>
    </div>
  );
}

export function ListCard({ title, items, tone = 'accent' }) {
  return (
    <div style={{
      background: 'var(--surface)', border: '1px solid var(--border)',
      borderLeft: `3px solid ${TONES[tone]}`, borderRadius: 'var(--radius)', padding: '14px 16px',
    }}>
      <h4 style={{ fontSize: 13, marginBottom: 8 }}>{title}</h4>
      {!items || items.length === 0 ? (
        <p className="muted" style={{ fontSize: 13, margin: 0 }}>None</p>
      ) : (
        <ul style={{ margin: 0, paddingLeft: 17, fontSize: 13, color: 'var(--text2)', lineHeight: 1.65 }}>
          {items.map((it, i) => <li key={i}>{it}</li>)}
        </ul>
      )}
    </div>
  );
}

/** One score component: label, points earned, progress bar, explanation. */
export function Bar({ label, component, weight }) {
  const pct = component.applicable ? Math.round((component.ratio || 0) * 100) : 0;
  return (
    <div style={{ marginBottom: 11 }} title={component.note}>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12.5, marginBottom: 4 }}>
        <span style={{ color: 'var(--text2)' }}>{label} <span className="muted">({weight}%)</span></span>
        <span className="muted">
          {component.applicable ? `${component.earned} / ${component.max} pts` : 'not stated in JD'}
        </span>
      </div>
      <div style={{ height: 6, background: 'var(--surface)', borderRadius: 4, overflow: 'hidden' }}>
        <div style={{
          width: `${pct}%`, height: '100%',
          background: component.applicable ? 'var(--accent)' : 'var(--border)',
          transition: 'width .3s',
        }} />
      </div>
      <div className="muted" style={{ fontSize: 11.5, marginTop: 3 }}>{component.note}</div>
    </div>
  );
}

/** Extracted candidate profile — shown behind "Check" on the input page
 *  and again in full on the results page. */
export function ResumeProfile({ resume }) {
  const categories = Object.entries(resume.skills_by_category || {});
  return (
    <div style={{ marginTop: 16 }}>
      <Grid>
        <Field label="Name" value={resume.name || '—'} />
        <Field label="Email" value={resume.email || '—'} />
        <Field label="Phone" value={resume.phone || '—'} />
        <Field label="Total experience" value={`${resume.total_experience_years || 0} yrs`} />
      </Grid>

      {resume.profile_summary && (
        <p style={{ marginTop: 14, marginBottom: 0, fontSize: 13.5, color: 'var(--text2)', lineHeight: 1.7 }}>
          {resume.profile_summary}
        </p>
      )}

      {categories.length > 0 && (
        <div style={{ marginTop: 18 }}>
          <div className="field-label" style={{ marginBottom: 9 }}>Skills by category</div>
          {categories.map(([cat, items]) => (
            <div key={cat} style={{ marginBottom: 10 }}>
              <div className="muted" style={{ fontSize: 11.5, textTransform: 'uppercase', letterSpacing: '.05em', marginBottom: 5 }}>
                {cat.replace(/_/g, ' ')}
              </div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 7 }}>
                {items.map((s) => <span key={s} className="pill">{s}</span>)}
              </div>
            </div>
          ))}
        </div>
      )}

      <Grid style={{ marginTop: 18 }}>
        <ListCard
          title="Education"
          items={(resume.education || []).map((e) => [
            e.institution,                       // present only from the AI parser
            e.degree,
            e.year,
            e.score,
          ].filter(Boolean).join(' · '))}
        />
        <ListCard
          title="Experience"
          items={(resume.experience || []).map((e) =>
            [e.title || e.role || 'Role', e.location, e.duration].filter(Boolean).join(' · '))}
        />
        <ListCard
          title="Projects"
          items={(resume.projects || []).map((p) =>
            p.skills?.length ? `${p.name} — ${p.skills.join(', ')}` : p.name)}
        />
        <ListCard title="Certifications" items={resume.certifications || []} />
      </Grid>

      {resume.extraction && <ExtractionNote extraction={resume.extraction} />}
    </div>
  );
}

/** Which parser produced this, and what it refused to trust. */
function ExtractionNote({ extraction }) {
  const dropped = extraction.dropped_unverifiable_skills || [];
  return (
    <p className="muted" style={{ fontSize: 12, marginTop: 14, marginBottom: 0, lineHeight: 1.6 }}>
      {extraction.source === 'ai'
        ? `Extracted by ${extraction.model} using the resume's bold text and headings, then checked back against the document.`
        : 'Extracted by the built-in parser (section headers + skill taxonomy).'}
      {extraction.error && ` — ${extraction.error}`}
      {dropped.length > 0 &&
        ` Rejected as not actually in the resume: ${dropped.join(', ')}.`}
    </p>
  );
}

/** Extracted job requirements. */
export function JobRequirements({ jd }) {
  return (
    <div style={{ marginTop: 16 }}>
      <Grid>
        <Field label="Job title" value={jd.job_title || '—'} />
        <Field
          label="Experience required"
          value={jd.min_years_experience ? `${jd.min_years_experience}+ yrs` : 'not stated'}
        />
      </Grid>
      <Grid style={{ marginTop: 14 }}>
        <ListCard title={`Required skills (${jd.required_skills?.length || 0})`} items={jd.required_skills} tone="danger" />
        <ListCard title={`Preferred skills (${jd.preferred_skills?.length || 0})`} items={jd.preferred_skills} tone="warn" />
        <ListCard title={`Keywords (${jd.keywords?.length || 0})`} items={jd.keywords} tone="accent" />
      </Grid>
      {jd.skills_source !== 'explicit' && (
        <p className="muted" style={{ fontSize: 12.5, marginTop: 10 }}>
          This job description listed no specific skills, so the required set was {jd.skills_source}.
        </p>
      )}
    </div>
  );
}
