#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_ROOT="${BACKUP_ROOT:-${ROOT}/../backups}"
STAMP="$(date +%Y%m%d)"
DEST="${BACKUP_ROOT}/${STAMP}"

mkdir -p "${DEST}/cookies" "${DEST}/storage_state" "${DEST}/state/profiles"
chmod 700 "${BACKUP_ROOT}" "${DEST}"
chmod 700 "${DEST}/cookies" "${DEST}/storage_state" "${DEST}/state/profiles"

for item in cookies storage_state; do
  src="${ROOT}/${item}"
  if [[ -d "${src}" ]]; then
    cp -a "${src}/." "${DEST}/${item}/"
  fi
done

if [[ -d "${ROOT}/state/profiles" ]]; then
  cp -a "${ROOT}/state/profiles/." "${DEST}/state/profiles/"
fi

find "${DEST}" -type f -exec chmod 600 {} +
find "${BACKUP_ROOT}" -mindepth 1 -maxdepth 1 -type d -mtime +14 -exec rm -rf {} +

echo "backup: ${DEST}"
