#!/usr/bin/env bash
# scripts/install_systemd.sh — ставит systemd-юниты проекта под ТЕКУЩЕГО
# пользователя и ТЕКУЩИЙ каталог проекта (в шаблонах scripts/systemd/*.service
# нет вшитых путей: @USER@, @GROUP@, @ROOT@ подставляются здесь).
#
#   sudo scripts/install_systemd.sh                  # поставить юниты, daemon-reload
#   sudo scripts/install_systemd.sh --enable         # + включить таймеры и демон ротации
#   scripts/install_systemd.sh --out /tmp/units      # только отрисовать в каталог (без root/systemctl)
#
# Опции:
#   --user NAME   от чьего имени работают джобы (по умолчанию $SUDO_USER, иначе текущий)
#   --root DIR    каталог проекта (по умолчанию — родитель каталога scripts/)
#   --enable      enable --now для refresh-cookies.timer, refresh-guest-cookies.timer, rotation-daemon
#   --no-login    не ставить refresh-cookies (логин-аккаунты не нужны)
#   --out DIR     писать юниты в DIR вместо /etc/systemd/system и не трогать systemctl
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
ENABLE=0
LOGIN=1
OUT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) RUN_USER="$2"; shift 2 ;;
    --root) [[ -d "$2" ]] || { echo "ОШИБКА: нет каталога $2 (--root)" >&2; exit 1; }
            ROOT="$(cd "$2" && pwd)"; shift 2 ;;
    --enable) ENABLE=1; shift ;;
    --no-login) LOGIN=0; shift ;;
    --out) OUT="$2"; shift 2 ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "неизвестная опция: $1" >&2; exit 2 ;;
  esac
done

DEST="${OUT:-/etc/systemd/system}"
if [[ -z "$OUT" ]]; then
  [[ $EUID -eq 0 ]] || { echo "ОШИБКА: нужен root (запусти через sudo) или укажи --out DIR" >&2; exit 1; }
  id "$RUN_USER" >/dev/null 2>&1 || { echo "ОШИБКА: нет пользователя '$RUN_USER' (--user)" >&2; exit 1; }
  RUN_GROUP="$(id -gn "$RUN_USER")"
else
  mkdir -p "$OUT"
  RUN_GROUP="$(id -gn "$RUN_USER" 2>/dev/null || echo "$RUN_USER")"
fi

# --- предупреждения о том, чего не хватает (не фатально: юниты всё равно ставятся) ---
[[ -x "$ROOT/venv/bin/python3" ]] || echo "ВНИМАНИЕ: нет $ROOT/venv/bin/python3 — создай venv (README, раздел 'Установка')" >&2
for bin in flock xvfb-run; do
  command -v "$bin" >/dev/null 2>&1 || echo "ВНИМАНИЕ: нет '$bin' (apt install util-linux xvfb)" >&2
done

UNITS=(refresh-guest-cookies.service refresh-guest-cookies.timer rotation-daemon.service)
if [[ $LOGIN -eq 1 ]]; then
  UNITS+=(refresh-cookies.service refresh-cookies.timer)
fi

for unit in "${UNITS[@]}"; do
  src="$SCRIPT_DIR/systemd/$unit"
  [[ -f "$src" ]] || { echo "ОШИБКА: нет шаблона $src" >&2; exit 1; }
  content="$(cat "$src")"
  content="${content//@USER@/$RUN_USER}"
  content="${content//@GROUP@/$RUN_GROUP}"
  content="${content//@ROOT@/$ROOT}"
  if grep -q '@[A-Z]*@' <<<"$content"; then
    echo "ОШИБКА: в $unit остались неподставленные плейсхолдеры" >&2; exit 1
  fi
  printf '%s\n' "$content" > "$DEST/$unit"
  echo "OK: $DEST/$unit  (user=$RUN_USER, root=$ROOT)"
done

if [[ -n "$OUT" ]]; then
  echo "Юниты отрисованы в $OUT (systemctl не вызывался)."
  exit 0
fi

systemctl daemon-reload
if [[ $ENABLE -eq 1 ]]; then
  TO_ENABLE=(refresh-guest-cookies.timer rotation-daemon.service)
  [[ $LOGIN -eq 1 ]] && TO_ENABLE+=(refresh-cookies.timer)
  systemctl enable --now "${TO_ENABLE[@]}"
  echo "Включено: ${TO_ENABLE[*]}"
else
  echo "Юниты установлены, но не включены. Включить:"
  echo "  sudo systemctl enable --now refresh-guest-cookies.timer rotation-daemon.service$([[ $LOGIN -eq 1 ]] && echo ' refresh-cookies.timer')"
fi
