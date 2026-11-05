"""Deterministic normalization helpers. Unknown values are returned as None, never guessed."""
import html
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# --------------------------------------------------------------------------- text

_BLOCK = re.compile(r'(?i)<\s*(br|/p|/li|/h\d|/div|/tr|li)\b[^>]*>')
_TAG = re.compile(r'<[^>]+>')


def clean_text(value, limit=20000):
    """HTML/markup -> plain text with line breaks preserved, capped in length."""
    if not value:
        return ''
    text = _BLOCK.sub('\n', str(value))
    text = html.unescape(_TAG.sub(' ', text))
    text = re.sub(r'[ \t\r\f\v\xa0]+', ' ', text)
    text = re.sub(r'\s*\n\s*', '\n', text).strip()
    return text[:limit]


def fix_mojibake(value):
    """Repair UTF-8 text that was decoded as Latin-1 (e.g. 'MÃ¼nchen')."""
    if value and ('Ã' in value or 'â€' in value):
        try:
            return value.encode('latin-1').decode('utf-8')
        except (UnicodeEncodeError, UnicodeDecodeError):
            return value
    return value


def strip_accents(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', value) if not unicodedata.combining(c))


# --------------------------------------------------------------------------- dates

def to_iso(value):
    """Epoch (s/ms), ISO 8601, 'YYYY-MM-DD HH:MM:SS UTC' or RFC 2822 -> ISO 8601 UTC, else None."""
    if value in (None, ''):
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
            number = float(value)
            dt = datetime.fromtimestamp(number / 1000 if number > 1e11 else number, timezone.utc)
        else:
            text = str(value).strip().replace(' UTC', '+00:00')
            try:
                dt = datetime.fromisoformat(text.replace('Z', '+00:00'))
            except ValueError:
                dt = parsedate_to_datetime(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec='seconds')
    except (ValueError, TypeError, OverflowError, OSError, IndexError):
        return None


# --------------------------------------------------------------------------- geography

# Randstad and realistic commuting locations. Multi-word names first.
RANDSTAD_PLACES = sorted([
    'amsterdam', 'amstelveen', 'almere', 'utrecht', 'hilversum', 'amersfoort', 'haarlem', 'leiden', 'den haag',
    'the hague', 'hague', "'s-gravenhage", 's-gravenhage', 'rotterdam', 'delft', 'schiphol', 'hoofddorp',
    'haarlemmermeer', 'diemen', 'zaandam', 'zaanstad', 'nieuwegein', 'zeist', 'houten', 'woerden', 'gouda',
    'zoetermeer', 'rijswijk', 'voorburg', 'leidschendam', 'alphen aan den rijn', 'capelle aan den ijssel', 'schiedam',
    'dordrecht', 'lelystad', 'huizen', 'bussum', 'naarden', 'weesp', 'purmerend', 'alkmaar', 'badhoevedorp',
    'uithoorn', 'abcoude', 'maarssen', 'bilthoven', 'de bilt', 'baarn', 'veenendaal', 'breukelen', 'ijsselstein',
    'barendrecht', 'katwijk', 'noordwijk', 'oegstgeest', 'wassenaar', 'heemstede', 'velsen', 'beverwijk', 'vianen',
    'hoorn', 'noord-holland', 'north holland', 'zuid-holland', 'south holland', 'flevoland', 'randstad',
], key=len, reverse=True)
NL_OTHER_PLACES = ['eindhoven', 'veldhoven', 'helmond', "'s-hertogenbosch", 's-hertogenbosch', 'den bosch', 'tilburg',
                   'breda', 'nijmegen', 'arnhem', 'enschede', 'groningen', 'zwolle', 'maastricht', 'apeldoorn',
                   'deventer', 'leeuwarden', 'venlo', 'wageningen', 'hengelo', 'roermond', 'heerlen', 'sittard',
                   'oss', 'emmen', 'assen', 'harderwijk', 'middelburg', 'vlissingen', 'noord-brabant', 'gelderland',
                   'overijssel', 'limburg', 'friesland', 'drenthe', 'zeeland', 'hoevelaken', 'nijkerk', 'ede']
NL_COUNTRY = re.compile(r"\b(netherlands|the netherlands|nederland|holland|nld)\b|(?:^|[\s,(/|-])nl(?:$|[\s,)/|-])", re.I)
EUROPE = re.compile(r'\b(europe|european union|eu|eea|emea|cet|cest|european time ?zones?|gmt\+1|utc\+1|utc\+01|utc\+2)\b', re.I)
WORLDWIDE = re.compile(r'\b(worldwide|anywhere|global(ly)?|work from anywhere|all countries)\b', re.I)
REMOTE = re.compile(r'\b(remote|fully remote|work from home|wfh|distributed|thuiswerk(en)?|telecommute)\b', re.I)
HYBRID = re.compile(r'\b(hybrid|hybride)\b', re.I)
FOREIGN = re.compile(r"""\b(united states|usa|u\.s\.a?|us only|us-based|america|canada|mexico|brazil|brasil|argentina|
united kingdom|uk|england|scotland|wales|ireland|germany|deutschland|france|spain|espa[nñ]a|portugal|italy|italia|poland|
belgium|belgi[eë]|luxembourg|switzerland|austria|sweden|norway|denmark|finland|czech|czechia|romania|bulgaria|greece|
turkey|t[uü]rkiye|israel|india|pakistan|china|japan|singapore|australia|new zealand|south africa|nigeria|kenya|egypt|
uae|dubai|saudi|philippines|vietnam|indonesia|malaysia|korea|taiwan|hong kong|serbia|croatia|ukraine|hungary|estonia|
latvia|lithuania|colombia|chile|peru|costa rica|slovakia|slovenia|cyprus|malta|iceland|
london|manchester|edinburgh|cambridge|oxford|berlin|munich|m[uü]nchen|hamburg|frankfurt|cologne|k[oö]ln|stuttgart|
d[uü]sseldorf|paris|lyon|dublin|madrid|barcelona|valencia|lisbon|lisboa|porto|milan|milano|rome|warsaw|krak[oó]w|
wroc[lł]aw|prague|vienna|wien|zurich|z[uü]rich|geneva|stockholm|gothenburg|copenhagen|oslo|helsinki|brussels|bruxelles|
antwerp|antwerpen|ghent|gent|leuven|tallinn|riga|vilnius|bucharest|sofia|athens|istanbul|tel aviv|new york|nyc|
san francisco|bay area|seattle|austin|boston|chicago|los angeles|denver|atlanta|miami|toronto|vancouver|montreal|
bangalore|bengaluru|hyderabad|pune|chennai|mumbai|delhi|gurgaon|noida|sydney|melbourne|tokyo|seoul|shanghai|beijing|
s[aã]o paulo|buenos aires|mexico city|cape town|lagos|nairobi|cairo)\b""".replace('\n', ''), re.I)
US_TOKEN = re.compile(r'(?<![A-Za-z])(US|U\.S\.|USA)(?![A-Za-z])')  # matched case-sensitively on raw location text
US_STATE = re.compile(r',\s*(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|'
                      r'NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)\b')


_PLACE_RE = {}


def _find_place(text, places):
    key = id(places)
    if key not in _PLACE_RE:  # one precompiled alternation per list, longest names first
        _PLACE_RE[key] = re.compile(r'(?<![\w-])(' + '|'.join(re.escape(p) for p in sorted(places, key=len, reverse=True)) + r')(?![\w-])')
    match = _PLACE_RE[key].search(text)
    return match.group(1) if match else None


@dataclass
class LocationInfo:
    region: str                 # randstad | nl_other | nl_unspecified | remote_nl | remote_europe |
                                # remote_global | remote_unspecified | foreign | unknown
    country: str = None         # 'NL' when the vacancy is (also) in the Netherlands
    city: str = None
    remote_type: str = None     # remote | hybrid | onsite | None (unknown)
    label: str = ''             # human-readable normalized location

    @property
    def in_netherlands(self):
        return self.region in ('randstad', 'nl_other', 'nl_unspecified', 'remote_nl')


def remote_type(text, hint=None):
    hint = (hint or '').lower()
    if hint in ('remote', 'fully', 'fully_remote', 'true', 'yes'):
        return 'remote'
    if hint in ('hybrid', 'partial', 'partially'):
        return 'hybrid'
    if hint in ('onsite', 'on-site', 'on_site', 'office', 'none', 'false', 'no'):
        return 'onsite' if not REMOTE.search(text or '') and not HYBRID.search(text or '') else None
    if HYBRID.search(text or ''):
        return 'hybrid'
    if REMOTE.search(text or ''):
        return 'remote'
    return None


def parse_location(location_text, country_hint=None, remote_hint=None):
    """Classify a vacancy's location(s) for a Netherlands-based candidate."""
    raw = fix_mojibake(location_text or '').strip()
    text = strip_accents(raw).lower()
    rtype = remote_type(text, remote_hint)
    hint = (country_hint or '').strip().upper()
    if hint in ('NETHERLANDS', 'NLD'):
        hint = 'NL'
    city = _find_place(text, RANDSTAD_PLACES)
    if city:
        return LocationInfo('randstad', 'NL', city.title(), rtype, _label(city, rtype))
    city = _find_place(text, NL_OTHER_PLACES)
    if city and (NL_COUNTRY.search(text) or not FOREIGN.search(text)):
        return LocationInfo('nl_other', 'NL', city.title(), rtype, _label(city, rtype))
    if NL_COUNTRY.search(text) or hint == 'NL':
        region = 'remote_nl' if rtype == 'remote' else 'nl_unspecified'
        return LocationInfo(region, 'NL', None, rtype, 'Netherlands' + (f' ({rtype})' if rtype else ''))
    if rtype == 'remote':
        if FOREIGN.search(text) or US_STATE.search(raw) or US_TOKEN.search(raw):
            if EUROPE.search(text) or WORLDWIDE.search(text):
                pass  # e.g. "Remote - Europe, UK": still open to Europe
            else:
                return LocationInfo('foreign', None, None, rtype, raw or 'Remote (outside NL)')
        if EUROPE.search(text):
            return LocationInfo('remote_europe', None, None, 'remote', 'Remote (Europe)')
        if WORLDWIDE.search(text):
            return LocationInfo('remote_global', None, None, 'remote', 'Remote (worldwide)')
        return LocationInfo('remote_unspecified', None, None, 'remote', 'Remote (region not stated)')
    if EUROPE.search(text) and not FOREIGN.search(text):
        return LocationInfo('remote_europe' if rtype == 'remote' else 'unknown', None, None, rtype, raw)
    if WORLDWIDE.search(text):
        return LocationInfo('remote_global', None, None, 'remote', 'Remote (worldwide)')
    if FOREIGN.search(text) or US_STATE.search(raw) or US_TOKEN.search(raw) or (hint and hint != 'NL'):
        return LocationInfo('foreign', hint or None, None, rtype, raw)
    return LocationInfo('unknown', None, None, rtype, raw)


def _label(city, rtype):
    name = {'den haag': 'The Hague', 'the hague': 'The Hague', 'hague': 'The Hague', "'s-gravenhage": 'The Hague',
            's-gravenhage': 'The Hague', 'noord-holland': 'Noord-Holland', 'zuid-holland': 'Zuid-Holland'}.get(city, city.title())
    return f'{name}, Netherlands' + (f' ({rtype})' if rtype else '')


def mentions_netherlands(text):
    t = strip_accents(text or '').lower()
    return bool(NL_COUNTRY.search(t) or _find_place(t, RANDSTAD_PLACES))


def mentions_europe(text):
    return bool(EUROPE.search(text or '') or mentions_netherlands(text))


# --------------------------------------------------------------------------- employer / title

_EMPLOYER_STOP = {'bv', 'b v', 'nv', 'n v', 'inc', 'incorporated', 'ltd', 'limited', 'llc', 'gmbh', 'ag', 'sa', 'sas',
                  'plc', 'corp', 'corporation', 'co', 'company', 'group', 'holding', 'holdings', 'the', 'netherlands',
                  'nederland', 'nl', 'europe', 'emea', 'international', 'technologies', 'technology', 'labs', 'careers',
                  'jobs', 'hq', 'se', 'oy', 'ab', 'as', 'aps', 'srl', 'spa'}
EMPLOYER_ALIASES = {
    'bookingcom': 'booking', 'messagebird': 'bird', 'abnamrobank': 'abnamro', 'inggroep': 'ing', 'ingbank': 'ing',
    'inggroup': 'ing', 'justeattakeawaycom': 'justeattakeaway', 'takeawaycom': 'justeattakeaway', 'bol': 'bolcom',
    'nebiusai': 'nebius', 'koninklijkephilips': 'philips', 'philipselectronics': 'philips', 'elasticco': 'elastic',
    'elasticsearch': 'elastic', 'jetbrainsamsterdam': 'jetbrains', 'grafana': 'grafana', 'datadoghq': 'datadog',
    'tomtominternational': 'tomtom', 'rabobankgroup': 'rabobank', 'coöperatieraboban': 'rabobank', 'amazonwebservices': 'amazon',
    'aws': 'amazon', 'awsemea': 'amazon', 'googlenetherlands': 'google', 'microsoftnetherlands': 'microsoft',
    'catawikibv': 'catawiki', 'adyennv': 'adyen', 'mollienl': 'mollie', 'deptagency': 'dept', 'deptdigital': 'dept',
    'ohpenbv': 'ohpen', 'crisptechnologies': 'crisp', 'flowtradersnv': 'flowtraders',
}


def employer_key(name):
    """'Adyen N.V.' -> 'adyen'; 'Booking.com B.V.' -> 'booking'. None if unknown."""
    if not name:
        return None
    text = strip_accents(fix_mojibake(name)).lower().replace('&', ' and ')
    text = re.sub(r'\b([bn])\.\s?v\.?(?=\s|$)', r' \1v ', text)       # B.V. / N.V.
    text = re.sub(r'\.(com|io|ai|nl|co)\b', r'\1', text)
    words = [w for w in re.split(r'[^a-z0-9]+', text) if w and w not in _EMPLOYER_STOP]
    key = ''.join(words)
    return EMPLOYER_ALIASES.get(key, key) or None


SENIORITY_WORDS = {
    'intern': 'intern', 'internship': 'intern', 'trainee': 'intern', 'stagiair': 'intern', 'werkstudent': 'intern',
    'junior': 'junior', 'jr': 'junior', 'graduate': 'junior', 'entry': 'junior',
    'medior': 'mid', 'mid': 'mid', 'intermediate': 'mid',
    'senior': 'senior', 'sr': 'senior', 'snr': 'senior',
    'staff': 'staff', 'principal': 'principal', 'lead': 'lead', 'head': 'head', 'director': 'director',
    'chief': 'director', 'vp': 'director', 'distinguished': 'principal', 'postdoc': 'postdoc', 'postdoctoral': 'postdoc',
    'ii': 'level2', 'iii': 'level3', 'iv': 'level4', '2': 'level2', '3': 'level3', '4': 'level4',
}
_GENDER = re.compile(r'\(\s*(?:[mfwvdxh]\s*/\s*)+[mfwvdxh]\s*\)|\b(?:[mfwvdx]/){1,3}[mfwvdx]\b|\(all genders?\)|\(f/m/d\)', re.I)
_QUALIFIER = re.compile(r'\s+[-–—|@]\s+.*$|\s*\((?:[^)]*\b(?:remote|hybrid|on-?site|amsterdam|netherlands|europe|emea|'
                        r'utrecht|rotterdam|eindhoven|the hague|den haag|berlin|london|full[- ]time|part[- ]time|'
                        r'nl|eu)\b[^)]*)\)', re.I)
_TITLE_SYNONYMS = [(r'\bml\b', 'machine learning'), (r'\bai\b', 'ai'), (r'\bgen ?ai\b', 'generative ai'),
                   (r'\bnlp\b', 'natural language processing'), (r'\bdev\b', 'developer'), (r'\beng\b', 'engineer'),
                   (r'\bswe\b', 'software engineer'), (r'\bmle\b', 'machine learning engineer')]


def seniority(title, hint=None):
    """Set of seniority markers in a title, e.g. {'senior'}, {'staff'}, {'level2'}."""
    words = re.findall(r'[a-z0-9]+', strip_accents(title or '').lower())
    found = {SENIORITY_WORDS[w] for w in words if w in SENIORITY_WORDS and not (w in ('2', '3', '4') and len(words) < 2)}
    if 'mid' in found and 'level' in words:  # "mid-level"
        found.add('mid')
    if hint:
        h = hint.lower()
        for key, value in (('intern', 'intern'), ('entry', 'junior'), ('junior', 'junior'), ('mid', 'mid'),
                           ('senior', 'senior'), ('director', 'director'), ('executive', 'director')):
            if key in h and not found:
                found.add(value)
    return frozenset(found)


def seniority_label(markers):
    order = ['intern', 'junior', 'mid', 'level2', 'level3', 'level4', 'senior', 'staff', 'lead', 'principal',
             'postdoc', 'head', 'director']
    present = [m for m in order if m in markers]
    return present[-1] if present else None


def title_key(title):
    """Normalized role title without seniority, gender markers or location qualifiers."""
    text = strip_accents(fix_mojibake(title or '')).lower()
    text = _GENDER.sub(' ', text)
    text = _QUALIFIER.sub('', text)
    text = re.sub(r'[^a-z0-9+#]+', ' ', text)
    for pattern, repl in _TITLE_SYNONYMS:
        text = re.sub(pattern, repl, text)
    words = [w for w in text.split() if w not in SENIORITY_WORDS and w not in ('level', 'the', 'a', 'an', 'and', 'of')]
    return ' '.join(words)


def employment_type(value):
    v = (value or '').lower().replace('-', '').replace('_', '').replace(' ', '')
    if not v:
        return None
    if 'intern' in v:
        return 'internship'
    if 'parttime' in v:
        return 'part_time'
    if 'contract' in v or 'freelance' in v or 'temporary' in v or 'temp' == v:
        return 'contract'
    if 'fulltime' in v or 'permanent' in v or 'vast' in v:
        return 'full_time'
    return v[:30]


# --------------------------------------------------------------------------- language

_NL_WORDS = re.compile(r'\b(de|het|een|van|voor|met|je|jij|wij|ons|onze|bij|naar|zijn|wordt|werken|ervaring|en|'
                       r'functie|vacature|kennis|binnen|jouw)\b', re.I)
_EN_WORDS = re.compile(r'\b(the|and|with|for|you|your|our|we|to|of|experience|team|will|are)\b', re.I)
_DUTCH_REQUIRED = re.compile(
    r'(fluent|fluency|native|proficient|proficiency|excellent|strong|professional|business[- ]level|good command)'
    r'[^.\n]{0,40}\bdutch\b|\bdutch\b[^.\n]{0,30}\b(required|mandatory|is a must|must|essential|fluent|native|speaking)\b|'
    r'dutch[- ]speaking|nederlandstalig|(vloeiend|uitstekende?|goede?)[^.\n]{0,30}\b(nederlands|nederlandse taal)\b|'
    r'\bnederlands(e taal)?\b[^.\n]{0,30}\b(vereist|is een must|vloeiend)', re.I)
_DUTCH_PREFERRED = re.compile(r'\bdutch\b[^.\n]{0,40}\b(plus|bonus|advantage|nice[- ]to[- ]have|preferred|beneficial|asset)\b|'
                              r'\b(plus|bonus|advantage|nice[- ]to[- ]have)\b[^.\n]{0,40}\bdutch\b|\bnederlands\b[^.\n]{0,30}\bpr[eé]\b', re.I)


def posting_language(text):
    sample = (text or '')[:4000]
    if len(sample) < 80:
        return None
    nl, en = len(_NL_WORDS.findall(sample)), len(_EN_WORDS.findall(sample))
    return 'nl' if nl > en * 1.2 else 'en'


def language_requirement(title, description):
    """dutch_required | dutch_preferred | dutch_likely (posting in Dutch) | english_ok | None (no text)."""
    text = f'{title or ""}\n{description or ""}'
    if _DUTCH_PREFERRED.search(text) and not re.search(r'\b(fluent|native|required|mandatory|must)\b[^.\n]{0,25}\bdutch\b', text, re.I):
        return 'dutch_preferred'
    if _DUTCH_REQUIRED.search(text):
        return 'dutch_required'
    lang = posting_language(description)
    if lang == 'nl':
        return 'dutch_likely'
    return 'english_ok' if lang == 'en' else None


# --------------------------------------------------------------------------- URLs and ATS identifiers

TRACKING_PARAMS = re.compile(r'^(utm_.*|gh_src|source|src|ref|refid|referrer|lever-source.*|lever-origin|trk|trackingid|'
                             r'fbclid|gclid|mc_.*|_hs.*|hsa_.*|campaign|medium|origin|oga|iis|iisn|sid|share|from)$', re.I)
KEEP_PARAMS = {'gh_jid', 'id', 'jobid', 'job_id', 'jid', 'vacancy', 'vacancyid', 'reqid', 'requisitionid', 'pid'}


def canonical_url(url):
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        return None
    host = parts.hostname.lower().removeprefix('www.')
    host = re.sub(r'^job-boards(\.eu)?\.greenhouse\.io$', 'boards.greenhouse.io', host)
    path = re.sub(r'/+$', '', parts.path) or '/'
    path = re.sub(r'/(apply|application)$', '', path)
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
                   if k.lower() in KEEP_PARAMS and not TRACKING_PARAMS.match(k))
    return urlunsplit(('https', host, path, urlencode(query), ''))


_ATS_PATTERNS = [
    (re.compile(r'greenhouse\.io/(?:embed/job_app\?for=[\w-]+&token=|[\w-]+/jobs/)(\d+)', re.I), 'greenhouse'),
    (re.compile(r'[?&]gh_jid=(\d+)', re.I), 'greenhouse'),
    (re.compile(r'jobs\.(?:eu\.)?lever\.co/[\w.-]+/([0-9a-f]{8}-[0-9a-f-]{27})', re.I), 'lever'),
    (re.compile(r'jobs\.ashbyhq\.com/[^/]+/([0-9a-f]{8}-[0-9a-f-]{27})', re.I), 'ashby'),
    (re.compile(r'(?:jobs|careers)\.smartrecruiters\.com/[\w.-]+/(\d{6,})', re.I), 'smartrecruiters'),
    (re.compile(r'apply\.workable\.com/[\w-]+/j/([0-9A-F]{6,})', re.I), 'workable'),
    (re.compile(r'([\w-]+)\.recruitee\.com/o/([\w-]+)', re.I), 'recruitee'),
    (re.compile(r'([\w-]+)\.jobs\.personio\.(?:de|com)/job/(\d+)', re.I), 'personio'),
    (re.compile(r'([\w-]+)\.wd\d+\.myworkdayjobs\.com/.*?_((?:JR|R|REQ)?-?\d{4,}(?:-\d+)?)\b', re.I), 'workday'),
]


def ats_key(url):
    """Stable ATS job identifier found in a URL, e.g. 'greenhouse:7586281'. None if not recognizable."""
    if not url:
        return None
    for pattern, name in _ATS_PATTERNS:
        match = pattern.search(url)
        if match:
            return f'{name}:' + ':'.join(g.lower() for g in match.groups())
    return None
