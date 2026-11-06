# On-demand application browser for Cloud Run Jobs (one task = one approved application, then exit).
# Official Playwright image: Chromium + system libraries preinstalled (no stealth/evasion tooling added).
FROM mcr.microsoft.com/playwright/python:v1.63.0-jammy
WORKDIR /worker
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY apply_worker.py captcha_probe.py ./
# Runs as the image's non-root user; Chromium keeps its sandbox.
USER pwuser
# Default: one application. Read-only CAPTCHA probe from the same cloud egress: --command=python --args=/worker/captcha_probe.py,URL,--out,gs://...
ENTRYPOINT ["python", "/worker/apply_worker.py"]
