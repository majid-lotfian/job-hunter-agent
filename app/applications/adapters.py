"""Application adapters.

An adapter has two steps:
  prepare()  read-only readiness check (may read the employer's PUBLIC job data); never submits anything.
  submit()   the actual submission - only called in APPLY_MODE=live, for adapters with live_capable=True that you
             listed in APPLY_LIVE_ADAPTERS.

form() (read-only) describes the employer's application form for the supervised workflow (app/applications/prepare.py):
fields, required flags, screening questions, whether the vacancy is still open and whether the form is CAPTCHA-protected.
Recruitee and Greenhouse publish this data without authentication; other systems get a generic field list.

Research (2026-11-05): Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Personio and Teamtailor application
APIs all require the EMPLOYER's API key, so they cannot be used by an applicant. Recruitee exposes each job's
application-form requirements publicly; its careers-site apply endpoint is not verified (documentation, terms,
CAPTCHA), so the Recruitee adapter is dry-run only (live_capable=False) until verified with the owner.
"""
import html as html_lib
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ..discovery.models import SourceError

# Result statuses
SUBMITTED, CONFIRMED, DRY_RUN, MANUAL, BLOCKED, RETRY, READY = (
    'submitted', 'confirmed', 'dry_run', 'manual_action_required', 'blocked', 'retry', 'ready')


@dataclass
class Result:
    status: str
    message: str = ''
    external_id: str = None
    missing: list = field(default_factory=list)
    fields: list = field(default_factory=list)   # names of fields that would be sent (never values)


@dataclass
class FormField:
    key: str
    label: str
    required: bool
    kind: str = 'text'          # text | email | phone | url | textarea | file | boolean | single_choice | multi_choice
    options: list = field(default_factory=list)
    question: bool = False      # a screening question (answered only from your explicit profile answers)


@dataclass
class FormSpec:
    fields: list
    available: bool = None      # True/False when the employer's system says so; None = not checked
    captcha: bool = None        # True when the employer's form uses a CAPTCHA; None = unknown
    apply_page: str = None
    checked_via: str = ''
    description: str = None     # latest posting text, when the system provides it
    notes: list = field(default_factory=list)


def _text(value):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html_lib.unescape(value or ''))).strip()


def generic_fields():
    return [FormField('first_name', 'First name', True), FormField('last_name', 'Last name', True),
            FormField('email', 'Email', True, 'email'), FormField('phone', 'Phone', False, 'phone'),
            FormField('cv', 'CV / resume (PDF)', True, 'file'), FormField('cover_letter', 'Cover letter', False, 'textarea'),
            FormField('linkedin', 'LinkedIn profile', False, 'url')]


class Adapter:
    name = 'base'
    live_capable = False

    def supports(self, job):
        return False

    async def form(self, http, job):
        """Read-only description of the application form. Default: typical fields, form not readable."""
        return FormSpec(generic_fields(), apply_page=job['apply_url'] or job['url'], checked_via='',
                        notes=['This system does not publish its form: the fields below are the usual ones - '
                               'check the page for extra questions.'])

    async def prepare(self, http, job, profile, cv):
        raise NotImplementedError

    async def submit(self, http, job, profile, cv, plan):
        return Result(MANUAL, f'{self.name}: live submission is not implemented')


class ManualAdapter(Adapter):
    """Default for every system without a permitted applicant-side API: you apply via the URL."""
    name = 'manual'

    def supports(self, job):
        return True

    async def prepare(self, http, job, profile, cv):
        return Result(MANUAL, 'This employer\'s application system has no permitted automatic submission - apply manually.')


def _common_missing(profile, cv):
    missing = []
    if profile is None:
        missing.append('applicant profile (data/applicant_profile.yaml)')
        return missing
    if not profile.verified:
        missing.append('profile not marked verified: true')
    if not profile.full_name:
        missing.append('name')
    if not profile.get('email'):
        missing.append('email')
    if cv is None or not cv.get('html'):
        missing.append('validated tailored CV')
    return missing


class RecruiteeAdapter(Adapter):
    """Recruitee careers sites. prepare() reads the job's public form requirements and checks that every required
    field can be filled from verified data. Live submission is disabled (endpoint not verified)."""
    name = 'recruitee'
    live_capable = False

    def supports(self, job):
        return (job['source'] or '').startswith('recruitee:') and bool(self._slug(job))

    @staticmethod
    def _slug(job):
        for url in (job['url'], job['apply_url']):
            m = re.search(r'/o/([\w-]+)', urlsplit(url or '').path)
            if m:
                return m.group(1)
        return None

    async def form(self, http, job):
        company, slug = job['source'].split(':', 1)[1], self._slug(job)
        page = job['apply_url'] or job['url']
        try:
            data = await http.get_json(f'https://{company}.recruitee.com/api/offers/{slug}')
        except SourceError as exc:
            closed = 'HTTP 404' in str(exc) or 'HTTP 410' in str(exc)
            return FormSpec(generic_fields(), available=False if closed else None, apply_page=page,
                            checked_via='Recruitee offer API', notes=[f'Could not read the form: {exc}'])
        offer = (data or {}).get('offer') or {}
        if not offer:
            return FormSpec([], available=False, apply_page=page, checked_via='Recruitee offer API')
        fields = [FormField('name', 'Full name', True), FormField('email', 'Email', True, 'email')]
        for option, key, label, kind in (('options_phone', 'phone', 'Phone', 'phone'), ('options_cv', 'cv', 'CV (PDF)', 'file'),
                                         ('options_cover_letter', 'cover_letter', 'Cover letter', 'textarea'),
                                         ('options_photo', 'photo', 'Photo', 'file')):
            if offer.get(option) in ('required', 'optional'):
                fields.append(FormField(key, label, offer[option] == 'required', kind))
        if offer.get('locations_question_required') and offer.get('location_question_visible'):
            fields.append(FormField('location_choice', offer.get('locations_question') or 'Preferred location', True,
                                    'single_choice', [l.get('city') for l in offer.get('locations') or [] if l.get('city')], True))
        kinds = {'boolean': 'boolean', 'legal': 'boolean', 'single_choice': 'single_choice', 'dropdown': 'single_choice',
                 'multi_choice': 'multi_choice', 'file': 'file', 'text': 'textarea', 'string': 'text'}
        for q in offer.get('open_questions') or []:
            label = (q.get('translations') or {}).get('en', {}).get('body') or q.get('body') or f"Question {q.get('id')}"
            fields.append(FormField(f"question_{q.get('id')}", _text(label), bool(q.get('required')), kinds.get(q.get('kind'), 'text'),
                                    [o.get('body') for o in q.get('open_question_options') or [] if o.get('body')], True))
        spec = FormSpec(fields, available=True, apply_page=offer.get('careers_apply_url') or page,
                        checked_via='Recruitee offer API', description=_text(offer.get('description')) + ' ' + _text(offer.get('requirements')))
        try:   # the public careers page says whether the employer turned on a CAPTCHA for this form
            text = html_lib.unescape(await http.get_text(spec.apply_page))   # config JSON sits HTML-escaped in an attribute
            mode = re.search(r'"captcha"\s*:\s*"(\w+)"', text)
            spec.captcha = bool((mode and mode.group(1).lower() not in ('none', 'off', 'disabled', 'false'))
                                or re.search(r'"hcaptcha"\s*:\s*true', text))
        except SourceError as exc:
            spec.notes.append(f'Could not check the application page for a CAPTCHA ({exc}) - assume manual submission.')
        return spec

    async def prepare(self, http, job, profile, cv):
        company = job['source'].split(':', 1)[1]
        try:
            data = await http.get_json(f'https://{company}.recruitee.com/api/offers/{self._slug(job)}')
        except SourceError as exc:
            if 'HTTP 404' in str(exc) or 'HTTP 410' in str(exc):
                return Result(BLOCKED, 'job form not found - the vacancy is closed')
            return Result(RETRY, f'could not read the job form: {exc}')
        offer = (data or {}).get('offer') or {}
        if not offer:
            return Result(BLOCKED, 'job form not found (job may be closed)')
        missing = _common_missing(profile, cv)
        fields = ['name', 'email']
        if offer.get('options_phone') == 'required':
            fields.append('phone')
            if profile and not profile.get('phone'):
                missing.append('phone')
        if offer.get('options_cv') == 'required':
            fields.append('cv (file)')
            if not (cv and cv.get('pdf')):
                missing.append('CV as PDF (missing, invalid or outdated)')
        for option, label in (('options_cover_letter', 'cover letter'), ('options_photo', 'photo')):
            if offer.get(option) == 'required':
                fields.append(label)
                missing.append(f'{label} (not generated by the agent)')
        if offer.get('locations_question_required'):
            fields.append('location choice')
            if not (profile and profile.answer_for(offer.get('locations_question') or 'location')):
                missing.append(f"answer: {offer.get('locations_question') or 'preferred location'}")
        for q in offer.get('open_questions') or []:
            if not q.get('required'):
                continue
            texts = [q.get('body')] + [t.get('body') for t in (q.get('translations') or {}).values() if isinstance(t, dict)]
            fields.append(f"question {q.get('id')}")
            answer = profile.answer_for(*texts) if profile else None
            if not _valid_answer(q, answer):
                missing.append(f"answer: {(q.get('translations') or {}).get('en', {}).get('body') or q.get('body')}"[:160])
        if missing:
            return Result(MANUAL, 'missing information - nothing was submitted', missing=missing, fields=fields)
        return Result(READY, 'all required fields can be filled from verified data', fields=fields)


def _valid_answer(question, answer):
    if answer is None:
        return False
    kind = question.get('kind')
    if kind == 'boolean':
        return isinstance(answer, bool)
    if kind in ('single_choice', 'multi_choice', 'dropdown'):
        options = {(o.get('body') or '').strip().lower() for o in question.get('open_question_options') or []}
        values = answer if isinstance(answer, list) else [answer]
        return bool(values) and all(str(v).strip().lower() in options for v in values)
    if kind == 'file':
        return False
    return isinstance(answer, (str, int, float)) and str(answer).strip() != ''


class GreenhouseAdapter(ManualAdapter):
    """Greenhouse job boards: the public board API lists every application question. Submission stays manual
    (Greenhouse's application API needs the employer's key)."""
    name = 'greenhouse'

    def supports(self, job):
        return (job['source'] or '').startswith('greenhouse:') and str(job['source_id'] or '').isdigit()

    async def form(self, http, job):
        board, page = job['source'].split(':', 1)[1], job['apply_url'] or job['url']
        try:
            data = await http.get_json(f'https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job["source_id"]}?questions=true')
        except SourceError as exc:
            closed = 'HTTP 404' in str(exc) or 'HTTP 410' in str(exc)
            return FormSpec(generic_fields(), available=False if closed else None, apply_page=page,
                            checked_via='Greenhouse board API', notes=[f'Could not read the form: {exc}'])
        kinds = {'input_text': 'text', 'textarea': 'textarea', 'input_file': 'file', 'input_hidden': None,
                 'multi_value_single_select': 'single_choice', 'multi_value_multi_select': 'multi_choice'}
        standard = {'first_name', 'last_name', 'preferred_name', 'email', 'phone', 'resume', 'resume_text',
                    'cover_letter', 'cover_letter_text'}
        fields = []
        for q in data.get('questions') or []:
            inputs = [f for f in q.get('fields') or [] if kinds.get(f.get('type'))]
            if not inputs:
                continue
            first = inputs[0]
            name = first.get('name') or ''
            kind = 'file' if any(f.get('type') == 'input_file' for f in inputs) else kinds[first['type']]
            options = [v.get('label') for v in first.get('values') or [] if v.get('label')]
            fields.append(FormField(name, _text(q.get('label')), bool(q.get('required')), kind, options,
                                    question=name.split('[')[0] not in standard))
        return FormSpec(fields, available=True, apply_page=data.get('absolute_url') or page, checked_via='Greenhouse board API',
                        description=_text(data.get('content')),
                        notes=['Greenhouse forms may use an invisible CAPTCHA; submit in your browser.'])


ADAPTERS = [RecruiteeAdapter(), GreenhouseAdapter(), ManualAdapter()]   # first match wins; ManualAdapter is the catch-all


def select_adapter(job):
    return next(a for a in ADAPTERS if a.supports(job))
