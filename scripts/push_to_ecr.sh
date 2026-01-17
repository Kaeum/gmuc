#!/usr/bin/env bash
# Build/tag/push gmuc:latest to ECR. Requires aws cli and docker login permissions.
# Usage:
#   REGION=ap-northeast-2 ACCOUNT_ID=123456789012 ./scripts/push_to_ecr.sh
# Optionally override IMAGE (default gmuc:latest) and REPO (default gmuc).

if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi

REGION=ap-northeast-2
ACCOUNT_ID=005592596386
IMAGE="${IMAGE:-gmuc:latest}"
REPO="${REPO:-gmuc}"
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

echo "[ECR] Repository: ${REPO} in ${REGION}"
aws ecr create-repository --repository-name "${REPO}" --region "${REGION}" >/dev/null 2>&1 || true

echo "[ECR] Login"
aws ecr get-login-password --region "${REGION}" | docker login --username AWS --password-stdin "${ECR_URI}"

echo "[ECR] Tagging ${IMAGE} -> ${ECR_URI}/${REPO}:latest"
docker tag "${IMAGE}" "${ECR_URI}/${REPO}:latest"

echo "[ECR] Pushing"
docker push "${ECR_URI}/${REPO}:latest"

echo "[done] Pushed to ${ECR_URI}/${REPO}:latest"
