"""Discovery subsystem tests: adapters (mocked HTTP), normalization, relevance, dedupe, store, engine.

Deterministic: every HTTP call goes through httpx.MockTransport; temporary SQLite DB only.
"""
import asyncio
import copy
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_TMP = tempfile.TemporaryDirectory()
os.environ.setdefault('DB_PATH', str(Path(_TMP.name) / 'discovery-test.sqlite3'))
os.environ.setdefault('DASHBOARD_TOKEN', 'test-token-for-unit-tests')
os.environ['TELEGRAM_BOT_TOKEN'] = ''

import httpx  # noqa: E402

from app import core  # noqa: E402
from app.discovery import config as dconfig  # noqa: E402
from app.discovery import dedupe, engine, normalize, relevance, store  # noqa: E402
from app.discovery.http import HttpClient  # noqa: E402
from app.discovery.models import AGGREGATOR, InvalidResponse, JobRecord, SourceError, SourceRateLimited  # noqa: E402
from app.discovery.sources import FetchContext, Source  # noqa: E402
from app.discovery.sources import ats, boards  # noqa: E402

AI_DESC = ('We build LLM agents and RAG pipelines in Python with PyTorch, fine-tuning transformers, '
           'deploying models with MLOps and evaluating them. ') * 6


def run(coro):
    return asyncio.run(coro)


def make_http(handler, **kwargs):
    async def no_sleep(_):
        return None
    return HttpClient(transport=httpx.MockTransport(handler), sleep=no_sleep, default_interval=0, retries=2, **kwargs)


async def collect(source, http):
    ctx = FetchContext()
    out = [r async for r in source.fetch(http, ctx)]
    for i, r in enumerate(out):
        if r.needs_detail:
            out[i] = await source.detail(http, r)
    await http.aclose()
    return out, ctx


def reset_db():
    core.init_db()
    with core.connect() as db:
        for table in ('job_sources', 'source_health', 'discovery_runs', 'cv_jobs', 'events', 'jobs'):
            db.execute(f'DELETE FROM {table}')


# --------------------------------------------------------------------------- HTTP client

class HttpClientTests(unittest.TestCase):
    def test_retries_temporary_errors_then_succeeds(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, json={'ok': True})
        http = make_http(handler)
        self.assertEqual(run(http.get_json('https://x.test/a')), {'ok': True})
        self.assertEqual(len(calls), 3)

    def test_rate_limit_honours_retry_after_then_raises(self):
        sleeps = []

        async def fake_sleep(s):
            sleeps.append(s)
        http = HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={'retry-after': '7'})),
                          sleep=fake_sleep, default_interval=0, retries=2)
        with self.assertRaises(SourceRateLimited):
            run(http.get_json('https://x.test/a'))
        self.assertTrue(all(s >= 7 for s in sleeps) and len(sleeps) == 2)

    def test_retry_after_is_capped(self):
        sleeps = []

        async def fake_sleep(s):
            sleeps.append(s)
        http = HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={'retry-after': '99999'})),
                          sleep=fake_sleep, default_interval=0, retries=1)
        with self.assertRaises(SourceRateLimited):
            run(http.get_json('https://x.test/a'))
        self.assertLessEqual(max(sleeps), 61)

    def test_timeout_and_network_errors_become_source_errors(self):
        for exc in (httpx.ReadTimeout('slow'), httpx.ConnectError('down')):
            def handler(request, exc=exc):
                raise exc
            with self.assertRaises(SourceError):
                run(make_http(handler).get_json('https://x.test/a'))

    def test_4xx_is_not_retried(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(404)
        with self.assertRaisesRegex(SourceError, '404'):
            run(make_http(handler).get_json('https://x.test/a'))
        self.assertEqual(len(calls), 1)

    def test_invalid_json_and_size_cap(self):
        with self.assertRaises(InvalidResponse):
            run(make_http(lambda r: httpx.Response(200, text='<html>nope')).get_json('https://x.test/a'))
        with self.assertRaisesRegex(InvalidResponse, 'too large'):
            run(make_http(lambda r: httpx.Response(200, content=b'x' * 5000), max_bytes=1000).get_json('https://x.test/a'))

    def test_per_host_pacing(self):
        sleeps, now = [], [100.0]

        async def fake_sleep(s):
            sleeps.append(s)
            now[0] += s
        http = HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})), sleep=fake_sleep,
                          clock=lambda: now[0], default_interval=2.0, host_intervals={'slow.test': 5.0})

        async def go():
            await http.get_json('https://slow.test/1')
            await http.get_json('https://slow.test/2')
            await http.get_json('https://fast.test/1')
            await http.get_json('https://fast.test/2')
        run(go())
        self.assertEqual(sleeps, [5.0, 2.0])


# --------------------------------------------------------------------------- adapters (mocked real-shaped payloads)

class AdapterTests(unittest.TestCase):
    def test_greenhouse_list_then_detail(self):
        def handler(request):
            path = request.url.path
            if path.endswith('/boards/acme/jobs'):
                return httpx.Response(200, json={'jobs': [{'id': 11, 'title': 'ML Engineer', 'absolute_url': 'https://job-boards.greenhouse.io/acme/jobs/11',
                                                           'location': {'name': 'Amsterdam'}, 'updated_at': '2026-11-01T10:00:00-04:00'}]})
            if path.endswith('/boards/acme/jobs/11'):
                return httpx.Response(200, json={'content': '&lt;p&gt;Build &lt;b&gt;LLM&lt;/b&gt; systems&lt;/p&gt;',
                                                 'company_name': 'Acme', 'offices': [{'location': 'Amsterdam, Netherlands'}],
                                                 'first_published': '2026-10-01T00:00:00Z', 'departments': [{'name': 'AI'}]})
            return httpx.Response(404)
        recs, _ = run(collect(ats.GreenhouseSource('greenhouse:acme', {'board': 'acme'}), make_http(handler)))
        r = recs[0]
        self.assertEqual((r.source_job_id, r.employer, r.description), ('11', 'Acme', 'Build LLM systems'))
        self.assertEqual(r.locations, ['Amsterdam, Netherlands'])
        self.assertEqual(r.published_at, '2026-10-01T00:00:00+00:00')

    def test_lever_paginates_and_filters_location(self):
        seen = []

        def handler(request):
            params = request.url.params
            seen.append((params.get('skip'), params.get_list('location')))
            skip = int(params.get('skip'))
            page = [{'id': f'id-{skip + i}', 'text': 'AI Engineer', 'hostedUrl': f'https://jobs.lever.co/x/id-{skip + i}',
                     'categories': {'location': 'Netherlands'}, 'createdAt': 1793692203224, 'descriptionPlain': 'd'}
                    for i in range(100 if skip == 0 else 7)]
            return httpx.Response(200, json=page)
        src = ats.LeverSource('lever:x', {'company': 'x', 'aggregator': True, 'locations': ['Netherlands']})
        recs, ctx = run(collect(src, make_http(handler)))
        self.assertEqual(len(recs), 107)
        self.assertEqual(seen, [('0', ['Netherlands']), ('100', ['Netherlands'])])
        self.assertFalse(ctx.truncated)
        self.assertEqual(recs[0].kind, AGGREGATOR)
        self.assertIsNone(recs[0].employer)  # aggregator hides the employer: never guessed

    def test_lever_page_limit_marks_truncated(self):
        page = [{'id': f'i{n}', 'text': 't', 'categories': {}} for n in range(100)]
        src = ats.LeverSource('lever:x', {'company': 'x', 'max_pages': 2})
        recs, ctx = run(collect(src, make_http(lambda r: httpx.Response(200, json=page))))
        self.assertTrue(ctx.truncated)

    def test_ashby_skips_unlisted_and_reads_address(self):
        payload = {'jobs': [
            {'id': 'a1', 'title': 'Applied Scientist', 'location': 'Amsterdam', 'isListed': True, 'workplaceType': 'Hybrid',
             'address': {'postalAddress': {'addressLocality': 'Amsterdam', 'addressCountry': 'Netherlands'}},
             'jobUrl': 'https://jobs.ashbyhq.com/x/a1', 'descriptionPlain': 'desc', 'publishedAt': '2026-10-21T08:17:48.381+00:00'},
            {'id': 'a2', 'title': 'Hidden', 'isListed': False}]}
        recs, _ = run(collect(ats.AshbySource('ashby:x', {'board': 'x', 'name': 'X'}), make_http(lambda r: httpx.Response(200, json=payload))))
        self.assertEqual([r.source_job_id for r in recs], ['a1'])
        self.assertIn('Amsterdam, Netherlands', recs[0].locations)
        self.assertEqual(recs[0].remote_hint, 'Hybrid')

    def test_smartrecruiters_country_filter_pagination_and_detail(self):
        def handler(request):
            if '/postings/' in request.url.path:
                return httpx.Response(200, json={'jobAd': {'sections': {'jobDescription': {'text': '<p>LLM work</p>'},
                                                                         'qualifications': {'text': 'Python'}}},
                                                 'postingUrl': 'https://jobs.smartrecruiters.com/KPN/1', 'applyUrl': 'https://a'})
            self.assertEqual(request.url.params.get('country'), 'nl')
            return httpx.Response(200, json={'totalFound': 1, 'content': [{
                'id': '1', 'name': 'AI Engineer', 'ref': 'https://api.smartrecruiters.com/v1/companies/KPN/postings/1',
                'company': {'name': 'KPN'}, 'releasedDate': '2026-11-04T14:41:47.608Z',
                'location': {'city': 'Amsterdam', 'country': 'nl', 'hybrid': True, 'fullLocation': 'Amsterdam, NH, Netherlands'}}]})
        recs, _ = run(collect(ats.SmartRecruitersSource('smartrecruiters:KPN', {'company': 'KPN'}), make_http(handler)))
        self.assertEqual(recs[0].description, 'LLM work\nPython')
        self.assertEqual((recs[0].remote_hint, recs[0].country), ('hybrid', 'NL'))

    def test_workable_recruitee_personio_teamtailor(self):
        workable = {'name': 'HF', 'jobs': [{'shortcode': 'AB12', 'title': 'ML Engineer', 'url': 'https://apply.workable.com/hf/j/AB12/',
                                            'city': 'Amsterdam', 'country': 'Netherlands', 'telecommuting': True, 'description': '<p>x</p>'}]}
        recs, _ = run(collect(ats.WorkableSource('workable:hf', {'account': 'hf'}), make_http(lambda r: httpx.Response(200, json=workable))))
        self.assertEqual((recs[0].source_job_id, recs[0].remote_hint, recs[0].employer), ('AB12', 'remote', 'HF'))

        recruitee = {'offers': [{'id': 5, 'title': 'AI Engineer', 'careers_url': 'https://x.recruitee.com/o/ai', 'city': 'Utrecht',
                                 'country': 'Netherlands', 'country_code': 'NL', 'remote': False, 'hybrid': True,
                                 'description': '<p>a</p>', 'requirements': '<ul><li>b</li></ul>', 'published_at': '2026-10-30 16:32:58 UTC'}]}
        recs, _ = run(collect(ats.RecruiteeSource('recruitee:x', {'company': 'x'}), make_http(lambda r: httpx.Response(200, json=recruitee))))
        self.assertEqual((recs[0].description, recs[0].remote_hint, recs[0].published_at), ('a\nb', 'hybrid', '2026-10-30T16:32:58+00:00'))

        xml = ('<?xml version="1.0"?><workzag-jobs><position><id>7</id><subcompany>Ohpen</subcompany><office>Amsterdam</office>'
               '<additionalOffices><office>Utrecht</office></additionalOffices><name>AI Engineer</name><jobDescriptions>'
               '<jobDescription><name>Role</name><value><![CDATA[<p>LLM</p>]]></value></jobDescription></jobDescriptions>'
               '<createdAt>2026-10-01T10:00:00+00:00</createdAt></position></workzag-jobs>')
        recs, _ = run(collect(ats.PersonioSource('personio:ohpen', {'company': 'ohpen'}), make_http(lambda r: httpx.Response(200, text=xml))))
        self.assertEqual((recs[0].url, recs[0].locations, recs[0].description), ('https://ohpen.jobs.personio.de/job/7', ['Amsterdam', 'Utrecht'], 'Role\nLLM'))

        rss = ('<rss xmlns:tt="https://teamtailor.com/locations"><channel><item><title>ML Engineer</title><description>&lt;p&gt;x&lt;/p&gt;</description>'
               '<pubDate>Mon, 02 Nov 2026 16:47:11 +0100</pubDate><link>https://jobs.x.com/jobs/865-ml</link><remoteStatus>hybrid</remoteStatus>'
               '<tt:locations><tt:location><tt:city>Eindhoven</tt:city><tt:country>Netherlands</tt:country></tt:location></tt:locations></item></channel></rss>')
        recs, _ = run(collect(ats.TeamtailorSource('teamtailor:x', {'subdomain': 'x'}), make_http(lambda r: httpx.Response(200, text=rss))))
        self.assertEqual((recs[0].source_job_id, recs[0].location, recs[0].remote_hint), ('865', 'Eindhoven, Netherlands', 'hybrid'))

    def test_workday_is_opt_in_and_query_based(self):
        src = ats.WorkdaySource('workday:p', {'tenant': 'p', 'site': 's'})
        self.assertFalse(src.enabled)
        self.assertFalse(src.complete_listing)

        def handler(request):
            if request.method == 'POST':
                body = json.loads(request.content)
                posts = [{'title': 'AI Engineer', 'externalPath': '/job/Eindhoven/AI-Engineer_R1', 'locationsText': 'Eindhoven', 'bulletFields': ['R1']}] \
                    if body['offset'] == 0 else []
                return httpx.Response(200, json={'total': 1, 'jobPostings': posts})
            return httpx.Response(200, json={'jobPostingInfo': {'jobDescription': '<p>ML</p>', 'location': 'Eindhoven',
                                                                'startDate': '2026-10-20', 'jobReqId': 'R1', 'timeType': 'Full time'}})
        src = ats.WorkdaySource('workday:p', {'tenant': 'p', 'site': 's', 'enabled': True, 'queries': ['ml', 'ai']})
        recs, _ = run(collect(src, make_http(handler)))
        self.assertEqual(len(recs), 1)  # same posting from two queries is yielded once
        self.assertEqual((recs[0].source_job_id, recs[0].description), ('R1', 'ML'))

    def test_board_apis(self):
        cases = [
            (boards.ArbeitnowSource('arbeitnow', {}), {'data': [{'slug': 's1', 'title': 'AI Engineer', 'company_name': 'Tebi', 'location': 'Amsterdam',
                                                                  'remote': False, 'url': 'https://www.arbeitnow.com/jobs/s1', 'description': '<p>x</p>',
                                                                  'created_at': 1793829319}], 'links': {'next': None}}),
            (boards.RemotiveSource('remotive', {'categories': ['data']}), {'jobs': [{'id': 9, 'title': 'ML', 'url': 'https://remotive.com/9',
                                                                                     'candidate_required_location': 'Europe', 'company_name': 'C'}]}),
            (boards.RemoteOKSource('remoteok', {}), [{'legal': 'x'}, {'id': '3', 'position': 'AI Engineer', 'company': 'C', 'url': 'https://remoteok.com/3',
                                                                      'location': 'MÃ¼nchen', 'date': '2026-11-03T16:00:25+00:00'}]),
            (boards.TheMuseSource('themuse', {'categories': ['Data and Analytics'], 'max_pages': 1}),
             {'page_count': 1, 'results': [{'id': 1, 'name': 'Senior Applied AI Engineer', 'locations': [{'name': 'Amsterdam, Netherlands'}],
                                            'company': {'name': 'X'}, 'refs': {'landing_page': 'https://themuse.com/1'}, 'contents': '<p>x</p>'}]}),
            (boards.JobicySource('jobicy', {'geos': ['netherlands']}), {'jobs': [{'id': 4, 'jobTitle': 'ML Engineer', 'url': 'https://jobicy.com/4',
                                                                                  'jobGeo': 'Netherlands', 'companyName': 'C', 'jobType': ['full-time']}]}),
            (boards.HimalayasSource('himalayas', {'queries': ['ml'], 'max_pages': 1}),
             {'jobs': [{'guid': 'https://himalayas.app/j/1', 'title': 'ML Engineer', 'companyName': 'C', 'locationRestrictions': ['Netherlands']}]}),
            (boards.WorkingNomadsSource('workingnomads', {}), [{'url': 'https://www.workingnomads.com/job/go/1/', 'title': 'AI Engineer', 'location': 'Europe'}]),
        ]
        for source, payload in cases:
            with self.subTest(source=source.family):
                recs, _ = run(collect(source, make_http(lambda r, p=payload: httpx.Response(200, json=p))))
                self.assertEqual(len(recs), 1)
                self.assertEqual(recs[0].kind, AGGREGATOR)
                self.assertFalse(source.complete_listing)
                self.assertTrue(recs[0].url.startswith('https://'))
        self.assertEqual(run(collect(boards.RemoteOKSource('remoteok', {}), make_http(lambda r: httpx.Response(200, json=cases[2][1]))))[0][0].location, 'München')

    def test_invalid_payloads_raise(self):
        bad = [(ats.GreenhouseSource('greenhouse:x', {'board': 'x'}), {'nope': 1}), (ats.LeverSource('lever:x', {'company': 'x'}), {'a': 1}),
               (ats.AshbySource('ashby:x', {'board': 'x'}), []), (ats.RecruiteeSource('recruitee:x', {'company': 'x'}), {'x': 1}),
               (boards.ArbeitnowSource('arbeitnow', {}), {'x': 1}), (boards.RemoteOKSource('remoteok', {}), {'x': 1})]
        for source, payload in bad:
            with self.subTest(source=source.key), self.assertRaises(InvalidResponse):
                run(collect(source, make_http(lambda r, p=payload: httpx.Response(200, json=p))))
        with self.assertRaises(InvalidResponse):
            run(collect(ats.PersonioSource('personio:x', {'company': 'x'}), make_http(lambda r: httpx.Response(200, text='<broken'))))

    def test_optional_sources_need_credentials(self):
        with mock.patch.dict(os.environ, {'ADZUNA_APP_ID': '', 'ADZUNA_APP_KEY': '', 'JOOBLE_API_KEY': ''}):
            self.assertIn('ADZUNA_APP_ID', boards.AdzunaSource('adzuna', {}).missing_config())
            self.assertIn('JOOBLE_API_KEY', boards.JoobleSource('jooble', {}).missing_config())
        with mock.patch.dict(os.environ, {'ADZUNA_APP_ID': 'i', 'ADZUNA_APP_KEY': 'k'}):
            self.assertIsNone(boards.AdzunaSource('adzuna', {}).missing_config())

            def handler(request):
                self.assertEqual(request.url.params.get('app_id'), 'i')
                return httpx.Response(200, json={'results': [{'id': 1, 'title': 'ML Engineer', 'redirect_url': 'https://adzuna.nl/1',
                                                              'company': {'display_name': 'C'}, 'location': {'display_name': 'Utrecht'}}]})
            recs, _ = run(collect(boards.AdzunaSource('adzuna', {'queries': ['ml'], 'max_pages': 1}), make_http(handler)))
            self.assertEqual((recs[0].country, recs[0].location), ('NL', 'Utrecht'))


# --------------------------------------------------------------------------- normalization + relevance

class NormalizeTests(unittest.TestCase):
    def test_locations(self):
        cases = {'Amsterdam': ('randstad', 'Amsterdam'), 'Den Haag': ('randstad', 'Den Haag'), "'s-Gravenhage": ('randstad', "'S-Gravenhage"),
                 'Netherlands': ('nl_unspecified', None), 'Eindhoven, Noord-Brabant, Nederland': ('nl_other', 'Eindhoven'),
                 'Remote - Europe': ('remote_europe', None), 'Remote, US': ('foreign', None), 'Remote': ('remote_unspecified', None),
                 'Berlin, Germany': ('foreign', None), 'San Francisco, CA': ('foreign', None), 'London | Amsterdam': ('randstad', 'Amsterdam'),
                 'Anywhere': ('remote_global', None), 'Remote (EU/UK)': ('remote_europe', None), '': ('unknown', None)}
        for text, (region, city) in cases.items():
            with self.subTest(text=text):
                loc = normalize.parse_location(text)
                self.assertEqual((loc.region, loc.city), (region, city))
        self.assertEqual(normalize.parse_location('', 'NL').region, 'nl_unspecified')
        self.assertEqual(normalize.parse_location('Utrecht', remote_hint='hybrid').remote_type, 'hybrid')

    def test_employer_and_title_keys(self):
        self.assertEqual(normalize.employer_key('Booking.com B.V.'), normalize.employer_key('booking.com'))
        self.assertEqual(normalize.employer_key('ABN AMRO Bank N.V.'), 'abnamro')
        self.assertEqual(normalize.employer_key('MessageBird'), 'bird')
        self.assertIsNone(normalize.employer_key(None))
        self.assertEqual(normalize.title_key('Senior Machine Learning Engineer (m/f/d)'), normalize.title_key('Sr. ML Engineer - Amsterdam'))
        self.assertNotEqual(normalize.seniority('Senior ML Engineer'), normalize.seniority('ML Engineer'))
        self.assertNotEqual(normalize.seniority('ML Engineer II'), normalize.seniority('ML Engineer III'))

    def test_urls_and_ats_ids(self):
        self.assertEqual(normalize.canonical_url('https://job-boards.greenhouse.io/a/jobs/1?gh_src=x&utm_source=y'),
                         normalize.canonical_url('https://boards.greenhouse.io/a/jobs/1/'))
        self.assertEqual(normalize.canonical_url('https://jobs.lever.co/x/abc/apply?lever-source=LinkedIn'), 'https://jobs.lever.co/x/abc')
        self.assertEqual(normalize.ats_key('https://www.adyen.com/careers?gh_jid=7586281'), 'greenhouse:7586281')
        self.assertEqual(normalize.ats_key('https://boards.greenhouse.io/adyen/jobs/7586281'), 'greenhouse:7586281')
        self.assertIsNone(normalize.ats_key('https://example.com/careers/1'))

    def test_dates_and_language(self):
        self.assertEqual(normalize.to_iso(1793692203224), '2026-11-03T07:50:03+00:00')
        self.assertEqual(normalize.to_iso('Mon, 02 Nov 2026 16:47:11 +0100'), '2026-11-02T15:47:11+00:00')
        self.assertIsNone(normalize.to_iso('not a date'))
        self.assertEqual(normalize.language_requirement('X', 'You are fluent in Dutch. We build things with the team for you.'), 'dutch_required')
        self.assertEqual(normalize.language_requirement('X', 'Dutch is a plus. You will work with the team on our platform.'), 'dutch_preferred')
        self.assertEqual(normalize.language_requirement('X', 'Wij zoeken een ervaren engineer voor het team. Je werkt met de nieuwste '
                                                             'technologie en jouw ervaring is van belang voor onze klanten.'), 'dutch_likely')
        self.assertIsNone(normalize.language_requirement('X', ''))


class RelevanceTests(unittest.TestCase):
    def test_target_roles(self):
        primary = ['Senior AI Engineer', 'Applied AI Engineer', 'Machine Learning Engineer', 'Senior Machine Learning Engineer',
                   'LLM Engineer', 'Generative AI Engineer', 'Agentic AI Engineer', 'AI Scientist', 'Applied Scientist',
                   'NLP Engineer', 'AI Solutions Engineer', 'AI Research Scientist', 'Foundation Model Engineer']
        for t in primary:
            self.assertEqual(relevance.role_tier(t, '')[0], 'primary', t)
        for t in ['MLOps Engineer', 'LLMOps Engineer', 'AI Platform Engineer', 'Technical AI Architect']:
            self.assertEqual(relevance.role_tier(t, '')[0], 'secondary', t)

    def test_context_dependent_titles(self):
        self.assertEqual(relevance.role_tier('Research Engineer', AI_DESC)[0], 'primary')
        self.assertEqual(relevance.role_tier('Research Engineer', 'Turbine design and CAD.')[0], 'none')
        self.assertEqual(relevance.role_tier('Data Scientist', AI_DESC)[0], 'secondary')
        self.assertEqual(relevance.role_tier('Data Scientist', 'Excel reporting')[0], 'none')
        self.assertEqual(relevance.role_tier('Python Developer', 'Django CRUD apps')[0], 'none')
        self.assertEqual(relevance.role_tier('AI Consultant', 'Strategy slide decks only.')[0], 'none')
        self.assertEqual(relevance.role_tier('Software Engineer', AI_DESC)[0], 'possible')  # unusual title kept

    def test_exclusions(self):
        for t in ['Machine Learning Intern', 'Junior ML Engineer', 'AI Sales Engineer', 'AI Trainer - Dutch', 'PhD Candidate in NLP',
                  'Senior Product Manager - Machine Learning', 'Senior AI Content Specialist', 'Werkstudent Data Science']:
            self.assertEqual(relevance.role_tier(t, AI_DESC)[0], 'none', t)
        self.assertEqual(relevance.role_tier('Senior Backend Developer (Java, Agentic LLMs)', '')[0], 'secondary')

    def test_location_eligibility(self):
        ok = lambda text, desc='': relevance.location_eligible(normalize.parse_location(text), desc)[0]
        self.assertTrue(ok('Amsterdam'))
        self.assertTrue(ok('Remote - Europe'))
        self.assertTrue(ok('Eindhoven'))
        self.assertFalse(relevance.location_eligible(normalize.parse_location('Eindhoven'), '', {'accept_nl_outside_randstad': False})[0])
        self.assertFalse(ok('Berlin, Germany'))
        self.assertTrue(relevance.location_eligible(normalize.parse_location('Berlin, Germany'), '', {'allow_relocation': True})[0])
        self.assertFalse(ok('Remote'))
        self.assertTrue(ok('Remote', 'Open to candidates in the EU.'))
        self.assertFalse(ok('Remote, US'))
        self.assertTrue(ok('', 'Our office in Utrecht'))


# --------------------------------------------------------------------------- dedupe (pure)

class DedupeTests(unittest.TestCase):
    def cand(self, **kw):
        base = dict(job_id=1, employer_key='adyen', title_key='machine learning engineer', seniority='senior', region='randstad',
                    city='Amsterdam', description=AI_DESC, source_keys=frozenset({'greenhouse:adyen'}))
        base.update(kw)
        return dedupe.Candidate(**base)

    def same(self, cand, **kw):
        base = dict(source_key='arbeitnow', employer_key='adyen', title_key='machine learning engineer', seniority='senior',
                    region='randstad', city='Amsterdam', description=AI_DESC)
        base.update(kw)
        return dedupe.is_same_job(cand, **base)[0]

    def test_cross_source_copy_merges(self):
        self.assertTrue(self.same(self.cand()))
        self.assertTrue(self.same(self.cand(region='nl_unspecified', city=None), description=AI_DESC + ' Apply now.'))

    def test_conservative_non_merges(self):
        self.assertFalse(self.same(self.cand(), seniority=''))                         # different seniority
        self.assertFalse(self.same(self.cand(), source_key='greenhouse:adyen'))        # other requisition, same board
        self.assertFalse(self.same(self.cand(), city='Rotterdam'))                     # different location
        self.assertFalse(self.same(self.cand(), region='remote_europe', city=None))
        self.assertFalse(self.same(self.cand(), description='Completely different payments backend role in Java. ' * 20))
        self.assertFalse(self.same(self.cand(), employer_key='mollie'))
        self.assertFalse(self.same(self.cand(availability='closed')))                  # repost after closure = new job
        self.assertFalse(self.same(self.cand(description='', city=None, region='nl_unspecified'), description=''))  # too little evidence


# --------------------------------------------------------------------------- store, migration, engine

class FakeSource(Source):
    family = 'fake'

    def __init__(self, key, records=None, error=None, kind='direct', complete=True, **options):
        super().__init__(key, options)
        self.records, self.error, self.kind, self.complete_listing = list(records or []), error, kind, complete
        self.calls = 0

    async def fetch(self, http, ctx):
        self.calls += 1
        if self.error:
            raise self.error
        for r in self.records:
            yield copy.deepcopy(r)  # real adapters build fresh records on every fetch


def rec(source_key='greenhouse:acme', job_id='1', title='Senior Machine Learning Engineer', employer='Acme', location='Amsterdam',
        description=AI_DESC, url=None, apply_url=None, kind='direct', marker='m1', published=None):
    return JobRecord(source_key=source_key, source_job_id=job_id, title=title, employer=employer, location=location,
                     description=description, url=url or f'https://boards.greenhouse.io/acme/jobs/{job_id}', apply_url=apply_url,
                     kind=kind, updated_marker=marker, published_at=published)


def test_cfg(**notif):
    cfg = {'settings': dict(dconfig.DEFAULT_SETTINGS), 'notifications': {**dconfig.DEFAULT_NOTIFICATIONS, 'min_score': 25, **notif},
           'sources': {}, 'matching': {'matching': {'titles_any': ['Machine Learning Engineer', 'AI Engineer'],
                                                    'keywords_any': ['LLM', 'Python', 'RAG'],
                                                    'locations_any': ['Netherlands', 'Amsterdam', 'Remote', 'Europe'],
                                                    'excluded_terms': ['intern'], 'min_score': 25, 'max_age_days': 30}}}
    return cfg


class EngineTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        self.sent = []

        async def job(job_id, j):
            self.sent.append(('job', job_id))

        async def text(t):
            self.sent.append(('digest', t))
        self.notifier = {'job': job, 'text': text}
        self.queued = []

    def discover(self, sources, cfg=None, force=True):
        return run(engine.run_discovery(on_new_job=lambda j, s: self.queued.append((j, s)), sources=sources, cfg=cfg or test_cfg(),
                                        http=make_http(lambda r: httpx.Response(500)), notifier=self.notifier, force=force))

    def jobs(self):
        with core.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM jobs ORDER BY id')]

    def links(self):
        with core.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM job_sources ORDER BY id')]

    def test_repeated_scans_are_idempotent(self):
        src = FakeSource('greenhouse:acme', [rec(job_id='1'), rec(job_id='2', title='AI Engineer')])
        first = self.discover([src])
        second = self.discover([src])
        self.assertEqual((first['new'], second['new'], second['unchanged']), (2, 0, 2))
        self.assertEqual(len(self.jobs()), 2)
        self.assertEqual(len(self.links()), 2)

    def test_cross_source_duplicates_consolidate_with_provenance(self):
        direct = FakeSource('greenhouse:acme', [rec(job_id='77')])
        board = FakeSource('arbeitnow', [rec(source_key='arbeitnow', job_id='slug-1', employer='Acme B.V.', location='Amsterdam, Netherlands',
                                             url='https://www.arbeitnow.com/jobs/slug-1', kind=AGGREGATOR, description=AI_DESC + ' Apply.')],
                           kind=AGGREGATOR, complete=False)
        summary = self.discover([board, direct])
        jobs = self.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(summary['duplicates'], 1)
        self.assertEqual(jobs[0]['source_count'], 2)
        self.assertEqual(jobs[0]['source'], 'greenhouse:acme')            # employer's own posting preferred
        self.assertIn('greenhouse.io', jobs[0]['url'])
        self.assertEqual(len(self.links()), 2)
        self.assertEqual(len(self.queued), 1)                              # one CV queue call, not two

    def test_ats_id_in_aggregator_url_merges(self):
        direct = FakeSource('greenhouse:acme', [rec(job_id='555')])
        copy = rec(source_key='jobicy', job_id='j9', employer='Someone Else', title='ML Engineer', kind=AGGREGATOR,
                   url='https://jobicy.com/9', apply_url='https://acme.com/careers?gh_jid=555')
        self.discover([direct, FakeSource('jobicy', [copy], kind=AGGREGATOR, complete=False)])
        self.assertEqual(len(self.jobs()), 1)

    def test_canonical_url_merge_ignores_tracking(self):
        a = rec(source_key='themuse', job_id='m1', kind=AGGREGATOR, url='https://jobs.lever.co/acme/abc?lever-source=muse', employer='Other')
        b = rec(source_key='remotive', job_id='r1', kind=AGGREGATOR, url='https://jobs.lever.co/acme/abc/apply?utm_source=x', employer='Other2')
        self.discover([FakeSource('themuse', [a], complete=False), FakeSource('remotive', [b], complete=False)])
        self.assertEqual(len(self.jobs()), 1)

    def test_similar_but_distinct_jobs_stay_separate(self):
        recs_ = [rec(job_id='1'), rec(job_id='2'),                                     # two requisitions, same title, same board
                 rec(job_id='3', title='Machine Learning Engineer'),                   # different seniority
                 rec(job_id='4', location='Rotterdam')]                                # different city
        self.discover([FakeSource('greenhouse:acme', recs_)])
        self.assertEqual(len(self.jobs()), 4)

    def test_irrelevant_and_foreign_jobs_rejected(self):
        src = FakeSource('greenhouse:acme', [rec(job_id='1', title='Account Executive'), rec(job_id='2', location='Berlin, Germany'),
                                             rec(job_id='3', title='Machine Learning Intern'), rec(job_id='4')])
        summary = self.discover([src])
        self.assertEqual((summary['new'], summary['rejected']), (1, 3))

    def test_stale_aggregator_postings_rejected_but_direct_kept(self):
        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        agg = FakeSource('arbeitnow', [rec(source_key='arbeitnow', job_id='a', kind=AGGREGATOR, published=old, url='https://a.test/1')],
                         kind=AGGREGATOR, complete=False)
        direct = FakeSource('greenhouse:acme', [rec(job_id='d', published=old, title='AI Engineer')])
        summary = self.discover([agg, direct])
        self.assertEqual(summary['new'], 1)
        self.assertEqual(self.jobs()[0]['source'], 'greenhouse:acme')

    def test_source_failure_does_not_stop_others_and_is_recorded(self):
        good = FakeSource('greenhouse:good', [rec(source_key='greenhouse:good', job_id='1')])
        bad = FakeSource('greenhouse:bad', error=SourceError('HTTP 500 from x'))
        limited = FakeSource('arbeitnow', error=SourceRateLimited('HTTP 429 from y'), complete=False)
        summary = self.discover([bad, limited, good])
        self.assertEqual(summary['new'], 1)
        with core.connect() as db:
            health = {r['source_key']: dict(r) for r in db.execute('SELECT * FROM source_health')}
        self.assertEqual(health['greenhouse:bad']['status'], 'failed')
        self.assertIn('HTTP 500', health['greenhouse:bad']['last_error'])
        self.assertEqual(health['arbeitnow']['status'], 'rate_limited')
        self.assertEqual(health['greenhouse:good']['status'], 'ok')
        self.assertEqual(health['greenhouse:good']['new_unique'], 1)
        self.assertEqual(health['greenhouse:bad']['consecutive_failures'], 1)

    def test_health_statuses_disabled_not_configured_skipped_empty(self):
        disabled = FakeSource('workday:x', enabled=False)
        needs_key = boards.AdzunaSource('adzuna', {})
        empty = FakeSource('greenhouse:empty', [])
        with mock.patch.dict(os.environ, {'ADZUNA_APP_ID': '', 'ADZUNA_APP_KEY': ''}):
            self.discover([disabled, needs_key, empty])
            interval = FakeSource('greenhouse:empty', [], interval_minutes=60)
            self.discover([interval], force=False)  # attempted moments ago -> skipped
        with core.connect() as db:
            health = {r['source_key']: r['status'] for r in db.execute('SELECT * FROM source_health')}
        self.assertEqual(health, {'workday:x': 'disabled', 'adzuna': 'not_configured', 'greenhouse:empty': 'skipped'})
        self.assertEqual(interval.calls, 0)
        self.assertEqual(disabled.calls, 0)

    def test_lifecycle_close_after_two_complete_scans_and_reopen(self):
        src = FakeSource('greenhouse:acme', [rec(job_id='1'), rec(job_id='2', title='AI Engineer')])
        self.discover([src])
        src.records = [rec(job_id='1')]
        self.discover([src])
        self.assertEqual([j['availability'] for j in self.jobs()], ['active', 'active'])   # one miss: still active
        self.discover([src])
        self.assertEqual([j['availability'] for j in self.jobs()], ['active', 'closed'])
        src.records = [rec(job_id='1'), rec(job_id='2', title='AI Engineer')]
        self.discover([src])
        self.assertEqual([j['availability'] for j in self.jobs()], ['active', 'active'])   # reappeared: reopened

    def test_failed_or_truncated_scans_never_close_jobs(self):
        src = FakeSource('greenhouse:acme', [rec(job_id='1')])
        self.discover([src])
        src.error = SourceError('HTTP 503')
        for _ in range(3):
            self.discover([src])
        src.error, src.records = None, []
        for _ in range(3):
            self.discover([src])       # an empty listing is not treated as evidence of closure
        self.assertEqual(self.jobs()[0]['availability'], 'active')
        search = FakeSource('arbeitnow', [rec(source_key='arbeitnow', job_id='a', kind=AGGREGATOR, url='https://a.test/1',
                                              title='NLP Engineer', employer='Other Co')], kind=AGGREGATOR, complete=False)
        self.discover([search])
        search.records = []
        for _ in range(3):
            self.discover([search])
        self.assertEqual(self.jobs()[1]['availability'], 'active')   # search sources only go stale by age

    def test_stale_after_days_for_search_only_jobs(self):
        self.discover([FakeSource('arbeitnow', [rec(source_key='arbeitnow', job_id='a', kind=AGGREGATOR, url='https://a.test/1')],
                                  kind=AGGREGATOR, complete=False)])
        with core.connect() as db:
            db.execute("UPDATE jobs SET last_seen_at='2020-01-01T00:00:00+00:00'")
            self.assertEqual(store.mark_stale(db, 21), 1)
        self.assertEqual(self.jobs()[0]['availability'], 'stale')

    def test_notifications_dedup_cap_and_baseline_digest(self):
        # first scan of a new source -> one digest, no per-job messages
        self.discover([FakeSource('greenhouse:acme', [rec(job_id=str(i), title='AI Engineer', location=c)
                                                      for i, c in enumerate(['Amsterdam', 'Utrecht', 'Delft'])])])
        self.assertEqual([k for k, _ in self.sent], ['digest'])
        self.sent.clear()
        # later scans: individual messages for genuinely new jobs only (a duplicate is not re-announced)
        src = FakeSource('greenhouse:acme', [rec(job_id='new1', title='AI Engineer', location='Leiden'),
                                             rec(job_id='0', title='AI Engineer', location='Amsterdam')])
        self.discover([src])
        self.assertEqual([k for k, _ in self.sent], ['job'])
        self.sent.clear()
        self.discover([src])
        self.assertEqual(self.sent, [])
        # more than the cap -> a single digest
        many = FakeSource('greenhouse:acme', [rec(job_id=f'x{i}', title='AI Engineer', location=c)
                                              for i, c in enumerate(['Haarlem', 'Hilversum', 'Almere', 'Zeist'])])
        self.discover([many], cfg=test_cfg(max_individual_per_run=2))
        self.assertEqual([k for k, _ in self.sent], ['digest'])

    def test_cv_queue_hook_only_for_new_relevant_jobs(self):
        src = FakeSource('greenhouse:acme', [rec(job_id='1', title='AI Engineer'), rec(job_id='2', title='Software Engineer')])
        self.discover([src])
        self.discover([src])
        tiers = {j['id']: j['relevance_tier'] for j in self.jobs()}
        self.assertEqual([tiers[j] for j, _ in self.queued], ['primary'])   # 'possible' tier never queued; no repeats
        self.assertGreaterEqual(self.queued[0][1], 70)

    def test_rejected_detail_postings_are_not_refetched_until_changed(self):
        class DetailSource(FakeSource):
            details = 0

            async def detail(self, http, record):
                DetailSource.details += 1
                record.description, record.needs_detail = 'Payments backend in Java only.', False
                return record
        stub = lambda marker: JobRecord(source_key='greenhouse:acme', source_job_id='9', title='Software Engineer',
                                        url='https://boards.greenhouse.io/acme/jobs/9', location='Amsterdam',
                                        updated_marker=marker, needs_detail=True)
        src = DetailSource('greenhouse:acme', [stub('v1')])
        first = self.discover([src])
        second = self.discover([src])
        self.assertEqual((DetailSource.details, first['detail_fetches'], second['rejected_cached']), (1, 1, 1))
        src.records = [stub('v2')]          # posting changed -> fetched and evaluated again
        self.discover([src])
        self.assertEqual(DetailSource.details, 2)

    def test_record_errors_are_isolated(self):
        class Boom(FakeSource):
            async def fetch(self, http, ctx):
                yield rec(job_id='ok1')
                yield JobRecord(source_key=self.key, source_job_id='bad', title='AI Engineer', url='x', description=12345)  # malformed
                yield rec(job_id='ok2', title='AI Engineer')
        summary = self.discover([Boom('greenhouse:acme')])
        self.assertEqual(summary['new'], 2)
        with core.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM source_health").fetchone()[0], 'partial')


class ApiIntegrationTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        from fastapi.testclient import TestClient
        from app import auth, main
        self.main, self.auth = main, auth
        self.client = TestClient(main.app, base_url='https://testserver', follow_redirects=False)
        self.client.cookies.set(auth.COOKIE_NAME, auth.new_session())
        direct = FakeSource('greenhouse:acme', [rec(job_id='1', title='AI Engineer'), rec(job_id='2', title='NLP Engineer', location='Utrecht')])
        board = FakeSource('arbeitnow', [rec(source_key='arbeitnow', job_id='s', employer='Acme', title='AI Engineer', kind=AGGREGATOR,
                                             url='https://www.arbeitnow.com/jobs/s', description=AI_DESC + ' Apply.')],
                           kind=AGGREGATOR, complete=False)
        failing = FakeSource('jooble', error=SourceError('HTTP 403 from jooble.org'), complete=False)
        run(engine.run_discovery(sources=[direct, board, failing], cfg=test_cfg(), http=make_http(lambda r: httpx.Response(500)),
                                 notifier={'job': mock.AsyncMock(), 'text': mock.AsyncMock()}, force=True))

    def test_jobs_api_exposes_provenance_and_hides_closed(self):
        data = self.client.get('/api/jobs').json()
        self.assertEqual(len(data), 2)
        ai = next(j for j in data if j['title'] == 'AI Engineer')
        self.assertEqual(sorted(s['source'] for s in ai['sources']), ['arbeitnow', 'greenhouse:acme'])
        self.assertEqual((ai['relevance_tier'], ai['availability'], ai['location_region']), ('primary', 'active', 'randstad'))
        self.assertIn('cv_status', ai)
        with core.connect() as db:
            db.execute("UPDATE jobs SET availability='closed' WHERE title='NLP Engineer'")
        self.assertEqual(len(self.client.get('/api/jobs').json()), 1)
        self.assertEqual(len(self.client.get('/api/jobs?include_inactive=true').json()), 2)

    def test_health_api_requires_login_and_reports_failures(self):
        anon = type(self.client)(self.main.app, base_url='https://testserver')
        self.assertEqual(anon.get('/api/discovery/health').status_code, 401)
        h = self.client.get('/api/discovery/health').json()
        by = {s['source_key']: s for s in h['sources']}
        self.assertEqual(by['jooble']['status'], 'failed')
        self.assertEqual(by['greenhouse:acme']['new_unique'], 2)
        self.assertEqual(h['last_run']['new_unique'], 2)
        self.assertEqual(h['last_run']['duplicates'], 1)
        self.assertNotIn(os.environ['DASHBOARD_TOKEN'], json.dumps(h))

    def test_cv_backfill_skips_closed_jobs(self):
        from app import cv_pipeline
        with core.connect() as db:
            db.execute("UPDATE jobs SET score=90")
            db.execute("UPDATE jobs SET availability='closed' WHERE title='NLP Engineer'")
        with mock.patch.dict(os.environ, {'CV_MIN_SCORE': '70', 'CV_AUTO_GENERATE': 'true'}):
            self.assertEqual(cv_pipeline.backfill(), 1)


class MigrationTests(unittest.TestCase):
    def test_legacy_database_migrates_without_losing_data(self):
        path = Path(_TMP.name) / 'legacy.sqlite3'
        legacy = sqlite3.connect(path)
        legacy.executescript('''
            CREATE TABLE jobs (id INTEGER PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL, company TEXT NOT NULL,
                title TEXT NOT NULL, location TEXT, description TEXT, url TEXT NOT NULL, apply_url TEXT, published_at TEXT,
                discovered_at TEXT NOT NULL, score INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'new', cv_path TEXT,
                UNIQUE(source,source_id));
            CREATE TABLE events (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, action TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL);
            INSERT INTO jobs VALUES (1,'lever','abc','jobgether','AI Engineer','Netherlands','d','https://jobs.lever.co/jobgether/abc',
                                     NULL,'1793692203224','2026-11-04T15:42:28+00:00',70,'approved','/x/job-1.html');
            INSERT INTO events VALUES (1,1,'approved','Telegram action','2026-11-04T16:00:00+00:00');
        ''')
        legacy.commit()
        legacy.close()
        with mock.patch.object(store, 'DB', path), mock.patch.object(core, 'DB', path):
            core.init_db()
            core.init_db()  # second call is a no-op
            db = sqlite3.connect(path)
            db.row_factory = sqlite3.Row
            job = dict(db.execute('SELECT * FROM jobs').fetchone())
            link = dict(db.execute('SELECT * FROM job_sources').fetchone())
            self.assertEqual((job['status'], job['cv_path'], job['score'], job['availability']), ('approved', '/x/job-1.html', 70, 'active'))
            self.assertEqual((link['source_key'], link['source_job_id'], link['kind']), ('lever:jobgether', 'abc', 'aggregator'))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 1)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], store.SCHEMA_VERSION)
            self.assertIsNotNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='rejected_postings'").fetchone())
            health = db.execute("SELECT last_success_at FROM source_health WHERE source_key='lever:jobgether'").fetchone()
            self.assertIsNotNone(health[0])  # existing board is not treated as a first scan (no digest flood)
            db.close()
        self.assertEqual(len(list((path.parent / 'backups').glob('legacy-pre-discovery-v*.sqlite3'))), 1)

    def test_rescan_of_migrated_legacy_job_is_not_a_duplicate(self):
        reset_db()
        with core.connect() as db:
            db.execute("INSERT INTO jobs (source,source_id,company,title,location,url,discovered_at,score,status) VALUES "
                       "('lever','abc','jobgether','AI Engineer','Netherlands','https://jobs.lever.co/jobgether/abc','2026-11-04',70,'approved')")
            store._backfill_links(db)
        src = FakeSource('lever:jobgether', [rec(source_key='lever:jobgether', job_id='abc', title='AI Engineer', employer=None,
                                                 location='Netherlands', url='https://jobs.lever.co/jobgether/abc', kind=AGGREGATOR)])
        summary = run(engine.run_discovery(sources=[src], cfg=test_cfg(), http=make_http(lambda r: httpx.Response(500)),
                                           notifier={'job': mock.AsyncMock(), 'text': mock.AsyncMock()}, force=True))
        with core.connect() as db:
            rows = db.execute('SELECT status FROM jobs').fetchall()
        self.assertEqual((summary['new'], len(rows), rows[0][0]), (0, 1, 'approved'))


class ConfigTests(unittest.TestCase):
    def test_repository_config_builds_and_merges_legacy_boards(self):
        with mock.patch.object(dconfig, '_read', side_effect=lambda f: dconfig.load_yaml(f) if f == 'discovery.yaml' else
                               {'sources': {'lever': ['jobgether', 'sambatv'], 'greenhouse': ['newco']}, 'matching': {'min_score': 25}}):
            cfg = dconfig.load()
        sources, errors = dconfig.build_sources(cfg)
        keys = {s.key for s in sources}
        self.assertEqual(errors, [])
        self.assertIn('lever:sambatv', keys)
        self.assertIn('greenhouse:newco', keys)
        self.assertEqual(sum(1 for s in sources if s.key == 'lever:jobgether'), 1)   # not duplicated
        jobgether = next(s for s in sources if s.key == 'lever:jobgether')
        self.assertEqual((jobgether.kind, jobgether.locations), (AGGREGATOR, ['Netherlands']))
        self.assertTrue(all(not s.enabled for s in sources if s.family == 'workday'))  # opt-in
        self.assertEqual(cfg['notifications']['min_score'], 25)
        self.assertGreaterEqual(len(sources), 60)


if __name__ == '__main__':
    unittest.main()
