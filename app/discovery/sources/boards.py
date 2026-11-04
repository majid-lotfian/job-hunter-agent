"""Public job-board / aggregator APIs. All are search- or feed-based (not complete listings), so absence
from a response is never treated as closure. Attribution: each record keeps the source's own URL."""
import os

from ..models import AGGREGATOR, InvalidResponse, JobRecord
from ..normalize import clean_text, fix_mojibake, to_iso
from . import Source


class _Board(Source):
    kind = AGGREGATOR
    complete_listing = False


class ArbeitnowSource(_Board):
    """arbeitnow.com free job-board API (Europe, many NL/DE tech jobs). Paginated feed of recent jobs."""
    family = 'arbeitnow'
    attribution = 'Arbeitnow'
    default_interval_minutes = 120

    async def fetch(self, http, ctx):
        for page in range(1, self.max_pages + 1):
            data = await http.get_json('https://www.arbeitnow.com/api/job-board-api', params={'page': page})
            items = data.get('data') if isinstance(data, dict) else None
            if items is None:
                raise InvalidResponse('arbeitnow: missing "data"')
            ctx.pages += 1
            for j in items:
                yield JobRecord(
                    source_key=self.key, source_job_id=j['slug'], title=j.get('title') or '', url=j.get('url') or '',
                    employer=j.get('company_name'), location=j.get('location'),
                    remote_hint='remote' if j.get('remote') else None,
                    employment_type=(j.get('job_types') or [None])[0], description=clean_text(j.get('description')),
                    published_at=to_iso(j.get('created_at')), updated_marker=str(j.get('created_at')), kind=self.kind,
                    metadata={'tags': (j.get('tags') or [])[:6]})
            if not (data.get('links') or {}).get('next'):
                return
        ctx.truncated = True


class RemotiveSource(_Board):
    """remotive.com remote-jobs API. Terms: link back + credit Remotive; max ~4 requests/day."""
    family = 'remotive'
    attribution = 'Remotive'
    default_interval_minutes = 360

    async def fetch(self, http, ctx):
        for category in self.options.get('categories', ['software-development', 'artificial-intelligence', 'data']):
            data = await http.get_json('https://remotive.com/api/remote-jobs', params={'category': category})
            jobs = data.get('jobs') if isinstance(data, dict) else None
            if jobs is None:
                raise InvalidResponse('remotive: missing "jobs"')
            ctx.pages += 1
            for j in jobs:
                yield JobRecord(
                    source_key=self.key, source_job_id=str(j['id']), title=j.get('title') or '', url=j.get('url') or '',
                    employer=j.get('company_name'), location=j.get('candidate_required_location'), remote_hint='remote',
                    employment_type=j.get('job_type'), description=clean_text(j.get('description')),
                    published_at=to_iso(j.get('publication_date')), updated_marker=j.get('publication_date'),
                    kind=self.kind, metadata={'category': j.get('category')})


class RemoteOKSource(_Board):
    """remoteok.com API. Terms: link back to Remote OK and mention it as the source."""
    family = 'remoteok'
    attribution = 'Remote OK'
    default_interval_minutes = 360

    async def fetch(self, http, ctx):
        data = await http.get_json('https://remoteok.com/api')
        if not isinstance(data, list):
            raise InvalidResponse('remoteok: expected a list')
        ctx.pages = 1
        for j in data:
            if not isinstance(j, dict) or 'id' not in j or 'position' not in j:
                continue  # first element is the legal notice
            yield JobRecord(
                source_key=self.key, source_job_id=str(j['id']), title=fix_mojibake(j.get('position') or ''),
                url=j.get('url') or '', employer=fix_mojibake(j.get('company') or '') or None,
                location=fix_mojibake(j.get('location') or '') or None, remote_hint='remote',
                description=clean_text(fix_mojibake(j.get('description') or '')), published_at=to_iso(j.get('date')),
                updated_marker=j.get('date'), kind=self.kind, metadata={'tags': (j.get('tags') or [])[:6]})


class TheMuseSource(_Board):
    """themuse.com public jobs API, filtered by Dutch locations and tech categories."""
    family = 'themuse'
    attribution = 'The Muse'
    default_interval_minutes = 240

    async def fetch(self, http, ctx):
        locations = self.options.get('locations', ['Amsterdam, Netherlands', 'Utrecht, Netherlands', 'Rotterdam, Netherlands',
                                                   'The Hague, Netherlands', 'Eindhoven, Netherlands'])
        categories = self.options.get('categories', ['Data and Analytics', 'Software Engineering', 'Science and Engineering'])
        for category in categories:
            for page in range(self.max_pages):
                params = [('page', page), ('category', category)] + [('location', x) for x in locations]
                data = await http.get_json('https://www.themuse.com/api/public/jobs', params=params)
                results = data.get('results') if isinstance(data, dict) else None
                if results is None:
                    raise InvalidResponse('themuse: missing "results"')
                ctx.pages += 1
                for j in results:
                    locs = [x.get('name') for x in j.get('locations') or [] if x.get('name')]
                    yield JobRecord(
                        source_key=self.key, source_job_id=str(j['id']), title=(j.get('name') or '').strip(),
                        url=(j.get('refs') or {}).get('landing_page') or '', employer=(j.get('company') or {}).get('name'),
                        location=locs[0] if locs else None, locations=locs,
                        seniority_hint=((j.get('levels') or [{}])[0]).get('name'), description=clean_text(j.get('contents')),
                        published_at=to_iso(j.get('publication_date')), updated_marker=j.get('publication_date'), kind=self.kind)
                if page + 1 >= int(data.get('page_count') or 0):
                    break
            else:
                ctx.truncated = True


class JobicySource(_Board):
    """jobicy.com remote-jobs API (geo filter). Terms: credit Jobicy, link to the original job URL."""
    family = 'jobicy'
    attribution = 'Jobicy'
    default_interval_minutes = 240

    async def fetch(self, http, ctx):
        for geo in self.options.get('geos', ['netherlands', 'europe', 'emea']):
            data = await http.get_json('https://jobicy.com/api/v2/remote-jobs', params={'count': 50, 'geo': geo})
            jobs = data.get('jobs') if isinstance(data, dict) else None
            if jobs is None:
                if isinstance(data, dict) and data.get('jobCount') == 0:
                    continue
                raise InvalidResponse('jobicy: missing "jobs"')
            ctx.pages += 1
            for j in jobs:
                yield JobRecord(
                    source_key=self.key, source_job_id=str(j['id']), title=clean_text(j.get('jobTitle')), url=j.get('url') or '',
                    employer=j.get('companyName'), location=j.get('jobGeo'), remote_hint='remote',
                    employment_type=', '.join(j.get('jobType') or []) if isinstance(j.get('jobType'), list) else j.get('jobType'),
                    seniority_hint=j.get('jobLevel'), description=clean_text(j.get('jobDescription') or j.get('jobExcerpt')),
                    published_at=to_iso(j.get('pubDate')), updated_marker=j.get('pubDate'), kind=self.kind)


class HimalayasSource(_Board):
    """himalayas.app remote-jobs search API (20 per page)."""
    family = 'himalayas'
    attribution = 'Himalayas'
    default_interval_minutes = 240

    async def fetch(self, http, ctx):
        seen = set()
        for query in self.options.get('queries', ['machine learning engineer', 'AI engineer', 'LLM', 'data scientist']):
            for page in range(1, self.max_pages + 1):
                data = await http.get_json('https://himalayas.app/jobs/api/search',
                                           params={'q': query, 'country': 'Netherlands', 'page': page})
                jobs = data.get('jobs') if isinstance(data, dict) else None
                if jobs is None:
                    raise InvalidResponse('himalayas: missing "jobs"')
                ctx.pages += 1
                for j in jobs:
                    guid = j.get('guid') or j.get('applicationLink')
                    if not guid or guid in seen:
                        continue
                    seen.add(guid)
                    restrictions = j.get('locationRestrictions') or []
                    yield JobRecord(
                        source_key=self.key, source_job_id=guid, title=j.get('title') or '', url=guid,
                        apply_url=j.get('applicationLink'), employer=j.get('companyName'),
                        location='Remote - ' + ', '.join(restrictions) if restrictions else 'Remote - Worldwide',
                        remote_hint='remote', employment_type=j.get('employmentType'),
                        seniority_hint=', '.join(j.get('seniority') or []) if isinstance(j.get('seniority'), list) else j.get('seniority'),
                        description=clean_text(j.get('description') or j.get('excerpt')), published_at=to_iso(j.get('pubDate')),
                        updated_marker=str(j.get('pubDate')), kind=self.kind)
                if len(jobs) < 20:
                    break


class WorkingNomadsSource(_Board):
    """workingnomads.com public feed of recent remote jobs."""
    family = 'workingnomads'
    attribution = 'Working Nomads'
    default_interval_minutes = 240

    async def fetch(self, http, ctx):
        data = await http.get_json('https://www.workingnomads.com/api/exposed_jobs/')
        if not isinstance(data, list):
            raise InvalidResponse('workingnomads: expected a list')
        ctx.pages = 1
        for j in data:
            if not j.get('url'):
                continue
            yield JobRecord(
                source_key=self.key, source_job_id=j['url'], title=j.get('title') or '', url=j['url'],
                employer=j.get('company_name'), location=j.get('location'), remote_hint='remote',
                description=clean_text(j.get('description')), published_at=to_iso(j.get('pub_date')),
                updated_marker=j.get('pub_date'), kind=self.kind, metadata={'category': j.get('category_name')})


class AdzunaSource(_Board):
    """Adzuna search API for the Netherlands. OPTIONAL: needs ADZUNA_APP_ID and ADZUNA_APP_KEY (free registration)."""
    family = 'adzuna'
    attribution = 'Adzuna'
    required_env = ('ADZUNA_APP_ID', 'ADZUNA_APP_KEY')
    default_interval_minutes = 360

    async def fetch(self, http, ctx):
        auth = {'app_id': os.getenv('ADZUNA_APP_ID', '').strip(), 'app_key': os.getenv('ADZUNA_APP_KEY', '').strip()}
        for query in self.options.get('queries', ['machine learning engineer', 'AI engineer', 'LLM engineer', 'data scientist machine learning']):
            for page in range(1, self.max_pages + 1):
                data = await http.get_json(f'https://api.adzuna.com/v1/api/jobs/nl/search/{page}', params={
                    **auth, 'what': query, 'results_per_page': 50, 'max_days_old': 30, 'content-type': 'application/json'})
                results = data.get('results') if isinstance(data, dict) else None
                if results is None:
                    raise InvalidResponse('adzuna: missing "results"')
                ctx.pages += 1
                for j in results:
                    yield JobRecord(
                        source_key=self.key, source_job_id=str(j['id']), title=clean_text(j.get('title')),
                        url=j.get('redirect_url') or '', employer=(j.get('company') or {}).get('display_name'),
                        location=(j.get('location') or {}).get('display_name'), country='NL',
                        employment_type=j.get('contract_time'), description=clean_text(j.get('description')),
                        published_at=to_iso(j.get('created')), updated_marker=j.get('created'), kind=self.kind)
                if len(results) < 50:
                    break


class JoobleSource(_Board):
    """Jooble REST API. OPTIONAL: needs JOOBLE_API_KEY (request via jooble.org/api/about)."""
    family = 'jooble'
    attribution = 'Jooble'
    required_env = ('JOOBLE_API_KEY',)
    default_interval_minutes = 360

    async def fetch(self, http, ctx):
        key = os.getenv('JOOBLE_API_KEY', '').strip()
        for query in self.options.get('queries', ['machine learning engineer', 'AI engineer', 'LLM engineer']):
            for page in range(1, self.max_pages + 1):
                data = await http.post_json(f'https://jooble.org/api/{key}', {'keywords': query, 'location': 'Netherlands', 'page': page})
                jobs = data.get('jobs') if isinstance(data, dict) else None
                if jobs is None:
                    raise InvalidResponse('jooble: missing "jobs"')
                ctx.pages += 1
                for j in jobs:
                    yield JobRecord(
                        source_key=self.key, source_job_id=str(j.get('id') or j.get('link')), title=clean_text(j.get('title')),
                        url=j.get('link') or '', employer=j.get('company'), location=j.get('location'), country='NL',
                        employment_type=j.get('type'), description=clean_text(j.get('snippet')),
                        published_at=to_iso(j.get('updated')), updated_marker=j.get('updated'), kind=self.kind)
                if len(jobs) < 20:
                    break


BOARD_SOURCES = {cls.family: cls for cls in (ArbeitnowSource, RemotiveSource, RemoteOKSource, TheMuseSource, JobicySource,
                                              HimalayasSource, WorkingNomadsSource, AdzunaSource, JoobleSource)}
