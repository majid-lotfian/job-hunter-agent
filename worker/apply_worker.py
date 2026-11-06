"""On-demand browser worker: fill and (when allowed) submit ONE approved job application, then exit.

Runs as a Cloud Run Job task (container from worker/Dockerfile) or locally for tests:

    python worker/apply_worker.py --package package.json --result result.json [--evidence-dir DIR]
    python worker/apply_worker.py --package gs://BUCKET/app-12/package.json --result gs://BUCKET/app-12/result.json

Rules (enforced here, not just by the caller):
- Uses a standard Playwright Chromium: no stealth plugins, no fingerprint or user-agent spoofing, no CAPTCHA
  solving services, no proxies. Bot protection is never interfered with.
- If the page needs a login or shows a human-verification challenge (visible hCaptcha/reCAPTCHA challenge,
  Cloudflare interstitial), the worker stops WITHOUT interacting with it: status "blocked".
- Only the fields in the package are filled; required fields that cannot be filled and read back stop the run.
- Submit is clicked at most once, and only when the package says submit=true.
- "submitted" only with confirmation evidence (confirmation text or URL); otherwise "uncertain" - never retried.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CONFIRM_TEXT = [r'thank(s| you)[^.]{0,40}(appl|interest)', r'application (has been |was )?(received|submitted|sent)',
                        r'we (have )?received your application', r'bedankt voor (je|uw) sollicitatie',
                        r'sollicitatie (is )?(ontvangen|verstuurd)']
SUBMIT_NAMES = re.compile(r'^\s*(submit|submit application|apply|apply now|send|send application|solliciteer|verstuur|versturen)\b', re.I)
YES = ('yes', 'ja', 'true')
NO = ('no', 'nee', 'false')


# --------------------------------------------------------------------------- storage (local path or gs://)

def _gcs_token():
    import httpx
    r = httpx.get('http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token',
                  headers={'Metadata-Flavor': 'Google'}, timeout=10)
    r.raise_for_status()
    return r.json()['access_token']


def _gcs_split(uri):
    bucket, _, name = uri[5:].partition('/')
    return bucket, name


def read_bytes(location):
    if location.startswith('gs://'):
        import httpx
        from urllib.parse import quote
        bucket, name = _gcs_split(location)
        r = httpx.get(f'https://storage.googleapis.com/storage/v1/b/{bucket}/o/{quote(name, safe="")}?alt=media',
                      headers={'Authorization': f'Bearer {_gcs_token()}'}, timeout=60)
        r.raise_for_status()
        return r.content
    return Path(location).read_bytes()


def write_bytes(location, data, content_type='application/json'):
    if location.startswith('gs://'):
        import httpx
        from urllib.parse import quote
        bucket, name = _gcs_split(location)
        r = httpx.post(f'https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o?uploadType=media&name={quote(name, safe="")}',
                       headers={'Authorization': f'Bearer {_gcs_token()}', 'Content-Type': content_type}, content=data, timeout=60)
        r.raise_for_status()
        return
    Path(location).parent.mkdir(parents=True, exist_ok=True)
    Path(location).write_bytes(data)


# --------------------------------------------------------------------------- page checks

def classify_frame(src, title=''):
    """(provider, role) of a CAPTCHA iframe from its URL/title; role in checkbox|challenge|invisible|widget, or None.

    - reCAPTCHA: /anchor?...size=invisible = invisible widget (only the small badge shows), /anchor otherwise = the
      "I'm not a robot" checkbox (needs a click), /bframe = the image-challenge popup (hidden until a challenge).
    - hCaptcha: #frame=checkbox = checkbox (hidden in invisible mode), #frame=challenge = challenge popup.
    - Cloudflare Turnstile/managed challenge: challenges.cloudflare.com widget.
    """
    s, t = (src or '').lower(), (title or '').lower()
    if 'recaptcha' in s:
        if '/bframe' in s:
            return 'recaptcha', 'challenge'
        if '/anchor' in s:
            return 'recaptcha', 'invisible' if 'size=invisible' in s else 'checkbox'
        return 'recaptcha', 'widget'
    if 'hcaptcha' in s or 'hcaptcha' in t:   # also self-hosted copies, e.g. Recruitee's captcha-assets.recruiteecdn.com
        if 'frame=challenge' in s:
            return 'hcaptcha', 'challenge'
        if 'frame=checkbox' in s or 'frame=enclave' in s:
            return 'hcaptcha', 'checkbox'
        if 'checkbox' in t:
            return 'hcaptcha', 'checkbox'
        if 'challenge' in t:
            return 'hcaptcha', 'challenge'
        return 'hcaptcha', 'widget'
    if 'challenges.cloudflare.com' in s:
        return 'turnstile', 'widget'
    return None, None


def captcha_frames(page):
    """Every CAPTCHA iframe with its role and whether a person could actually see it (on screen, not hidden, > 50px)."""
    frames = []
    # page.frames also sees frames inside closed shadow roots (Cloudflare Turnstile renders there)
    for child in page.frames[1:]:
        try:
            frame = child.frame_element()
            src, title = f"{child.url} {frame.get_attribute('src') or ''}", frame.get_attribute('title') or ''
            provider, role = classify_frame(src, title)
            if not provider:
                continue
            box = frame.bounding_box() if frame.is_visible() else None
            opacity = frame.evaluate("""el => { let o = 1, n = el; while (n && n.nodeType === 1) {
                const s = getComputedStyle(n); o *= parseFloat(s.opacity || '1');
                if (s.visibility === 'hidden' || s.display === 'none') return 0; n = n.parentElement; } return o; }""")
        except Exception:
            continue
        shown = bool(box) and opacity > 0.1 and box['width'] > 50 and box['height'] > 50 \
            and box['x'] + box['width'] > 0 and box['y'] + box['height'] > 0
        frames.append({'provider': provider, 'role': role, 'shown': shown,
                       'size': [round(box['width']), round(box['height'])] if box else [0, 0]})
    return frames


def detect_blocker(page):
    """A login wall or something that needs a human: a shown CAPTCHA challenge or checkbox, a Cloudflare interstitial.

    CAPTCHA scripts, hidden challenge frames, the invisible-reCAPTCHA badge and score-based checks are NOT blockers:
    they only become one when the employer's page actually presents a challenge."""
    title = (page.title() or '').lower()
    if any(t in title for t in ('just a moment', 'attention required', 'access denied')):
        return 'human verification or access block page'
    if page.locator('input[type="password"]:visible').count():
        return 'login required'
    for f in captcha_frames(page):
        if f['shown'] and f['provider'] == 'turnstile' and turnstile_passed(page):
            continue                     # Turnstile shows its widget but already passed without any interaction
        if f['shown'] and (f['role'] in ('challenge', 'checkbox') or f['provider'] == 'turnstile'):
            return f"human verification challenge ({f['provider']} {f['role']})"
    return None


def turnstile_passed(page):
    """Cloudflare Turnstile puts its token in a hidden cf-turnstile-response field once it passed (no interaction)."""
    try:
        return bool(page.evaluate("""() => [...document.querySelectorAll('input[name="cf-turnstile-response"]')]
            .some(e => (e.value || '').length > 20)"""))
    except Exception:
        return False


def _group(page, label):
    """Container of a question (fieldset/legend, role=group, or the element holding the label text)."""
    for locator in (page.get_by_role('group', name=label),
                    page.locator('fieldset').filter(has_text=label),
                    page.locator('div,li,section').filter(has_text=label).filter(has=page.locator('input,select'))):
        if locator.count():
            return locator.last
    return None


def _file_target(page, label):
    """(group, file input) for an upload field: the labelled input itself, the file input inside a labelled wrapper
    (Greenhouse: role=group "Resume/CV" around a hidden input labelled "Attach"), else the form's first file input."""
    labelled = page.get_by_label(label, exact=False)
    if labelled.count() and labelled.first.get_attribute('type') == 'file':
        return None, labelled.first
    if labelled.count() and labelled.first.locator('input[type="file"]').count():
        return labelled.first, labelled.first.locator('input[type="file"]').first
    if page.locator('input[type="file"]').count():
        return None, page.locator('input[type="file"]').first
    return None, None


def _file_attached(page, label, cv_path):
    """Widgets that keep the file in their own state clear (or remove) the input and show the file name instead, so
    either a selected file or the file name inside that upload field counts as attached. The field is located by its
    label every time - never by "first file input" once a labelled field exists (Greenhouse removes the resume's
    input after an upload, and the next file input on the page is the cover letter's)."""
    name = Path(cv_path).name
    labelled = page.get_by_label(label, exact=False)
    try:
        if labelled.count():
            el = labelled.first
            if el.get_attribute('type') == 'file':
                if el.evaluate('el => el.files.length', timeout=3000) == 1:
                    return True
                group = el.locator('xpath=ancestor::*[@role="group" or contains(@class, "upload")][1]')
                return group.count() > 0 and name in group.inner_text(timeout=3000)
            inputs = el.locator('input[type="file"]')
            if inputs.count() and inputs.first.evaluate('el => el.files.length', timeout=3000) == 1:
                return True
            return name in el.inner_text(timeout=3000)
        target = page.locator('input[type="file"]').first      # unlabelled form: its first file input
        if target.count() and target.evaluate('el => el.files.length', timeout=3000) == 1:
            return True
        group = target.locator('xpath=ancestor::*[@role="group" or contains(@class, "upload")][1]')
        return group.count() > 0 and name in group.inner_text(timeout=3000)
    except Exception:
        return False


def _attach_file(page, label, cv_path):
    """Attach the CV to the upload field named `label` and verify it is attached."""
    _, target = _file_target(page, label)
    if target is None:
        return False
    for attempt in range(2):        # a widget that was not ready ignores the file; setting it again is harmless
        target.set_input_files(str(cv_path))
        page.wait_for_timeout(1500)
        if _file_attached(page, label, cv_path):
            return True
        page.wait_for_timeout(2000)
        if _file_attached(page, label, cv_path):
            return True
        _, target = _file_target(page, label)
        if target is None:
            return False
    return False


def _choose_in_combobox(page, box, wanted):
    """ARIA combobox (custom dropdown): open it, click the option whose whole text matches, read the choice back from
    the control. Never types free text, never picks a 'closest' option."""
    for names in wanted:
        box.click()
        options = page.get_by_role('option')
        try:
            options.first.wait_for(timeout=5000)
        except Exception:
            return False
        texts = [t.strip() for t in options.all_inner_texts()]
        lowered = {n.strip().lower() for n in names}
        pick = next((i for i, t in enumerate(texts) if t.lower() in lowered), None)
        if pick is None:
            page.keyboard.press('Escape')
            return False
        options.nth(pick).click()
        page.wait_for_timeout(200)
        control = box.locator('xpath=ancestor::*[contains(@class, "control")][1]')
        shown = control.inner_text() if control.count() else box.locator('xpath=../..').inner_text()
        if texts[pick].lower() not in shown.lower():
            return False
        page.keyboard.press('Escape')
    return True


def _as_yes_no(value):
    if isinstance(value, bool):
        return YES if value else NO
    return YES if str(value).strip().lower() in YES else NO if str(value).strip().lower() in NO else (str(value),)


def fill_field(page, field, cv_path):
    """Fill one field and read it back. Returns True when the value is verifiably in the form."""
    label, kind, value = field['label'], field.get('kind', 'text'), field.get('value')
    if kind == 'file':
        return _attach_file(page, label, cv_path)
    if kind in ('boolean', 'single_choice', 'multi_choice'):
        wanted = [_as_yes_no(value)] if kind == 'boolean' else [(str(v),) for v in (value if isinstance(value, list) else [value])]
        select = page.get_by_label(label, exact=False)
        if select.count() and select.first.evaluate('el => el.tagName') == 'SELECT':
            options = select.first.locator('option').all_inner_texts()
            pick = next((o for o in options if o.strip().lower() in {w.lower() for w in wanted[0]}), None)
            if pick is None:
                return False
            select.first.select_option(label=pick)
            return select.first.evaluate('el => el.options[el.selectedIndex].text').strip() == pick.strip()
        if select.count() and select.first.get_attribute('role') == 'combobox':   # e.g. Greenhouse's react-select
            return _choose_in_combobox(page, select.first, wanted)
        group = _group(page, label)
        if group is None:
            if kind == 'boolean':                      # a single consent-style checkbox
                box = page.get_by_label(label, exact=False)
                if box.count() and box.first.get_attribute('type') == 'checkbox':
                    if wanted[0] is YES:
                        box.first.check()
                    return box.first.is_checked() == (wanted[0] is YES)
            return False
        ok = True
        for names in wanted:
            option = None
            for n in names:   # option labels match whole-text, case-insensitively ("yes" == "Yes")
                candidate = group.get_by_label(re.compile(rf'^\s*{re.escape(n)}\s*$', re.I))
                if candidate.count():
                    option = candidate
                    break
            if option is None:
                return False
            option.first.check()
            ok = ok and option.first.is_checked()
        return ok
    target = page.get_by_label(label, exact=False)
    if not target.count():
        return False
    target.first.fill(str(value))
    return target.first.input_value().strip() == str(value).strip()


def verify_field(page, field, cv_path):
    """Read a field's CURRENT state without changing it: is the package value still in the form? Used right before
    Submit, because scripts on the page (e.g. Greenhouse's resume autofill) can overwrite fields after they were filled."""
    label, kind, value = field['label'], field.get('kind', 'text'), field.get('value')
    try:
        if kind == 'file':
            return _file_attached(page, label, cv_path)
        if kind in ('boolean', 'single_choice', 'multi_choice'):
            wanted = [_as_yes_no(value)] if kind == 'boolean' else [(str(v),) for v in (value if isinstance(value, list) else [value])]
            el = page.get_by_label(label, exact=False)
            if el.count() and el.first.evaluate('el => el.tagName') == 'SELECT':
                chosen = el.first.evaluate('el => el.selectedIndex >= 0 ? el.options[el.selectedIndex].text : ""').strip().lower()
                return chosen in {w.lower() for w in wanted[0]}
            if el.count() and el.first.get_attribute('role') == 'combobox':
                control = el.first.locator('xpath=ancestor::*[contains(@class, "control")][1]')
                shown = (control.inner_text() if control.count() else el.first.locator('xpath=../..').inner_text()).lower()
                return all(any(n.lower() in shown for n in names) for names in wanted)
            group = _group(page, label)
            if group is None:
                if kind == 'boolean' and el.count() and el.first.get_attribute('type') == 'checkbox':
                    return el.first.is_checked() == (wanted[0] is YES)
                return False
            for names in wanted:
                hits = [group.get_by_label(re.compile(rf'^\s*{re.escape(n)}\s*$', re.I)) for n in names]
                if not any(h.count() and h.first.is_checked() for h in hits):
                    return False
            return True
        target = page.get_by_label(label, exact=False)
        return bool(target.count()) and target.first.input_value().strip() == str(value).strip()
    except Exception:
        return False


TEXT_INPUTS_JS = """() => [...document.querySelectorAll('input, textarea')].filter(e =>
    ['text', 'email', 'tel', 'url', ''].includes((e.getAttribute('type') || '').toLowerCase()) || e.tagName === 'TEXTAREA')
  .filter(e => e.getAttribute('role') !== 'combobox' && e.type !== 'hidden' && e.id)
  .map(e => { const l = document.querySelector(`label[for="${CSS.escape(e.id)}"]`);
              return [e.id, (l ? l.innerText : e.getAttribute('aria-label') || '').trim(), e.value || '']; })"""


def clear_unrequested_values(page, fields, empty_at_load):
    """Text fields that were empty when the page loaded, are not part of the package, and now hold a value (filled
    by the page's own autofill) are emptied again: only package values are sent. Returns their labels."""
    def norm(text):
        return re.sub(r'\s+', ' ', (text or '').replace('*', '')).strip().lower()
    ours = {norm(f['label']) for f in fields}
    cleared = []
    for el_id, label, value in page.evaluate(TEXT_INPUTS_JS):
        if el_id in empty_at_load and value.strip() and norm(label) not in ours:
            page.locator(f'[id="{el_id}"]').fill('')
            cleared.append(label or el_id)
    return cleared


ERROR_TEXT = re.compile(r'(is required|required field|this field is required|please (enter|fill|complete|select|provide)|'
                        r'is not valid|invalid (email|phone|format)|must be)', re.I)
ERRORS_JS = """() => [...document.querySelectorAll('body *')].filter(e => e.children.length === 0 && e.offsetParent !== null)
  .map(e => (e.innerText || '').trim()).filter(t => t && t.length < 160)"""


def validation_errors(page):
    texts = []
    for loc in (page.locator('[aria-invalid="true"]:visible'), page.locator('.error:visible, .field-error:visible, [role="alert"]:visible')):
        for el in loc.all()[:5]:
            text = (el.inner_text() or el.get_attribute('name') or '').strip()
            if text:
                texts.append(text[:120])
    try:
        texts += [t[:120] for t in page.evaluate(ERRORS_JS) if ERROR_TEXT.search(t)][:5]
    except Exception:
        pass
    return list(dict.fromkeys(texts))


def confirmation(page, rules):
    url_rules = [re.compile(p, re.I) for p in rules.get('url', [])]
    text_rules = [re.compile(p, re.I) for p in (rules.get('text') or DEFAULT_CONFIRM_TEXT)]
    if any(r.search(page.url) for r in url_rules):
        return f'url matched: {page.url}'
    body = page.locator('body').inner_text(timeout=2000) if page.locator('body').count() else ''
    for rule in text_rules:
        match = rule.search(body)
        if match:
            start = max(0, match.start() - 60)
            return re.sub(r'\s+', ' ', body[start:match.end() + 120]).strip()
    return None


# --------------------------------------------------------------------------- run

def run(package, cv_path, evidence_dir, browser_channel=None, headless=True):
    from playwright.sync_api import sync_playwright
    result = {'application_id': package.get('application_id'), 'job_id': package.get('job_id'), 'status': 'failed',
              'reason': '', 'filled': [], 'missing': [], 'submitted_clicked': False, 'evidence': {}}
    timeouts = package.get('timeouts') or {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, channel=browser_channel or None)
        try:
            page = browser.new_page()
            page.set_default_timeout(int(timeouts.get('action', 15)) * 1000)
            page.goto(package['apply_url'], wait_until='domcontentloaded', timeout=int(timeouts.get('page', 45)) * 1000)
            try:   # let the form's scripts finish loading: a file attached earlier is silently ignored (Greenhouse)
                page.wait_for_load_state('networkidle', timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(int(timeouts.get('hydrate', 3)) * 1000)
            blocker = detect_blocker(page)
            if blocker:
                return {**result, 'status': 'blocked', 'reason': blocker}
            empty_at_load = {i for i, _, v in page.evaluate(TEXT_INPUTS_JS) if not v.strip()}
            # files first: attaching a CV can trigger the page's resume autofill, which rewrites other fields
            ordered = [f for f in package['fields'] if f.get('kind') == 'file'] + \
                      [f for f in package['fields'] if f.get('kind') != 'file']
            for i, field in enumerate(ordered):
                try:
                    ok = fill_field(page, field, cv_path)
                except Exception as exc:   # element missing / not interactable
                    ok, field = False, {**field, 'error': type(exc).__name__}
                (result['filled'] if ok else result['missing']).append(field['label'])
                if field.get('kind') == 'file' and (i + 1 == len(ordered) or ordered[i + 1].get('kind') != 'file'):
                    page.wait_for_timeout(int(timeouts.get('autofill_settle', 4)) * 1000)
            if any(f['label'] in result['missing'] for f in package['fields'] if f.get('required', True)):
                return {**result, 'status': 'failed', 'reason': 'required fields could not be filled - nothing was submitted'}
            # final check of the whole form right before Submit; one re-fill for fields the page changed afterwards
            page.wait_for_timeout(2000)
            result['cleared'] = clear_unrequested_values(page, package['fields'], empty_at_load)
            filled = [f for f in package['fields'] if f['label'] in result['filled']]
            changed = [f for f in filled if not verify_field(page, f, cv_path)]
            if changed:
                result['refilled'] = [f['label'] for f in changed]
                for f in changed:
                    try:
                        fill_field(page, f, cv_path)
                    except Exception:
                        pass
                page.wait_for_timeout(2000)
                result['cleared'] += clear_unrequested_values(page, package['fields'], empty_at_load)
            not_ok = [f['label'] for f in filled if not verify_field(page, f, cv_path)]
            if not_ok:
                return {**result, 'status': 'failed', 'missing': not_ok,
                        'reason': 'fields changed by the page and could not be restored: ' + ', '.join(not_ok)
                                  + ' - nothing was submitted', 'evidence': _evidence(page, evidence_dir, None)}
            result['verified_before_submit'] = True
            blocker = detect_blocker(page)
            if blocker:
                return {**result, 'status': 'blocked', 'reason': blocker}
            if not package.get('submit'):
                return {**result, 'status': 'filled', 'reason': 'dry run: form filled, not submitted',
                        'evidence': _evidence(page, evidence_dir, None)}
            buttons = page.get_by_role('button', name=SUBMIT_NAMES)
            button = buttons.last if buttons.count() else page.locator('button[type="submit"], input[type="submit"]').last
            if not button.count():
                return {**result, 'status': 'failed', 'reason': 'submit button not found - nothing was submitted'}
            result['submitted_clicked'] = True
            button.click()                                         # exactly once
            deadline = time.monotonic() + int(timeouts.get('confirm', 30))
            while time.monotonic() < deadline:
                page.wait_for_timeout(750)
                found = confirmation(page, package.get('confirmation') or {})
                if found:
                    return {**result, 'status': 'submitted', 'reason': 'confirmation shown by the employer\'s page',
                            'evidence': _evidence(page, evidence_dir, found)}
                blocker = detect_blocker(page)
                if blocker:
                    return {**result, 'status': 'blocked', 'reason': f'{blocker} after submit - not completed',
                            'evidence': _evidence(page, evidence_dir, None)}
                errors = validation_errors(page)
                if errors:
                    return {**result, 'status': 'failed', 'reason': 'the form reported errors: ' + '; '.join(errors)[:300],
                            'evidence': _evidence(page, evidence_dir, None)}
            return {**result, 'status': 'uncertain', 'reason': 'no confirmation within the time limit - check before retrying',
                    'evidence': _evidence(page, evidence_dir, None)}
        except Exception as exc:
            status = 'uncertain' if result['submitted_clicked'] else 'failed'
            return {**result, 'status': status, 'reason': f'worker error: {type(exc).__name__}: {str(exc)[:200]}'}
        finally:
            browser.close()


def _evidence(page, evidence_dir, matched):
    shot = page.screenshot(full_page=True)
    name = f'evidence-{int(time.time())}.png'
    if evidence_dir:
        write_bytes(f'{evidence_dir.rstrip("/")}/{name}', shot, 'image/png')
    return {'url': page.url, 'confirmation_text': (matched or '')[:400], 'screenshot': name,
            'screenshot_sha256': hashlib.sha256(shot).hexdigest(), 'at': datetime.now(timezone.utc).isoformat()}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Fill and (if allowed) submit one approved application, then exit.')
    parser.add_argument('--package', required=True)
    parser.add_argument('--result', required=True)
    parser.add_argument('--evidence-dir')
    parser.add_argument('--channel', default=os.getenv('WORKER_BROWSER_CHANNEL') or None, help='e.g. chrome (system Chrome)')
    args = parser.parse_args(argv)
    package = json.loads(read_bytes(args.package))
    with tempfile.TemporaryDirectory() as tmp:
        cv = Path(tmp) / (package.get('cv_filename') or 'CV.pdf')
        cv.write_bytes(read_bytes(package['cv_pdf']))
        result = run(package, cv, args.evidence_dir, browser_channel=args.channel)
    result['finished_at'] = datetime.now(timezone.utc).isoformat()
    write_bytes(args.result, json.dumps(result, indent=2).encode())
    print(json.dumps({k: result[k] for k in ('application_id', 'status', 'reason')}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
