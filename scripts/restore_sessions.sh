#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_ROOT="${BACKUP_ROOT:-${ROOT}/../backups}"
DATE="${1:-}"
ACCOUNT="${2:-}"

if [[ ! "${DATE}" =~ ^[0-9]{8}$ ]]; then
  echo "usage: $0 <date> [account]" >&2
  exit 2
fi

if [[ -n "${ACCOUNT}" && ( "${ACCOUNT}" == */* || "${ACCOUNT}" == "." || "${ACCOUNT}" == ".." ) ]]; then
  echo "invalid account name: ${ACCOUNT}" >&2
  exit 2
fi

SRC="${BACKUP_ROOT}/${DATE}"
if [[ ! -d "${SRC}" ]]; then
  echo "backup not found: ${SRC}" >&2
  exit 1
fi

RESTORED=0

restore_dir() {
  local name="$1"
  local target="${ROOT}/${name}"
  mkdir -p "${target}"
  if [[ -n "${ACCOUNT}" ]]; then
    if [[ -f "${SRC}/${name}/${ACCOUNT}.json" ]]; then
      cp -a "${SRC}/${name}/${ACCOUNT}.json" "${target}/"
      RESTORED=$((RESTORED + 1))
    fi
  elif [[ -d "${SRC}/${name}" ]]; then
    find "${SRC}/${name}" -mindepth 1 -maxdepth 1 -exec cp -a {} "${target}/" \;
  fi
}

restore_dir cookies
restore_dir storage_state
if [[ -d "${SRC}/state/profiles" ]]; then
  mkdir -p "${ROOT}/state/profiles"
  if [[ -n "${ACCOUNT}" ]]; then
    if [[ -f "${SRC}/state/profiles/${ACCOUNT}.json" ]]; then
      cp -a "${SRC}/state/profiles/${ACCOUNT}.json" "${ROOT}/state/profiles/"
      RESTORED=$((RESTORED + 1))
    fi
  else
    find "${SRC}/state/profiles" -mindepth 1 -maxdepth 1 -exec cp -a {} "${ROOT}/state/profiles/" \;
  fi
fi

if [[ -n "${ACCOUNT}" ]]; then
  if [[ "${RESTORED}" -eq 0 ]]; then
    echo "no session files found for account ${ACCOUNT} in ${SRC}" >&2
    exit 1
  fi
  echo "restored account ${ACCOUNT} from ${SRC}"
else
  echo "restored from ${SRC}"
fi
