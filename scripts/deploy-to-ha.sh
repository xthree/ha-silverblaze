#!/usr/bin/env bash
# Deploy the integration to a Home Assistant host over SSH (tar over ssh).
set -euo pipefail

DEFAULT_HOST="ha"
DEFAULT_CONFIG_DIR="/config"

usage() {
  cat <<USAGE
Usage: $(basename "$0") [host] [config-dir]

Copies custom_components/silverblaze to <config-dir>/custom_components/silverblaze
on <host> over SSH. Does not restart Home Assistant.

Arguments:
  host        SSH host (default: ${DEFAULT_HOST})
  config-dir  Home Assistant config directory on the host (default: ${DEFAULT_CONFIG_DIR})

Options:
  -h, --help  Show this help

The remote side uses sudo unless the remote user is root.
USAGE
}

case "${1:-}" in
  -h | --help)
    usage
    exit 0
    ;;
esac

HOST="${1:-$DEFAULT_HOST}"
CONFIG_DIR="${2:-$DEFAULT_CONFIG_DIR}"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -d "${SRC_DIR}/custom_components/silverblaze" ]]; then
  echo "custom_components/silverblaze not found" >&2
  exit 1
fi

# Remote script: runs under sh, reads the tar stream from stdin, extracts to a
# temp dir next to the target, then swaps it into place. Everything in single
# quotes expands on the remote side.
# shellcheck disable=SC2016
REMOTE_SCRIPT='
set -eu
config_dir="$1"
if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi
target="$config_dir/custom_components"
tmp="$target/.silverblaze.new"
$SUDO mkdir -p "$tmp"
$SUDO rm -rf "$tmp"
$SUDO mkdir -p "$tmp"
$SUDO tar -xf - -C "$tmp"
$SUDO rm -rf "$target/silverblaze"
$SUDO mv "$tmp/silverblaze" "$target/silverblaze"
$SUDO rm -rf "$tmp"
'

tar -C "${SRC_DIR}/custom_components" \
  --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='tests' --exclude='test_*.py' --exclude='*_test.py' --exclude='conftest.py' \
  -cf - silverblaze |
  ssh "${HOST}" sh -c "'${REMOTE_SCRIPT}'" sh "$(printf %q "${CONFIG_DIR}")"

echo "Deployed custom_components/silverblaze to ${HOST}:${CONFIG_DIR}/custom_components/silverblaze"
echo "Restart Home Assistant to load the new version"
