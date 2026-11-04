"""Applicant-tracking-system adapters (public, unauthenticated job-board endpoints).

Every endpoint here was verified against real public data (see docs/discovery/SOURCES.md).
"""
import html
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote

from ..models import AGGREGATOR, DIRECT, InvalidResponse, JobRecord
from ..normalize import clean_text, to_iso
from . import Source


def _q(value):
    return quote(str(value), safe='')


class GreenhouseSource(Source):
    """boards-api.greenhouse.io: list (small) + per-job detail only for promising, new/changed jobs."""
    family = 'greenhouse'

    def __init__(self, key, options):
        super().__init__(key, options)
        self.board = options['board']

    async def fetch(self, http, ctx):
        data = await http.get_json(f'https://boards-api.greenhouse.io/v1/boards/{_q(self.board)}/jobs')
        jobs = data.get('jobs') if isinstance(data, dict) else None
        if jobs is None:
            raise InvalidResponse('greenhouse: missing "jobs"')
        ctx.pages = 1
        for job in jobs:
            yield JobRecord(
                source_key=self.key, source_job_id=str(job['id']), title=job.get('title') or '',
                url=job.get('absolute_url') or '', employer=self.options.get('name'),
                location=(job.get('location') or {}).get('name'), updated_marker=job.get('updated_at'),
                needs_detail=True, detail_ref=job['id'],
                metadata={'requisition_id': job.get('requisition_id'), 'internal_job_id': job.get('internal_job_id')})

    async def detail(self, http, record):
        d = await http.get_json(f'https://boards-api.greenhouse.io/v1/boards/{_q(self.board)}/jobs/{record.detail_ref}')
        record.description = clean_text(html.unescape(d.get('content') or ''))
        record.employer = d.get('company_name') or record.employer
        offices = [o.get('location') or o.get('name') for o in d.get('offices') or [] if o]
        record.locations = [x for x in offices if x]
        record.published_at = to_iso(d.get('first_published')) or None
        record.metadata.update({'requisition_id': d.get('requisition_id'),
                                'departments': [x.get('name') for x in d.get('departments') or []][:3]})
        record.needs_detail = False
        return record


class LeverSource(Source):
    """api.lever.co postings, paginated (100/page). Optional server-side `locations` filter."""
    family = 'lever'
    page_size = 100

    def __init__(self, key, options):
        super().__init__(key, options)
        self.company = options['company']
        self.kind = AGGREGATOR if options.get('aggregator') else DIRECT
        self.locations = list(options.get('locations') or [])

    async def fetch(self, http, ctx):
        skip = 0
        for _ in range(self.max_pages):
            params = [('mode', 'json'), ('limit', self.page_size), ('skip', skip)] + [('location', x) for x in self.locations]
            page = await http.get_json(f'https://api.lever.co/v0/postings/{_q(self.company)}', params=params)
            if not isinstance(page, list):
                raise InvalidResponse('lever: expected a list')
            ctx.pages += 1
            for p in page:
                yield self._record(p)
            if len(page) < self.page_size:
                return
            skip += self.page_size
        ctx.truncated = True

    def _record(self, p):
        cat = p.get('categories') or {}
        lists = '\n'.join(f"{x.get('text', '')}\n{clean_text(x.get('content', ''))}" for x in p.get('lists') or [])
        description = '\n'.join(filter(None, [p.get('descriptionPlain') or clean_text(p.get('description')), lists,
                                              p.get('additionalPlain') or clean_text(p.get('additional'))]))
        aggregator = self.kind == AGGREGATOR
        return JobRecord(
            source_key=self.key, source_job_id=p['id'], title=p.get('text') or '', url=p.get('hostedUrl') or '',
            apply_url=p.get('applyUrl'), employer=None if aggregator else self.options.get('name', self.company),
            location=cat.get('location'), locations=list(cat.get('allLocations') or []), country=p.get('country'),
            remote_hint=p.get('workplaceType'), employment_type=cat.get('commitment'),
            description=description[:20000], published_at=to_iso(p.get('createdAt')), kind=self.kind,
            updated_marker=str(p.get('updatedAt') or p.get('createdAt') or ''),
            metadata={'team': cat.get('team'), 'department': cat.get('department'),
                      **({'employer_hidden': True} if aggregator else {})})


class AshbySource(Source):
    """api.ashbyhq.com public job-board API (single response incl. descriptions)."""
    family = 'ashby'

    def __init__(self, key, options):
        super().__init__(key, options)
        self.board = options['board']

    async def fetch(self, http, ctx):
        data = await http.get_json(f'https://api.ashbyhq.com/posting-api/job-board/{_q(self.board)}',
                                   params={'includeCompensation': 'false'})
        jobs = data.get('jobs') if isinstance(data, dict) else None
        if jobs is None:
            raise InvalidResponse('ashby: missing "jobs"')
        ctx.pages = 1
        for j in jobs:
            if j.get('isListed') is False:
                continue
            addr = ((j.get('address') or {}).get('postalAddress') or {})
            secondary = [s.get('location') for s in j.get('secondaryLocations') or [] if s.get('location')]
            locs = [j.get('location')] + secondary + [', '.join(filter(None, [addr.get('addressLocality'), addr.get('addressCountry')]))]
            yield JobRecord(
                source_key=self.key, source_job_id=j['id'], title=j.get('title') or '', url=j.get('jobUrl') or '',
                apply_url=j.get('applyUrl'), employer=self.options.get('name', self.board), location=j.get('location'),
                locations=[x for x in locs if x], remote_hint=(j.get('workplaceType') or ('remote' if j.get('isRemote') else None)),
                employment_type=j.get('employmentType'),
                description=(j.get('descriptionPlain') or clean_text(j.get('descriptionHtml')))[:20000],
                published_at=to_iso(j.get('publishedAt')), updated_marker=j.get('publishedAt'),
                metadata={'department': j.get('department'), 'team': j.get('team')})


class SmartRecruitersSource(Source):
    """api.smartrecruiters.com public postings API (filtered by country), detail per promising job."""
    family = 'smartrecruiters'
    page_size = 100

    def __init__(self, key, options):
        super().__init__(key, options)
        self.company = options['company']
        self.country = options.get('country', 'nl')

    async def fetch(self, http, ctx):
        offset = 0
        for _ in range(self.max_pages):
            params = {'limit': self.page_size, 'offset': offset, **({'country': self.country} if self.country else {})}
            data = await http.get_json(f'https://api.smartrecruiters.com/v1/companies/{_q(self.company)}/postings', params=params)
            content = data.get('content') if isinstance(data, dict) else None
            if content is None:
                raise InvalidResponse('smartrecruiters: missing "content"')
            ctx.pages += 1
            for p in content:
                loc = p.get('location') or {}
                yield JobRecord(
                    source_key=self.key, source_job_id=str(p['id']), title=p.get('name') or '',
                    url=f"https://jobs.smartrecruiters.com/{_q(self.company)}/{p['id']}",
                    employer=(p.get('company') or {}).get('name') or self.options.get('name'),
                    location=loc.get('fullLocation') or ', '.join(filter(None, [loc.get('city'), loc.get('country')])),
                    country=(loc.get('country') or '').upper() or None,
                    remote_hint='remote' if loc.get('remote') else ('hybrid' if loc.get('hybrid') else None),
                    employment_type=(p.get('typeOfEmployment') or {}).get('label'),
                    seniority_hint=(p.get('experienceLevel') or {}).get('label'),
                    published_at=to_iso(p.get('releasedDate')), updated_marker=p.get('releasedDate'),
                    needs_detail=True, detail_ref=p.get('ref'), metadata={'ref_number': p.get('refNumber')})
            offset += len(content)
            if len(content) < self.page_size or offset >= int(data.get('totalFound') or 0):
                return
        ctx.truncated = True

    async def detail(self, http, record):
        d = await http.get_json(record.detail_ref)
        sections = (d.get('jobAd') or {}).get('sections') or {}
        parts = [clean_text((sections.get(k) or {}).get('text')) for k in ('jobDescription', 'qualifications', 'additionalInformation')]
        record.description = '\n'.join(p for p in parts if p)[:20000]
        record.url = d.get('postingUrl') or record.url
        record.apply_url = d.get('applyUrl') or record.apply_url
        record.needs_detail = False
        return record


class WorkableSource(Source):
    """apply.workable.com public widget API (single request incl. descriptions). Workable rate-limits hard."""
    family = 'workable'
    default_interval_minutes = 180

    def __init__(self, key, options):
        super().__init__(key, options)
        self.account = options['account']

    async def fetch(self, http, ctx):
        data = await http.get_json(f'https://apply.workable.com/api/v1/widget/accounts/{_q(self.account)}',
                                   params={'details': 'true'})
        jobs = data.get('jobs') if isinstance(data, dict) else None
        if jobs is None:
            raise InvalidResponse('workable: missing "jobs"')
        ctx.pages = 1
        for j in jobs:
            locs = [', '.join(filter(None, [x.get('city'), x.get('region'), x.get('country')])) for x in j.get('locations') or []]
            yield JobRecord(
                source_key=self.key, source_job_id=str(j.get('shortcode') or j.get('code')), title=j.get('title') or '',
                url=j.get('url') or j.get('shortlink') or '', apply_url=j.get('application_url'),
                employer=data.get('name') or self.options.get('name'),
                location=', '.join(filter(None, [j.get('city'), j.get('state'), j.get('country')])), locations=locs,
                remote_hint='remote' if j.get('telecommuting') else None, employment_type=j.get('employment_type'),
                seniority_hint=j.get('experience'), description=clean_text(j.get('description')),
                published_at=to_iso(j.get('published_on') or j.get('created_at')), updated_marker=j.get('published_on'),
                metadata={'department': j.get('department')})


class RecruiteeSource(Source):
    """{company}.recruitee.com/api/offers (public careers-site API)."""
    family = 'recruitee'

    def __init__(self, key, options):
        super().__init__(key, options)
        self.company = options['company']

    async def fetch(self, http, ctx):
        data = await http.get_json(f'https://{_q(self.company)}.recruitee.com/api/offers/')
        offers = data.get('offers') if isinstance(data, dict) else None
        if offers is None:
            raise InvalidResponse('recruitee: missing "offers"')
        ctx.pages = 1
        for o in offers:
            if o.get('status') not in (None, 'published'):
                continue
            locs = [', '.join(filter(None, [x.get('city'), x.get('country')])) for x in o.get('locations') or []]
            remote = 'remote' if o.get('remote') else ('hybrid' if o.get('hybrid') else ('onsite' if o.get('on_site') else None))
            yield JobRecord(
                source_key=self.key, source_job_id=str(o['id']), title=o.get('title') or '',
                url=o.get('careers_url') or '', apply_url=o.get('careers_apply_url'),
                employer=o.get('company_name') or self.options.get('name'), location=o.get('location'),
                locations=[x for x in locs if x] + [', '.join(filter(None, [o.get('city'), o.get('country')]))],
                country=(o.get('country_code') or '').upper() or None, remote_hint=remote,
                employment_type=o.get('employment_type_code'), seniority_hint=o.get('experience_code'),
                description='\n'.join(filter(None, [clean_text(o.get('description')), clean_text(o.get('requirements'))]))[:20000],
                published_at=to_iso(o.get('published_at')), updated_marker=o.get('updated_at') or o.get('published_at'),
                metadata={'department': o.get('department')})


class PersonioSource(Source):
    """{company}.jobs.personio.de/xml - Personio's documented public XML job feed."""
    family = 'personio'

    def __init__(self, key, options):
        super().__init__(key, options)
        self.company = options['company']
        self.domain = options.get('domain', 'de')

    async def fetch(self, http, ctx):
        base = f'https://{_q(self.company)}.jobs.personio.{self.domain}'
        text = await http.get_text(f'{base}/xml', params={'language': 'en'})
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            raise InvalidResponse('personio: invalid XML') from None
        ctx.pages = 1
        for pos in root.iter('position'):
            get = lambda tag: (pos.findtext(tag) or '').strip()
            offices = [get('office')] + [(o.text or '').strip() for o in pos.findall('additionalOffices/office')]
            desc = '\n'.join(f"{d.findtext('name') or ''}\n{clean_text(d.findtext('value'))}" for d in pos.findall('jobDescriptions/jobDescription'))
            yield JobRecord(
                source_key=self.key, source_job_id=get('id'), title=get('name'), url=f"{base}/job/{get('id')}",
                employer=get('subcompany') or self.options.get('name'), location=offices[0] if offices else None,
                locations=[o for o in offices if o], employment_type=get('employmentType') or get('schedule'),
                seniority_hint=get('seniority'), description=desc[:20000], published_at=to_iso(get('createdAt')),
                updated_marker=get('createdAt'), metadata={'department': get('department')})


class TeamtailorSource(Source):
    """Public Teamtailor careers-site RSS feed ({subdomain}.teamtailor.com/jobs.rss or custom domain)."""
    family = 'teamtailor'
    NS = {'tt': 'https://teamtailor.com/locations'}

    def __init__(self, key, options):
        super().__init__(key, options)
        self.feed = options.get('feed') or f"https://{_q(options['subdomain'])}.teamtailor.com/jobs.rss"

    async def fetch(self, http, ctx):
        text = await http.get_text(self.feed)
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            raise InvalidResponse('teamtailor: invalid RSS') from None
        ctx.pages = 1
        for item in root.iter('item'):
            locs = []
            for loc in item.findall('tt:locations/tt:location', self.NS):
                parts = [loc.findtext('tt:city', default='', namespaces=self.NS), loc.findtext('tt:country', default='', namespaces=self.NS)]
                locs.append(', '.join(p for p in parts if p) or loc.findtext('tt:name', default='', namespaces=self.NS))
            link = (item.findtext('link') or '').strip()
            job_id = re.search(r'/jobs/(\d+)', link)
            yield JobRecord(
                source_key=self.key, source_job_id=job_id.group(1) if job_id else (item.findtext('guid') or link),
                title=(item.findtext('title') or '').strip(), url=link, employer=self.options.get('name'),
                location=locs[0] if locs else None, locations=locs,
                remote_hint={'fully': 'remote', 'hybrid': 'hybrid', 'none': 'onsite'}.get((item.findtext('remoteStatus') or '').strip()),
                description=clean_text(item.findtext('description')), published_at=to_iso(item.findtext('pubDate')),
                updated_marker=item.findtext('pubDate'),
                metadata={'department': item.findtext('tt:department', default=None, namespaces=self.NS)})


class WorkdaySource(Source):
    """Workday career-site JSON search (OPT-IN). Undocumented but public and not disallowed by robots.txt;
    query-based, so absence from results is not treated as closure."""
    family = 'workday'
    complete_listing = False
    default_interval_minutes = 360
    page_size = 20

    def __init__(self, key, options):
        super().__init__(key, options)
        self.enabled = bool(options.get('enabled', False))  # disabled unless explicitly enabled
        self.tenant, self.wd, self.site = options['tenant'], options.get('wd', 'wd3'), options['site']
        self.queries = list(options.get('queries') or ['machine learning', 'AI engineer', 'data scientist', 'LLM'])
        self.base = f'https://{self.tenant}.{self.wd}.myworkdayjobs.com'

    async def fetch(self, http, ctx):
        seen = set()
        url = f'{self.base}/wday/cxs/{_q(self.tenant)}/{_q(self.site)}/jobs'
        for query in self.queries:
            for page in range(self.max_pages):
                data = await http.post_json(url, {'appliedFacets': {}, 'limit': self.page_size,
                                                  'offset': page * self.page_size, 'searchText': query})
                posts = data.get('jobPostings') if isinstance(data, dict) else None
                if posts is None:
                    raise InvalidResponse('workday: missing "jobPostings"')
                ctx.pages += 1
                for p in posts:
                    path = p.get('externalPath')
                    if not path or path in seen:
                        continue
                    seen.add(path)
                    req = (p.get('bulletFields') or [None])[0]
                    yield JobRecord(
                        source_key=self.key, source_job_id=req or path, title=p.get('title') or '',
                        url=f'{self.base}/{self.site}{path}', employer=self.options.get('name', self.tenant),
                        location=p.get('locationsText'), needs_detail=True, detail_ref=path, updated_marker=req or path)
                if len(posts) < self.page_size:
                    break

    async def detail(self, http, record):
        d = (await http.get_json(f'{self.base}/wday/cxs/{_q(self.tenant)}/{_q(self.site)}{record.detail_ref}')).get('jobPostingInfo') or {}
        record.description = clean_text(d.get('jobDescription'))
        locs = [d.get('location')] + list(d.get('additionalLocations') or [])
        record.locations = [x for x in locs if x]
        record.location = d.get('location') or record.location
        record.country = ((d.get('country') or {}).get('descriptor'))
        record.employment_type = d.get('timeType')
        record.published_at = to_iso(d.get('startDate'))
        record.url = d.get('externalUrl') or record.url
        record.metadata['requisition_id'] = d.get('jobReqId')  # id stays the list id (stable across runs)
        record.needs_detail = False
        return record


ATS_SOURCES = {cls.family: cls for cls in (GreenhouseSource, LeverSource, AshbySource, SmartRecruitersSource,
                                            WorkableSource, RecruiteeSource, PersonioSource, TeamtailorSource, WorkdaySource)}
