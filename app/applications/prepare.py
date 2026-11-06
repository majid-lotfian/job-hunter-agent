"""Supervised application preparation: everything you need to apply in your own browser, checked and assembled.

For an approved job the package records: whether the vacancy is still open (employer API where available), the
tailored CV (HTML for review, PDF for upload - only a PDF that matches the current HTML), the employer's form fields
and screening questions, which of them your verified profile answers, and what is missing. Screening questions are
answered ONLY from explicit `answers` in data/applicant_profile.yaml - never guessed. Nothing is submitted here.
"""
import hashlib
import html as html_lib
import re

from ..core import now_iso
from . import adapters

# form field (key or label) -> profile field; first match wins
PROFILE_MAP = [
    (r'^(preferred[ _-]?(first[ _-]?)?name)$', None),                 # optional nickname: leave empty
    (r'\bfirst[ _-]?name\b|^voornaam', 'first_name'),
    (r'\blast[ _-]?name\b|surname|achternaam', 'last_name'),
    (r'^(full[ _-]?)?name$|^naam$', 'full_name'),
    (r'e-?mail', 'email'),
    (r'phone|telefoon|mobile', 'phone'),
    (r'linkedin', 'linkedin'),
    (r'github', 'github'),
    (r'portfolio|website|personal site', 'portfolio'),
    (r'salary|compensation|salaris', 'salary_expectation'),
    (r'notice period|opzegtermijn', 'notice_period'),
    (r'start date|availability|available to start|beschikbaar', 'availability'),
    (r'\bnationality|nationaliteit', 'nationality'),
    (r'(current )?(city|location)( of residence)?$|where (are you|do you) (based|live)|woonplaats', 'location'),
]


def _profile_value(profile, key):
    if profile is None:
        return None
    if key == 'full_name':
        return profile.full_name
    return profile.get(key)


def _choice_ok(field, answer):
    options = {str(o).strip().lower() for o in field.options}
    values = answer if isinstance(answer, list) else [answer]
    return bool(values) and all(str(v).strip().lower() in options for v in values)


def _bool_option(field, answer):
    """Map a true/false answer onto a Yes/No choice question (only exact Yes/No style options)."""
    want = {True: ('yes', 'ja', 'true'), False: ('no', 'nee', 'false')}[answer]
    hits = [o for o in field.options if str(o).strip().lower() in want]
    return hits[0] if len(hits) == 1 else None


def match_field(field, profile, cv):
    """Returns {'label','required','kind','value','source','status'}; status: ok | missing | needs_answer | optional."""
    row = {'key': field.key, 'label': field.label, 'required': field.required, 'kind': field.kind,
           'options': field.options[:20], 'question': field.question, 'value': None, 'source': None}

    def done(value, source):
        row.update(value=value, source=source, status='ok')
        return row

    def empty():
        row['status'] = ('needs_answer' if field.question else 'missing') if field.required else 'optional'
        return row

    text = f'{field.key} {field.label}'.lower()
    if field.kind == 'file' and re.search(r'resume|\bcv\b|curriculum', text):
        return done('Tailored CV (PDF)', 'cv_pdf') if cv.get('pdf') else empty()
    if re.search(r'cover[ _-]?letter|motivation', text):
        return empty()                                   # never generated automatically
    if field.kind == 'file':
        return empty()
    # 1) your explicit answer for this exact question always wins
    answer = profile.answer_for(field.label, field.key) if profile else None
    if answer is not None:
        if field.kind in ('single_choice', 'multi_choice') and field.options:
            if isinstance(answer, bool):
                mapped = _bool_option(field, answer)
                if mapped:
                    return done(mapped, 'answer')
            elif _choice_ok(field, answer):
                return done(answer, 'answer')
            row['note'] = 'your answer does not match the employer\'s options'
            return empty()
        return done(answer, 'answer')
    if field.question and field.kind != 'text':
        # choice/boolean screening questions: languages can be matched literally, anything else needs you
        if field.kind == 'multi_choice' and re.search(r'language|taal', text) and profile and profile.languages:
            picks = [o for o in field.options if str(o).strip().lower() in {k.lower() for k in profile.languages}]
            if picks:
                return done(picks, 'profile:languages')
        return empty()
    # 2) plain contact/profile fields
    for pattern, key in PROFILE_MAP:
        if re.search(pattern, field.key.lower()) or re.search(pattern, field.label.lower()):
            if key is None:
                return empty()
            value = _profile_value(profile, key)
            return done(value, f'profile:{key}') if value else empty()
    return empty()


def _digest(text):
    """Fingerprint of the posting's wording: HTML entities (&nbsp; ...) and whitespace differences are ignored."""
    text = html_lib.unescape(html_lib.unescape(text or '')).replace('\xa0', ' ')
    return hashlib.sha256(re.sub(r'\s+', ' ', text).strip().encode('utf-8')).hexdigest()[:16]


async def build_package(job, adapter, http, profile, cv, cv_version):
    """Assemble the review package for one job (read-only network access through the adapter)."""
    spec = await adapter.form(http, job)
    rows = [match_field(f, profile, cv) for f in spec.fields]
    errors, warnings_profile = profile.validate() if profile else (['no profile (data/applicant_profile.yaml)'], [])
    warnings = []
    if spec.captcha:
        warnings.append('The employer\'s form uses a CAPTCHA: submit it yourself in your browser (never automated).')
    if (job['language_req'] or '').startswith('dutch'):
        warnings.append(f"Language: {job['language_req'].replace('_', ' ')} - the posting is (partly) in Dutch.")
    stored = adapters._text(job['description'])
    changed = bool(spec.description) and _digest(spec.description) != _digest(stored)
    if changed:
        warnings.append('The live posting text differs from the version the CV was tailored to - re-read it before applying.')
    if spec.available is None and (job['availability'] or 'active') == 'active':
        warnings.append(f"Vacancy status not confirmed by the employer's system; last seen by discovery {str(job['last_seen_at'] or '?')[:16]}.")
    missing = [r['label'] for r in rows if r['status'] == 'missing']
    # fields the platform's documented submission API cannot carry (Recruitee: visible location choice)
    unsupported = [r['label'] for r in rows if r['key'] == 'location_choice' and r['required']]
    questions = [r['label'] for r in rows if r['status'] == 'needs_answer']
    if not cv.get('pdf') and not any(r['source'] is None and r['kind'] == 'file' and r['label'] in missing
                                     and re.search(r'resume|\bcv\b', r['label'].lower()) for r in rows):
        missing.insert(0, 'Tailored CV PDF (missing, invalid or outdated)')
    if errors:
        missing.append('Verified applicant profile (' + '; '.join(errors)[:200] + ')')
    blockers = []
    if not adapter.live_capable:
        blockers.append(f'{adapter.name}: no applicant-permitted submission API')
    else:
        if spec.available is not True:
            blockers.append('vacancy not confirmed open')
        if spec.captcha is not False:
            blockers.append('employer form uses a CAPTCHA' if spec.captcha else 'CAPTCHA setting unknown')
        if spec.consent_required is not False:
            blockers.append('consent agreement required' if spec.consent_required else 'consent setting unknown')
        blockers += [f'{label} has no documented API field' for label in unsupported]
    if missing or questions or not cv.get('pdf') or errors:
        blockers.append('missing information or unanswered questions')
    return {
        'prepared_at': now_iso(),
        'autonomous': {'possible': not blockers, 'blockers': blockers},
        'job': {'id': job['id'], 'title': job['title'], 'employer': job['employer'] or job['company'],
                'location': job['location_label'] or job['location'], 'score': job['score'], 'url': job['url']},
        'apply_page': spec.apply_page or job['apply_url'] or job['url'],
        'adapter': adapter.name,
        'availability': {'open': spec.available if spec.available is not None else
                         (None if (job['availability'] or 'active') == 'active' else False),
                         'checked_via': spec.checked_via or 'discovery'},
        'captcha': spec.captcha,
        'submission': 'browser',          # automated submission is not used for supervised applications
        'posting_changed': changed,
        'cv': {'version': cv_version, 'html': f"/api/jobs/{job['id']}/cv", 'pdf': f"/api/jobs/{job['id']}/cv.pdf",
               'pdf_current': bool(cv.get('pdf'))},
        'fields': rows,
        'missing': missing,
        'questions': questions,
        'profile': {'verified': bool(profile and profile.verified), 'errors': errors, 'notes': warnings_profile},
        'warnings': warnings,
        'notes': spec.notes,
        'ready': not missing and not questions and spec.available is not False,
    }
