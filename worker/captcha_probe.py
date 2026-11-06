"""READ-ONLY CAPTCHA probe: open employer application pages in a real (headless) Chromium and record what the
bot protection actually does - without typing, uploading or submitting anything.

    python worker/captcha_probe.py URL [URL ...] --out probe.json [--screenshots DIR] [--channel chromium]

For each page it separates four things that a static HTML scan cannot tell apart:
  1. scripts   - which CAPTCHA providers' scripts/requests the page loads at all;
  2. mode      - how they are configured: score-based (reCAPTCHA v3/Enterprise "render=<key>", never shows a
                 puzzle; the employer's server decides), invisible (reCAPTCHA size=invisible / hCaptcha invisible:
                 runs at submit and MAY then show a puzzle), checkbox (needs a click), Turnstile;
  3. challenge - whether a person-facing challenge (puzzle, checkbox, Cloudflare interstitial) is actually shown,
                 on load and after scrolling and focusing a field (focus only - nothing is typed);
  4. access    - whether the server refused the page itself (HTTP 403/429/503, block page).
Server-side rejection of a *submission* (e.g. a low reCAPTCHA score) cannot be observed without submitting; the
probe reports it as "decided at submit" rather than guessing.

Same standard browser as the worker: no stealth, no user-agent spoofing, no solving, no proxies.
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from apply_worker import captcha_frames, detect_blocker, turnstile_passed, write_bytes  # noqa: E402

PROVIDERS = {'recaptcha': re.compile(r'google\.com/recaptcha|recaptcha\.net|gstatic\.com/recaptcha'),
             'hcaptcha': re.compile(r'hcaptcha\.com|captcha-(assets|base)\.recruiteecdn\.com'),  # Recruitee self-hosts hCaptcha
             'turnstile': re.compile(r'challenges\.cloudflare\.com'),
             'datadome': re.compile(r'datadome\.co|captcha-delivery\.com'),
             'perimeterx': re.compile(r'perimeterx\.net|px-cdn\.net|px-cloud\.net')}
SUBMIT = re.compile(r'^\s*(submit|apply|send|solliciteer|verstuur|versturen)', re.I)


def provider_modes(urls):
    """Infer the CAPTCHA configuration from the requests the page made (not from text in the HTML)."""
    modes = {}
    for u in urls:
        low = u.lower()
        m = re.search(r'recaptcha/(api|enterprise)\.js\?(?:[^#]*&)?render=([^&#]+)', low)
        if m and m.group(2) not in ('explicit', 'onload'):
            modes.setdefault('recaptcha', set()).add(f'score-based ({m.group(1)})')
        elif re.search(r'recaptcha/(api2|enterprise)/anchor', low):
            modes.setdefault('recaptcha', set()).add('invisible' if 'size=invisible' in low else 'checkbox')
        elif PROVIDERS['hcaptcha'].search(low):
            modes.setdefault('hcaptcha', set())
            if 'checksiteconfig' in low:
                modes['hcaptcha'].add('site config checked')
            if '/getcaptcha' in low:
                modes['hcaptcha'].add('challenge requested')
        elif 'challenges.cloudflare.com' in low:
            modes.setdefault('turnstile', set()).add('turnstile')
    return {k: sorted(v) for k, v in modes.items()}


def form_summary(page):
    try:
        return {'inputs': page.locator('input:visible, textarea:visible, select:visible').count(),
                'file_inputs': page.locator('input[type="file"]').count(),
                'submit_buttons': [t.strip()[:40] for t in page.get_by_role('button', name=SUBMIT).all_inner_texts()][:3]}
    except Exception as exc:
        return {'error': type(exc).__name__}


def verdict(r):
    if r['access'] != 'ok':
        return 'blocked on page load (server refused the browser)'
    if r['challenge_on_load'] or r['challenge_after_focus']:
        return 'interactive challenge shown before submitting'
    if not r['scripts']:
        return 'no CAPTCHA observed'
    modes = {m for v in r['modes'].values() for m in v}
    if any(m.startswith('score-based') for m in modes) and not ({'invisible', 'checkbox'} & modes) \
            and 'hcaptcha' not in r['scripts']:
        return 'score-based only: no puzzle; the employer\'s server accepts or rejects at submit'
    return 'invisible CAPTCHA: no challenge before submit; may or may not challenge at submit'


def probe(page, url, shots, idx):
    requests = []
    page.on('request', lambda req: requests.append(req.url))
    r = {'url': url, 'at': datetime.now(timezone.utc).isoformat()}
    started = time.monotonic()
    try:
        resp = page.goto(url, wait_until='domcontentloaded', timeout=45000)
        r['http_status'] = resp.status if resp else None
        server = (resp.headers.get('server', '') if resp else '').lower()
        r['cdn'] = 'cloudflare' if 'cloudflare' in server else server[:30] or None
        try:
            page.wait_for_load_state('networkidle', timeout=15000)
        except Exception:
            pass
    except Exception as exc:
        r.update(http_status=None, error=f'{type(exc).__name__}: {str(exc)[:160]}')
    r['title'] = (page.title() or '')[:80]
    blocker = detect_blocker(page)
    r['frames_on_load'] = captcha_frames(page)
    r['challenge_on_load'] = blocker if blocker and 'login' not in blocker else None
    r['login_required'] = blocker == 'login required'
    # behave like a reader: scroll through the form and focus the first text field (no typing, no clicks on buttons)
    try:
        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(1500)
        first = page.locator('input[type="text"]:visible, input[type="email"]:visible, input:not([type]):visible').first
        if first.count():
            first.focus()
        page.wait_for_timeout(4000)
    except Exception:
        pass
    blocker = detect_blocker(page)
    r['frames_after_focus'] = captcha_frames(page)
    r['challenge_after_focus'] = blocker if blocker and 'login' not in blocker else None
    status = r.get('http_status')
    blocked_title = any(t in r['title'].lower() for t in ('just a moment', 'attention required', 'access denied', 'forbidden'))
    r['access'] = 'ok' if status and status < 400 and not blocked_title else f'refused ({status}, "{r["title"]}")'
    r['scripts'] = sorted({name for u in requests for name, rx in PROVIDERS.items() if rx.search(u)})
    r['modes'] = provider_modes(requests)
    r['js_objects'] = page.evaluate("""() => ({grecaptcha: !!window.grecaptcha, enterprise: !!(window.grecaptcha &&
        window.grecaptcha.enterprise), hcaptcha: !!window.hcaptcha, turnstile: !!window.turnstile})""")
    r['turnstile_token'] = turnstile_passed(page) if 'turnstile' in r['scripts'] else None
    r['form'] = form_summary(page)
    r['seconds'] = round(time.monotonic() - started, 1)
    if shots:
        Path(shots).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(shots) / f'probe-{idx}.png'), full_page=False)
    r['verdict'] = verdict(r)
    return r


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('urls', nargs='+')
    ap.add_argument('--out', required=True)
    ap.add_argument('--screenshots')
    ap.add_argument('--channel', help='"chromium" = full Chromium in new headless mode (default: headless shell)')
    args = ap.parse_args(argv)
    from playwright.sync_api import sync_playwright
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel=args.channel or None)
        ua = None
        for i, url in enumerate(args.urls):
            context = browser.new_context(viewport={'width': 1280, 'height': 900}, locale='en-US')
            page = context.new_page()
            ua = ua or page.evaluate('navigator.userAgent')
            res = probe(page, url, args.screenshots, i)
            results.append(res)
            print(f"{res['verdict'][:60]:60} | {res['scripts']} {res['modes']} | {url}", flush=True)
            context.close()
        browser.close()
    write_bytes(args.out, json.dumps({'user_agent': ua, 'channel': args.channel or 'headless-shell',   # local path or gs://
                                      'results': results}, indent=2, default=list).encode())
    return 0


if __name__ == '__main__':
    sys.exit(main())
