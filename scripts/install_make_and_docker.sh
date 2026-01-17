#!/usr/bin/env bash
# Install make and Docker on Ubuntu/Debian, fix docker group permissions.
# Usage: ./scripts/install_make_and_docker.sh
# After running, re-login or `newgrp docker` to apply group changes.

set -euo pipefail

if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi

install_make() {
  echo "[setup] Installing make (and deps)"
  sudo apt-get update
  sudo apt-get install -y make
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1
}

install_docker() {
  echo "[setup] Installing Docker"
  sudo apt-get update
  sudo apt-get install -y ca-certificates curl gnupg lsb-release
  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io
}

ensure_make() {
  if require_cmd make; then
    echo "[check] make already installed"
  else
    install_make
  fi
}

ensure_docker() {
  if require_cmd docker; then
    echo "[check] Docker already installed"
  else
    install_docker
  fi
}

fix_permissions() {
  echo "[setup] Adding user to docker group (if not already)"
  sudo usermod -aG docker "$USER" || true
  echo "[info] Re-login your shell or run: newgrp docker"
}

main() {
  ensure_make
  ensure_docker
  fix_permissions
  echo "[done] make + Docker setup complete."
}

main "$@"
