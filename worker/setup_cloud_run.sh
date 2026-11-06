#!/usr/bin/env bash
# ONE-TIME setup of the on-demand application browser (run by the owner in Cloud Shell, NOT on the VM).
# Creates: a private bucket (1-day lifecycle), an image in Artifact Registry, a Cloud Run Job with a minimal
# service account, and a dispatcher service account whose key lets the VM start the job and use the bucket only.
# Review every line before running. Nothing here submits an application.
set -euo pipefail
PROJECT=${PROJECT:-vacation-price-checker}
REGION=${REGION:-europe-west4}          # Netherlands; Tier 1 pricing
BUCKET=${BUCKET:-jh-apply-${PROJECT}}
REPO=${REPO:-job-hunter}
JOB=${JOB:-apply-worker}
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/apply-worker:1"

gcloud config set project "$PROJECT"
gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com storage.googleapis.com

# 1. private bucket for packages/results/evidence, auto-deleted after 1 day
gcloud storage buckets create "gs://${BUCKET}" --location="$REGION" --uniform-bucket-level-access --public-access-prevention
printf '{"rule":[{"action":{"type":"Delete"},"condition":{"age":1}}]}' > /tmp/lifecycle.json
gcloud storage buckets update "gs://${BUCKET}" --lifecycle-file=/tmp/lifecycle.json

# 2. image (Cloud Build, from the worker/ folder of the repo)
gcloud artifacts repositories create "$REPO" --repository-format=docker --location="$REGION" || true
gcloud builds submit worker --tag "$IMAGE"

# 3. runtime identity of the job: may read/write only this bucket
gcloud iam service-accounts create apply-worker-run --display-name="Job Hunter apply worker (runtime)" || true
RUN_SA="apply-worker-run@${PROJECT}.iam.gserviceaccount.com"
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" --member="serviceAccount:${RUN_SA}" --role=roles/storage.objectAdmin

# 4. the job: 1 task, no retries (a retry could submit twice), 2 GiB / 1 vCPU, 10 min max
gcloud run jobs create "$JOB" --image="$IMAGE" --region="$REGION" --service-account="$RUN_SA" \
  --tasks=1 --max-retries=0 --task-timeout=600s --memory=2Gi --cpu=1

# 5. dispatcher identity used by the VM: start this job + use the bucket, nothing else
gcloud iam service-accounts create apply-worker-dispatch --display-name="Job Hunter apply worker (dispatch from VM)" || true
DISPATCH_SA="apply-worker-dispatch@${PROJECT}.iam.gserviceaccount.com"
gcloud run jobs add-iam-policy-binding "$JOB" --region="$REGION" --member="serviceAccount:${DISPATCH_SA}" --role=roles/run.developer
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" --member="serviceAccount:${DISPATCH_SA}" --role=roles/storage.objectAdmin
gcloud iam service-accounts add-iam-policy-binding "$RUN_SA" --member="serviceAccount:${DISPATCH_SA}" --role=roles/iam.serviceAccountUser
gcloud iam service-accounts keys create /tmp/apply-worker-dispatch.json --iam-account="$DISPATCH_SA"
echo "Copy /tmp/apply-worker-dispatch.json to the VM (data/gcloud-dispatch.json, chmod 600), then delete it here."
echo "Bucket: ${BUCKET}  Job: ${JOB}  Region: ${REGION}"
