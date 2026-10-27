#!/usr/bin/env bash
# SCC — изолированный запуск DSH для разработки метода компакции
# =================================================================
#
# ГЛАВНОЕ ПРАВИЛО ПРОЕКТА: исследования НЕ затрагивают текущую конфигурацию
# харнесса (../ROLLOUT.md §0). Этот скрипт — механизм, который это обеспечивает.
#
# Всё делается в отдельном DSH_HOME. Рабочий ~/.dsh не открывается на
# запись НИ РАЗУ: ключи только копируются.
#
# ИСПОЛЬЗОВАНИЕ
#   ./isolated_run.sh setup              создать изолированное окружение
#   ./isolated_run.sh dump               показать собранную конфигурацию
#   ./isolated_run.sh run "запрос"       выполнить прогон
#   ./isolated_run.sh check              проверить, что рабочая конфигурация цела
#   ./isolated_run.sh clean              удалить окружение (полный откат)
#
# КОДЫ ВОЗВРАТА (соглашение SeverPact)
#   0 — успех
#   1 — нарушение (рабочая конфигурация изменена!)
#   2 — НЕ ИЗМЕРЕНО (окружение не готово, нет ключа)

set -euo pipefail

WORK="${SCC_WORK:-/tmp/scc_iso}"
HOME_DIR="$WORK/home"
PROFILE="${SCC_PROFILE:-scc-test}"
PATCH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/method.patch.yml"
# Каталог плагина: в патче путь зашит как /tmp/scc_iso/plugin/index.js. Здесь
# вычисляется фактический, и при расхождении патч копируется с подстановкой —
# так файл в репозитории остаётся с примером, а запуск работает из любого места.
PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EFFECTIVE_PATCH="$WORK/method.effective.yml"

# ── Рабочее развёртывание, которое НЕЛЬЗЯ трогать ────────────────────────────
LIVE_HOME="${DSH_HOME:-$HOME/.dsh}"
LIVE_PROFILE="${DSH_PROFILE:-web}"
LIVE_PATCH="$LIVE_HOME/profiles/$LIVE_PROFILE/cordis.patch.yml"
LIVE_URL="${DSH_WEB_URL:-http://127.0.0.1:3080}"

die() { echo "НАРУШЕНИЕ: $*" >&2; exit 1; }
unmeasured() { echo "НЕ ИЗМЕРЕНО: $*" >&2; exit 2; }

# ── Защита: отказ работать, если WORK указывает на рабочий home ──────────────
guard_paths() {
  case "$(readlink -f "$WORK" 2>/dev/null || echo "$WORK")" in
    "$(readlink -f "$LIVE_HOME" 2>/dev/null || echo "$LIVE_HOME")"*)
      die "WORK ($WORK) указывает внутрь рабочего home ($LIVE_HOME). Это остановило бы исследования" ;;
  esac
  [ "$WORK" = "/" ] && die "WORK не может быть корнем файловой системы"
  [ -z "$WORK" ] && die "WORK пуст"
  # Явный return обязателен: последняя проверка ложна в нормальном случае, и
  # без него функция вернула бы 1, а `set -e` завершил бы скрипт. Живой урок
  # 2026-10-26 — из-за этого setup молча не выполнялся.
  return 0
}

# ── Проверка, что рабочая конфигурация цела ──────────────────────────────────
check_live() {
  local status=0
  echo "── рабочая конфигурация ──"
  if [ -f "$LIVE_PATCH" ]; then
    echo "  $LIVE_PATCH"
    echo "  изменён: $(stat -c '%y' "$LIVE_PATCH" | cut -d. -f1)"
  else
    echo "  $LIVE_PATCH — НЕ НАЙДЕН"; status=1
  fi
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$LIVE_URL" || echo 000)"
  echo "  GUI $LIVE_URL — HTTP $code"
  # 401/200 означают, что сервер жив; 000 — не отвечает
  if [ "$code" = "000" ]; then echo "  сервер НЕ отвечает"; status=1; fi
  return $status
}

cmd_setup() {
  guard_paths
  echo "── создание изолированного окружения ──"
  mkdir -p "$HOME_DIR"

  # Профиль из шаблона. Имена шаблонов: acp, headless, sdk, sdk-minimal, web.
  # headless выбран потому, что в нём compaction-basic лежит на ВЕРХНЕМ уровне
  # дерева и адресуется патчем (в web он спрятан в пресете standard).
  if [ ! -d "$HOME_DIR/profiles/$PROFILE" ]; then
    echo "  профиль $PROFILE из шаблона headless"
    # --dump-config обязателен: без него dsh требует задачу и завершается
    # сообщением «a task is required». Нам нужен только сам профиль, а не прогон.
    DSH_HOME="$HOME_DIR" dsh --from-default-profile headless --profile "$PROFILE" --dump-config >/dev/null
  else
    echo "  профиль $PROFILE уже есть"
  fi

  # Ключи — КОПИЯ. Источник открывается только на чтение.
  if [ -f "$LIVE_HOME/.credentials.yaml" ] && [ ! -f "$HOME_DIR/.credentials.yaml" ]; then
    cp "$LIVE_HOME/.credentials.yaml" "$HOME_DIR/.credentials.yaml"
    chmod 600 "$HOME_DIR/.credentials.yaml"
    echo "  ключи скопированы (источник не изменён)"
  fi

  echo "  готово: $HOME_DIR"
  check_live || die "рабочая конфигурация уже нарушена до начала работ"
}

# Подготовить патч с фактическим путём к плагину.
prepare_patch() {
  mkdir -p "$WORK"
  if [ "$PLUGIN_DIR" = "/tmp/scc_iso/plugin" ]; then
    cp "$PATCH" "$EFFECTIVE_PATCH"        # путь совпадает, копия не нужна
  else
    sed "s#/tmp/scc_iso/plugin#${PLUGIN_DIR}#g" "$PATCH" > "$EFFECTIVE_PATCH"
  fi
  echo "$EFFECTIVE_PATCH"
}

cmd_dump() {
  [ -d "$HOME_DIR" ] || unmeasured "окружение не создано: выполните setup"
  guard_paths
  DSH_HOME="$HOME_DIR" dsh --profile "$PROFILE" --patch "$(prepare_patch)" --dump-config
}

cmd_run() {
  [ -d "$HOME_DIR" ] || unmeasured "окружение не создано: выполните setup"
  guard_paths
  [ -n "${ROUTERAI_API_KEY:-}" ] || unmeasured "нет ROUTERAI_API_KEY"
  [ $# -gt 0 ] || unmeasured "нужен текст запроса"
  DSH_HOME="$HOME_DIR" dsh --profile "$PROFILE" --patch "$(prepare_patch)" run "$*"
}

cmd_clean() {
  guard_paths
  echo "── откат ──"
  rm -rf "$WORK"
  echo "  удалено: $WORK"
  echo "  рабочая конфигурация не затрагивалась"
}

case "${1:-}" in
  setup) cmd_setup ;;
  dump)  shift; cmd_dump "$@" ;;
  run)   shift; cmd_run "$@" ;;
  check) check_live ;;
  clean) cmd_clean ;;
  *) sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; exit 2 ;;
esac
