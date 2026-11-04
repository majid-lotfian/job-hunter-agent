"""Deterministic relevance classification for target AI/ML roles in the Netherlands.

Runs before anything expensive. Tiers:
    primary    target AI/ML/LLM engineering and applied-science roles
    secondary  adjacent roles (MLOps, AI platform, data scientist with ML, AI architect, ...)
    possible   unusual title, but the description is strongly AI/ML engineering (kept, low priority)
    none       not relevant (rejected)
The existing numeric score (core.score_job) is computed separately and unchanged.
"""
import re
from dataclasses import dataclass, field

from .normalize import mentions_europe, mentions_netherlands, seniority

_I = re.I

PRIMARY = [re.compile(p, _I) for p in (
    r'\b(ai|a\.i\.|artificial intelligence|gen(?:erative)?\s*-?\s*ai|genai|agentic(?: ai)?|applied ai|conversational ai)\s*'
    r'(/\s*ml\s*)?(engineer|developer|scientist|researcher|specialist|architect|lead)\b',
    r'\b(machine learning|ml|deep learning)\s*(/\s*ai\s*)?(engineer|developer|scientist|researcher|specialist|lead)\b',
    r'\bapplied scientist\b', r'\b(nlp|natural language processing|computational linguist)', r'\bfoundation models?\b',
    r'\bai solutions?\s+(engineer|architect|consultant)\b', r'\bai research', r'\bprompt engineer',
    r'\bai\s*(&|and)\s*(ml|machine learning)\b', r'\bml\s*(&|and)\s*ai\b',
)]
# AI keywords in a title that is not itself an AI role form (e.g. 'Software Engineer, LLM Inference').
KEYWORD_PRIMARY = [re.compile(p, _I) for p in (
    r'\bllms?\b', r'\blarge language models?\b', r'\bgen(?:erative)?\s*-?\s*ai\b', r'\bagentic\b',
    r'\bmachine learning\b', r'\bdeep learning\b',
)]
NON_AI_STACK = re.compile(r'\b(java|spring|frontend|front-end|backend|back-end|full[- ]?stack|\.net|php|ruby|ios|android|'
                          r'mobile|react|angular|embedded|firmware|sap|salesforce)\b', _I)
# Titles that are relevant only when the description shows real AI/ML engineering.
CONTEXTUAL_PRIMARY = [re.compile(p, _I) for p in (
    r'\bresearch (engineer|scientist)\b', r'\bai consultant\b', r'\bcomputer vision\b', r'\bai\b',
    r'\bforward[- ]deployed\b',
)]
SECONDARY = [re.compile(p, _I) for p in (
    r'\b(ml|llm)\s*-?\s*ops\b', r'\b(ai|ml|machine learning|llm)\s+(platform|infrastructure|infra)\b',
    r'\b(ai|ml)\s+architect\b', r'\btechnical ai architect\b', r'\bai forward deployed\b',
)]
CONTEXTUAL_SECONDARY = [re.compile(p, _I) for p in (
    r'\bdata scientist\b', r'\bpython (engineer|developer)\b', r'\bpostdoc', r'\bresearch scientist\b',
    r'\bdecision scientist\b', r'\bquantitative researcher\b',
)]
ENGINEERING_TITLE = re.compile(r'\b(engineer|developer|scientist|researcher|architect|consultant|'
                               r'programmer|ontwikkelaar|onderzoeker)\b', _I)
EXCLUDE_TITLE = re.compile(
    r'\b(intern|internship|stage|stagiair|stagiaire|afstudeer\w*|graduation|graduate program|werkstudent|working student|'
    r'student|trainee|junior|jr\.?|phd (candidate|position|student)|promovendus|sales|account (executive|manager|director)|'
    r'business development|recruit\w*|talent acquisition|marketing|copywriter|teacher|docent|lecturer|'
    r'customer (support|service)|annotator|data labell?er|ai trainer|ai tutor|freelance writer|(language|translation) specialist|'
    r'product (manager|owner|lead|director)|program(me)? manager|project manager|delivery manager|designer|ux|ui|'
    r'content (specialist|writer|creator|manager)|legal|counsel|lawyer|accountant|finance manager|people partner|hr)\b', _I)

AI_SIGNALS = [re.compile(p, _I) for p in (
    r'\bmachine learning\b', r'\bdeep learning\b', r'\bllms?\b', r'\blarge language model', r'\bgenerative ai\b|\bgenai\b',
    r'\bnlp\b|\bnatural language processing\b', r'\bpytorch\b', r'\btensorflow\b|\bjax\b', r'\btransformers?\b',
    r'\bhugging ?face\b', r'\brag\b|retrieval[- ]augmented', r'\bfine[- ]?tun', r'\bmlops\b', r'\bcomputer vision\b',
    r'\breinforcement learning\b', r'\blangchain\b|\bllamaindex\b|\blanggraph\b', r'\bvector (database|search|store)',
    r'\bembeddings?\b', r'\bagentic\b|\bai agents?\b|\bmulti-agent', r'\bmodel (training|serving|deployment|evaluation)',
    r'\bneural network', r'\bscikit-learn\b|\bsklearn\b|\bxgboost\b', r'\binference\b', r'\bprompt engineering\b',
    r'\bopenai\b|\banthropic\b|\bgpt-?\d', r'\bstatistical model', r'\bpredictive model',
)]
HANDS_ON = re.compile(r'\b(python|pytorch|tensorflow|build(ing)?|develop(ing)?|implement|deploy|code|engineering|hands-on|'
                      r'production)\b', _I)


@dataclass
class Classification:
    tier: str                       # primary | secondary | possible | none
    accepted: bool
    reasons: list = field(default_factory=list)
    ai_signals: int = 0
    location_ok: bool = False


def ai_signal_count(text):
    return sum(1 for p in AI_SIGNALS if p.search(text or ''))


def role_tier(title, description=''):
    """Return (tier, reason) from the title, using the description only for context-dependent titles."""
    title = title or ''
    if EXCLUDE_TITLE.search(title):
        return 'none', f'excluded title term: {EXCLUDE_TITLE.search(title).group(0)}'
    if 'intern' in seniority(title) or 'junior' in seniority(title):
        return 'none', 'excluded seniority (intern/junior)'
    signals = ai_signal_count(description)
    hands_on = bool(HANDS_ON.search(description or ''))
    for p in SECONDARY:
        if p.search(title):
            return 'secondary', f'title matches {p.pattern[:40]}'
    for p in PRIMARY:
        if p.search(title):
            return 'primary', 'title matches target role'
    for p in KEYWORD_PRIMARY:
        if p.search(title):
            if NON_AI_STACK.search(title):
                return 'secondary', 'AI keyword in a non-AI stack title'
            return 'primary', 'AI keyword in title'
    for p in CONTEXTUAL_PRIMARY:
        # e.g. 'Research Engineer', 'AI Consultant', 'Computer Vision Engineer': need an engineering-type
        # title AND real AI/ML content in the description (so 'AI Product Manager' does not qualify).
        if not p.search(title) or not ENGINEERING_TITLE.search(title):
            continue
        if 'consultant' in p.pattern and not hands_on:
            continue
        if signals >= 2:
            return 'primary', f'title + AI context ({signals} signals)'
    for p in CONTEXTUAL_SECONDARY:
        if p.search(title) and signals >= 2:
            return 'secondary', f'title + ML context ({signals} signals)'
    if ENGINEERING_TITLE.search(title) and signals >= 5:
        return 'possible', f'unusual title, strong AI/ML description ({signals} signals)'
    return 'none', 'title/description not in target roles'


ELIGIBLE_REGIONS = {'randstad', 'nl_unspecified', 'remote_nl', 'remote_europe', 'remote_global'}


def location_eligible(loc, description='', config=None):
    """Is the vacancy realistically open to a candidate living in the Netherlands?"""
    config = config or {}
    if loc.region in ELIGIBLE_REGIONS:
        return True, loc.region
    if loc.region == 'nl_other':
        return bool(config.get('accept_nl_outside_randstad', True)), 'nl_other (outside Randstad)'
    if loc.region == 'remote_unspecified':
        ok = mentions_europe(description)
        return ok, 'remote, Europe/NL mentioned in description' if ok else 'remote, region not stated (likely not NL)'
    if loc.region == 'unknown':
        ok = mentions_netherlands(description)
        return ok, 'location missing, NL mentioned in description' if ok else 'location unknown'
    if loc.region == 'foreign' and config.get('allow_relocation'):
        return True, 'foreign (relocation allowed by config)'
    return False, f'location {loc.region}'


def classify(title, description, loc, config=None):
    tier, reason = role_tier(title, description)
    ok_loc, loc_reason = location_eligible(loc, description, config)
    return Classification(tier=tier, accepted=tier != 'none' and ok_loc, reasons=[reason, loc_reason],
                          ai_signals=ai_signal_count(description), location_ok=ok_loc)


def worth_detail_fetch(title, loc, config=None):
    """Cheap pre-filter on list data (title + location) before fetching a full posting."""
    ok_loc, _ = location_eligible(loc, '', config) if loc.region not in ('unknown', 'remote_unspecified') else (True, '')
    if not ok_loc:
        return False
    tier, _ = role_tier(title, '')
    return tier != 'none' or bool(ENGINEERING_TITLE.search(title or '')) and not EXCLUDE_TITLE.search(title or '')
