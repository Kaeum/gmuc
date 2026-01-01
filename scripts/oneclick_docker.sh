#!/usr/bin/env bash
# One-click Docker setup + build + optional run for GMUC (Ubuntu/Debian)
# Usage:
#   ./scripts/oneclick_docker.sh            # Docker 설치(필요시) + 이미지 빌드
#   ./scripts/oneclick_docker.sh --install-only   # Docker만 설치
# 실행(예약 파라미터 전달)은 별도: make docker-run ARGS='...'

set -euo pipefail

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || return 1
}

install_docker() {
  echo "[setup] Docker 미설치 → 설치 진행"
  sudo apt-get update
  sudo apt-get install -y ca-certificates curl gnupg lsb-release
  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io
  sudo usermod -aG docker "$USER" || true
  echo "[setup] Docker 설치 완료 (그룹 반영을 위해 재로그인이 필요할 수 있습니다)"
}

ensure_docker() {
  if require_cmd docker; then
    echo "[check] Docker already installed"
  else
    install_docker
  fi
}

build_image() {
  local image=${1:-gmuc:latest}
  echo "[build] Building image ${image}"
  docker build -t "${image}" .
}

main() {
  cd "$(dirname "$0")/.."  # repo root
  if [ "${1:-}" = "--install-only" ]; then
    ensure_docker
    exit 0
  fi
  ensure_docker
  build_image "${IMAGE:-gmuc:latest}"
  echo "[info] 이미지 빌드 완료: ${IMAGE:-gmuc:latest}"
  echo "[info] 실행은 별도로: make docker-run ARGS='--id ... --password ... --exec-at \"...\" --reservation \"...\"'"
}

main "$@"
