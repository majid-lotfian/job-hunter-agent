# Cloud browser worker for unattended applications: real-browser CAPTCHA findings, Cloud Run design, PoC, shortlist

- **PR:** [#13](https://github.com/majid-lotfian/job-hunter-agent/pull/13)
- **Branch:** `feature/cloud-browser-worker` (from `main` d425b4e)
- **Date:** 2026-11-06 16:41 (Europe/Amsterdam) · **Author:** Claude Code (Opus 5.5)
- **Status:** open: not merged, not deployed, no cloud resources created. **No application was submitted.** No real employer form had anything typed into it.

> Legend: **Implemented** = code in this PR · **Unit-tested** = automated tests · **Live-verified** = observed on
> real systems (stated where) · **Assumed/Proposed** = not verified.

## Owner decisions summary (read first)

1. **CAPTCHA findings, from a real browser (read-only, 26 application pages):**
   - **Cloudflare Turnstile with an interactive challenge:** 1 of 26, **Nebius** (Greenhouse). The "Verify you are human" checkbox waits for a click, and Turnstile never issued a token. **All Nebius jobs are out** for unattended submission, which reverses the previous draft's pick of job 24.
   - **Invisible + score-based reCAPTCHA Enterprise:** 22 of 26, all other Greenhouse (10 employers) and all Ashby (8 employers). No challenge on load, scroll or focus. The decision happens **at submit, on the employer's server**, which a read-only check cannot see.
   - **Invisible hCaptcha:** Lever (3/3) and Recruitee (Eraneos, Conclusion; Recruitee self-hosts it on `recruiteecdn.com`). No challenge before submit, but it **can** show a puzzle at submit.
   - **No CAPTCHA:** Recruitee **Centric**.
   - **Server refused the page:** 0 of 26. Every page returned HTTP 200 to a headless Chromium on a GCP datacenter IP.
2. **First eligible application (recommended):** **job 38, JetBrains "(Senior) Machine Learning Developer (Junie)"**: AI coding agent / LLM work, English, Amsterdam or remote, Greenhouse, no challenge before submit.
   - It needs **2 answers from you:** how you heard about the role (choose from their list) and your preferred location.
   - **Backup:** job 15, **Adyen** "Senior Machine Learning Engineer – AML Tech". It needs 2 answers: the "Point of Data Transfer" acknowledgement (only if you accept it) and Amsterdam 3 office days or relocation (Yes/No).
3. **Honest odds:** for jobs 38 and 15, whether reCAPTCHA Enterprise accepts an honest headless browser from a cloud IP is **unknown until one real submit**. The worker cannot cause a duplicate or a silent failure:
   - a challenge after the click means `blocked`;
   - no confirmation means manual follow-up, never resent.
4. **Architecture:** keep the VM for discovery, CV, Telegram and the dashboard. A **Cloud Run Job** (one task, zero retries, 2 GiB, 10 min max) runs one application and exits. The VM dispatches through a private GCS bucket.
   - **Cost:** under **USD 1/month** at a few applications a day, even without any free tier (§9).
   - **Note:** the VM is now an **e2-medium** (4 GB, observed today after the reboot), not an e2-micro. On it, the same worker ran all mock-form browser tests in 70 s. The existing `local` backend could therefore run it on the VM with no new cloud resources. The cloud worker matters if you go back to e2-micro.
5. **Proof of concept** (implemented, unit-tested, real Chromium on mock forms):
   - fills Greenhouse's real widget types (react-select comboboxes, checkbox lists, file upload) and reads every value back;
   - refuses answers that are not among the employer's options;
   - detects real challenges, including Turnstile hidden in a closed shadow root;
   - submits at most once;
   - reports `submitted` only with the employer's confirmation.
6. **Not done, needs your permission:** a **dry-run fill of a real Greenhouse form** with fake data and no submit, to prove the filler on the live JetBrains page.
   - The session's safety classifier blocked typing into a live employer form without your explicit go-ahead.
   - I recommend doing it as the first step after setup, from Cloud Run (§15).
7. **To go live you need to:**
   - approve the merge;
   - run `worker/setup_cloud_run.sh` in Cloud Shell (creates billable resources);
   - verify `data/applicant_profile.yaml` and add the 2 answers for job 38;
   - approve the cloud dry run;
   - approve `APPLY_MODE=live` + `APPLY_LIVE_ADAPTERS=browser` for exactly one approved job.

## 1. Task objective

The goal is a fully cloud-based route to the first real autonomous application, without the user's laptop. The PR #12 local runner is not implemented. The task had four parts:
1. Find out, in a real browser, what CAPTCHA each ATS actually presents. Distinguish four cases: script present, invisible check, interactive challenge, server-side rejection.
2. Design and price an on-demand Playwright worker.
3. Deliver a proof of concept for one ATS, tested on fixtures and mocks, with read-only checks on real pages, no real submission, and the existing approvals and duplicate protection preserved.
4. Shortlist realistic jobs (English AI/LLM/ML, NL or remote).

## 2. Initial problems identified

- **Static scans overstate the blockers.** A CAPTCHA script in the HTML says nothing about whether a person-facing challenge appears. The real browser shows that most forms load only invisible or score-based checks.
- **Detection gaps in the first worker draft** (from the interrupted earlier session), fixed here:
  - Cloudflare Turnstile renders inside a **closed shadow root**, which DOM queries cannot see. Frames are now enumerated via `page.frames`.
  - A visible reCAPTCHA or hCaptcha **checkbox** was not treated as a blocker.
  - Recruitee serves hCaptcha from `captcha-assets.recruiteecdn.com`, so host-based detection missed it.
- **The worker could not fill real Greenhouse questions.** Real `job-boards.greenhouse.io` single-choice questions are ARIA comboboxes (react-select), not `<select>` elements. They were verified read-only by listing the form controls of the JetBrains page.
- **Wrong page for some Greenhouse employers.** Greenhouse's `absolute_url` is often the employer's careers page with the form inside an iframe (Databricks, Nebius), where label-based filling cannot work.
- **VM changed:** it is now e2-medium, 4 GB, in us-central1-a. The VM service account still has limited scopes (storage read-only), so dispatching needs a dedicated key (§10).

## 3. Architecture and implementation decisions

```
 VM (unchanged services)                                    GCP, on demand only (europe-west4)
 discovery · CV · PDF · Telegram · dashboard
 per-job approval (version-bound) → prepared package complete
 + APPLY_MODE=live + "browser" in APPLY_LIVE_ADAPTERS + BROWSER_WORKER=cloud_run
        │
 app/applications/browser.py ──gcloud storage cp──▶  gs://jh-apply-…/app-<id>-<run>/{package.json, cv.pdf}
        │                                             (private, public-access prevention, 1-day auto-delete)
        └──gcloud run jobs execute --wait ─────────▶  Cloud Run Job "apply-worker": 1 task, 0 retries, 1 vCPU/2 GiB
                                                      worker/apply_worker.py (Playwright 1.63 Chromium):
                                                      open hosted form → blocker? → fill + read back → blocker? →
                                                      click Submit once → confirmation? → screenshot → exit
        ◀──result.json + evidence screenshot ───────  package.json and cv.pdf deleted right after the run
 map → existing state machine → Telegram
```

**Rules enforced in the worker:**
- **Browser:** a standard Chromium with its real (headless) user agent. No stealth, no fingerprint changes, no solving services, no proxies, and the worker never clicks a CAPTCHA.
- **What counts as a blocker:** a shown challenge or checkbox frame, a Turnstile widget without a token, a Cloudflare interstitial, or a login. Any of these gives `blocked`. Scripts, hidden frames, the invisible-reCAPTCHA badge and score-only checks are **not** blockers.
- **Filling:** only package values, every one read back. A choice must match one of the employer's options exactly; no "closest" option and no free text in dropdowns. A required field that can't be filled stops the run before submit.
- **Submitting:** Submit is clicked at most once, only with `submit: true`. `submitted` requires the employer's confirmation; otherwise `uncertain`, which goes to manual follow-up and is never retried.

**VM side** (`process()` step 4b, unchanged gates):
- **Eligibility:** the worker runs only for systems whose fields are read from the employer (Greenhouse, Recruitee), with no missing data or unanswered questions, a current PDF, a verified profile and an open vacancy.
- **Hosted form:** for Greenhouse, the worker opens the hosted form, `job-boards.greenhouse.io/<board>/jobs/<id>` (new `form_url`).
- **Unchanged guards:** per-job approval, duplicate guards (job and canonical URL), and crash-in-`submitting` never resent.

**Which ATS first:** Greenhouse.
- It has the most strong English matches (Adyen, JetBrains, Databricks).
- The agent already reads its questions from the public board API.
- It shows no pre-submit challenge except at Nebius.

Ashby (Prosus job 54 is the single best match) is next, once a question reader exists.

## 4. Files added or modified

| File | Change |
|---|---|
| `worker/apply_worker.py` (new) | Worker with GCS or local I/O. Challenge detection: `classify_frame`, `captcha_frames` via `page.frames`, `turnstile_passed`, `detect_blocker`. Label-based filling of text, file, select, **ARIA combobox**, radio and checkbox lists, with read-back. Single submit, confirmation and evidence. |
| `worker/captcha_probe.py` (new) | **Read-only** probe: scripts, CAPTCHA mode inferred from requests, challenge on load and after scroll/focus, access refusal, form summary, verdict |
| `worker/Dockerfile`, `worker/requirements.txt` (new) | `mcr.microsoft.com/playwright/python:v1.63.0-jammy`, non-root; contains the worker and the probe |
| `worker/setup_cloud_run.sh` (new) | One-time owner setup: APIs, private bucket + lifecycle, image build, job, runtime SA (bucket only), dispatcher SA (this job + bucket) and key |
| `app/applications/browser.py` (new) | Eligibility, `form_url`, worker package, `local`/`cloud_run` dispatch, result mapping |
| `app/applications/__init__.py` | Step 4b: browser path inside the existing gates and state machine |
| `tests/test_browser_worker.py` (new) | 9 integration tests, 2 classification tests, 10 opt-in real-browser tests |
| `tests/fixtures/forms/*` (new) | Greenhouse-like form (`ok`/`challenge`/`invalid`/`silent`), **react-select replica** with optional Turnstile (`block`/`pass`) in a closed shadow root, Recruitee-like form, login wall |
| `docs/agent-reports/…`, `README.md` | This report; index row; statuses of #10/#11 updated to deployed |

## 5. Features implemented

- **Implemented and unit-tested:**
  - the worker, the probe and the VM dispatcher (`local` backend);
  - result mapping and the state-machine integration;
  - Greenhouse hosted-form URL selection.
- **Implemented, not run:** the `cloud_run` backend, `setup_cloud_run.sh` and the container build. No GCP resources exist, and creating them needs approval and incurs costs.

## 6. Configuration changes

New optional `.env` keys, all inert by default:
- `BROWSER_WORKER` (`cloud_run` | `local` | empty)
- `BROWSER_WORKER_BUCKET`
- `BROWSER_WORKER_JOB`
- `BROWSER_WORKER_REGION`
- `BROWSER_WORKER_GCLOUD_CONFIG`
- `BROWSER_WORKER_TIMEOUT_SECONDS`
- `BROWSER_WORKER_PYTHON`, `BROWSER_WORKER_CHANNEL` (local backend)

`APPLY_MODE` stays `off`.

## 7. Tests executed and results

- **Full suite** (`.venv/bin/python -m unittest discover -s tests`, in the worktree): **294 tests, OK, 10 skipped.** The skipped ones are the opt-in real-browser tests.
- **Real-browser tests:**
  - **Command:**
    ```
    RUN_BROWSER_TESTS=1 BROWSER_WORKER_PYTHON=<python with playwright 1.63> python -m unittest tests.test_browser_worker
    ```
  - **Result: 20 tests, OK, in 69 s** on this VM. They cover:
    - submitted with confirmation (Greenhouse-like, react-select replica, Recruitee-like);
    - dry run, with no click;
    - visible hCaptcha challenge after submit gives `blocked`;
    - Turnstile waiting for a click gives `blocked` **before** submit;
    - Turnstile that passed silently is not a blocker;
    - an answer not in the employer's options gives `failed` before submit, with no click;
    - login wall gives `blocked`;
    - validation error gives `failed`;
    - no confirmation gives `uncertain`.
- **Integration (fake backend, real DB and state machine):**
  - one dispatch with exact fields;
  - `submitted` gives the job `applied`, approval consumed, no rerun;
  - challenge or login gives `blocked`, never retried;
  - unclear outcomes give manual follow-up;
  - incomplete package or unsupported system: not dispatched;
  - never dispatched with `APPLY_MODE=off`, without `browser`, or without a configured backend.

## 8. Live verification results (tested vs. assumed)

**Live-verified (read-only, 2026-11-06 16:20–16:40, headless Chromium 153 / Playwright 1.63 from this GCP VM):** no typing, no uploads, no submit clicks. User agent unchanged (`HeadlessChrome/153…`).

| Platform | Pages (employers) | Scripts loaded | Mode (from requests) | Challenge before submit | Access |
|---|---|---|---|---|---|
| Greenhouse | 11 (Adyen, JetBrains, Databricks, Thoughtworks, GitLab, Celonis, IMC, DEPT, Dataiku, Catawiki, Nebius) | reCAPTCHA Enterprise (10); **Cloudflare Turnstile (Nebius)** | invisible + score-based; Turnstile managed | **Nebius only:** "Verify you are human" checkbox, no token after 20 s (screenshot checked) | 200 on all |
| Ashby | 8 (Prosus ×2, ClickHouse, n8n, ElevenLabs, Miro, DeepL, LangChain, Polarsteps) | reCAPTCHA Enterprise | invisible + score-based; challenge frame present but hidden | none | 200 on all (Cloudflare CDN) |
| Lever | 3 (Jobgether, Samba TV, Mendix) | hCaptcha | invisible; challenge frames hidden (0×0) | none | 200 on all (Cloudflare CDN) |
| Recruitee | 3 (Eraneos, Conclusion, Centric) | hCaptcha self-hosted on `recruiteecdn.com` (Eraneos, Conclusion); **none (Centric)** | invisible (site config checked) | none | 200 on all |

**What cannot be known without submitting:**
- **reCAPTCHA Enterprise** returns a score that only the employer's server sees. A low score can lead to silent rejection, an error, or a fallback challenge.
- **Invisible hCaptcha** runs at submit and may show a puzzle.
- Headless browsers on datacenter IPs tend to score lower. **No real submission was made, so the rate is unknown.**
- I did not call the CAPTCHA APIs (for example `hcaptcha.execute()`) to "test" them: that would interfere with the bot protection.

**Live-verified Greenhouse form structure (JetBrains job 38, read-only):** single-choice questions are `input[role=combobox]`, and the multi-select is a checkbox list. The fixture `greenhouse_react.html` replicates this structure.

**Not verified:**
- the filler on a live form (permission needed, see summary item 6);
- the Cloud Run build and run;
- the GCS dispatch;
- any real submission.

**Shortlist** (English, NL/remote, a form the agent can read, no pre-submit challenge). The public Greenhouse question lists were fetched today.

| Rank | Job | Employer · role | Score | Required custom questions | Your input needed | Feasibility |
|---|---|---|---|---|---|---|
| **1** | **38** | JetBrains · (Senior) ML Developer (Junie): AI coding agent, LLMs | 85 | 2 | how you heard (from their list); preferred location | **Best:** strong AI/LLM fit, fewest answers; outcome decided by reCAPTCHA at submit |
| 2 | 15 | Adyen · Senior MLE – AML Tech | 89 | 2 | "Point of Data Transfer" acknowledgement (your call); NL or relocate + 3 office days | Good; classic production ML rather than LLM |
| 3 | 13 / 16 | Adyen · Senior MLE / Senior ML Scientist | 84 / 83 | 2 | same as 15 | Good |
| 4 | 8, 11 | Databricks · AI Forward Deployed Eng. / Lead SA GenAI | 80 / 90 | 5–6 | work authorization, sponsorship, prior employment, government-official declarations, source | Feasible after more answers |
| – | 17, 23, 24, 25, 33, 34 | Nebius (all) | 94–95 | – | – | **Not feasible unattended:** interactive Turnstile |
| – | 54 | Prosus · Senior MLE – LLMs (Ashby) | 95 | – | – | Best match overall, but needs an Ashby form reader first |
| – | 75 | Centric · Data & AI Architect (Recruitee, no CAPTCHA) | – | – | consent agreement; Dutch | CAPTCHA-free, but Dutch and a consent agreement |

## 9. Performance and resource considerations (costs)

- **Worker run:** about 60–120 s at 1 vCPU / 2 GiB.
  - **Measured on the VM** (no cloud run done): page load 7–25 s, 11.5 s on average over 26 pages; a mock form end to end 2–4 s.
- **Cloud Run Job:**
  - **Worst case with no free tier**, at the list prices I recall (USD 0.000024/vCPU-s, 0.0000025/GiB-s): about **USD 0.004 per application**.
  - **Free tier:** Cloud Run's monthly free tier (at least 180,000 vCPU-s / 360,000 GiB-s) covers about 1,500 runs.
  - **Verify both on the pricing page; the live page could not be read today.**
- **Artifact Registry:** the image is about 2 GB, roughly USD 0.15–0.20/month after the free 0.5 GB.
- **Cloud Build:** within the free minutes.
- **GCS:** about USD 0, since files are auto-deleted after 1 day.
- **Total:** under **USD 1/month**. Nothing runs between applications.
- **VM:** unchanged; `gcloud` runs for a few seconds per dispatch. The e2-medium is billed regardless. If you return to e2-micro, the cloud worker stays the only viable browser host.

## 10. Security and privacy checks

- **Bucket:** private, with public-access prevention and 1-day deletion. The package and CV are removed right after the run.
- **Service accounts:**
  - runtime SA: this bucket only;
  - dispatcher SA (the key on the VM, chmod 600, not in git): run this one job plus this bucket.
- **What leaves the VM:** only for an approved, eligible job, the verified field values plus the PDF.
- **Probe:** sends nothing but page loads. Screenshots and results stayed in the session scratchpad and are not committed.
- **No secrets, personal data, CVs or database rows** in the code, report or tests. The fixtures use `example.org` data.

## 11. Known limitations

- The submit-time outcome of invisible or score-based CAPTCHA is unknown until real attempts. Expect some `blocked`/`uncertain` results.
- Only Greenhouse and Recruitee forms are eligible: their questions are read from the employer. Ashby, Lever and Workday stay manual.
- Label-based filling covers the widget types seen on Greenhouse today. Unusual widgets stop the run before submit, which is safe but not filled.
- Optional fields not in the package (for example JetBrains' optional data-retention checkbox, EEO questions) are left empty.
- An org policy could block service-account key creation. The alternative, changing the VM scopes, needs a VM stop.

## 12. Outstanding issues / decisions needed

1. **Merge** this PR. It is inert until configured.
2. **Where the browser runs:** Cloud Run (recommended if the VM goes back to e2-micro) or the `local` backend on the current e2-medium (no new cloud resources).
3. **Profile:** set `verified: true` after review, and add for job 38: "How did you hear about this role?" (one of JetBrains' options) and "Which is your preferred location for this role?" (for example Amsterdam, Netherlands).
4. **Permission for one live dry run** (fake or real data, `submit: false`) on job 38's form.
5. **Approval for the first real submission** (job 38), then review the evidence.

## 13. Deployment instructions (not executed, require approval)

1. **Merge and deploy** as in previous reports:
   - back up the DB;
   - `git stash` the VM-local edits, fast-forward `main`, `git stash pop`;
   - run the tests;
   - restart only `job-hunter`.

   No new VM dependencies.
2. **Cloud Shell** (creates billable resources): run `bash worker/setup_cloud_run.sh` from a clone, then review the variables at the top. Copy the printed key to the VM as `data/gcloud-dispatch.json`, chmod 600, and delete it from Cloud Shell.
3. **VM:**
   ```
   CLOUDSDK_CONFIG=$PWD/data/gcloud gcloud auth activate-service-account --key-file=data/gcloud-dispatch.json
   ```
   Then set in `.env`: `BROWSER_WORKER=cloud_run`, `BROWSER_WORKER_BUCKET=jh-apply-vacation-price-checker`, `BROWSER_WORKER_JOB=apply-worker`, `BROWSER_WORKER_REGION=europe-west4`, `BROWSER_WORKER_GCLOUD_CONFIG=<repo>/data/gcloud`.
4. **Optional read-only probe from the Cloud Run egress:**
   ```
   gcloud run jobs execute apply-worker --region=europe-west4 --wait \
     --command=python --args=/worker/captcha_probe.py,https://job-boards.greenhouse.io/jetbrains/jobs/4937092101,--out,gs://<bucket>/probe.json
   ```
5. **Dry run** (with approval): one worker run with `submit: false` for job 38, then check the evidence screenshot.
6. **Live** (with approval): set `APPLY_MODE=live` and `APPLY_LIVE_ADAPTERS=browser`, restart, approve job 38. Then set `APPLY_MODE=off` again until the result is reviewed.

## 14. Rollback instructions

- **Instant:** `APPLY_MODE=off` or an empty `BROWSER_WORKER`, then restart.
- **Cloud:**
  ```
  gcloud run jobs delete apply-worker --region=europe-west4
  gcloud storage rm -r gs://<bucket>
  gcloud artifacts repositories delete job-hunter --location=europe-west4
  ```
  Delete both service accounts and revoke the key.
- **Code:** revert the merge commit. There are no DB migrations.

## 15. Recommended next steps

1. Merge.
2. Choose Cloud Run or local.
3. Complete the profile and the job 38 answers.
4. Live dry run on job 38.
5. One approved submission.
6. Depending on the outcome:
   - **if reCAPTCHA lets it through:** do Adyen 15 next (one approval each, no bulk);
   - **if blocked:** keep the supervised manual route for Greenhouse and focus unattended efforts on CAPTCHA-free forms (Centric-type Recruitee).
7. Then add an Ashby reader (Prosus 54).

## 16. PR URL and commits

https://github.com/majid-lotfian/job-hunter-agent/pull/13 · commits: 3d778cb (implementation, tests, report), then this rename commit.
