/**
 * SCC — движок компакции для DeepSeek Harness (КАРКАС)
 * ======================================================
 *
 * Заменяет штатную компакцию `dsh-compaction-basic` на свёртку SCC.
 * Описан в ../ROLLOUT.md, разделы 3 и 4.
 *
 * СТАТУС: КАРКАС. Показывает форму модуля и точки входа. Фактическая связка
 * «сессия → поток событий → SCC → поверхностная замена» НЕ реализована.
 *
 * ФОРМА ПЛАГИНА (образец: dsh-plugin-ddg-search, dsh-command-compact)
 * --------------------------------------------------------------------
 *   export const name    — имя плагина
 *   export const inject  — какие сервисы нужны
 *   export function apply(ctx)  — монтирование
 *
 * ПОЧЕМУ СЕРВИС, А НЕ ПРОСТО СЛУШАТЕЛЬ
 * -------------------------------------
 * `compaction` — синглтон: вторая регистрация бросает
 * `service "compaction" has been registered at <...>`. Поэтому в патче
 * штатная строка ОТКЛЮЧАЕТСЯ, а не дополняется.
 *
 * ВАЖНО: `command-compact` (/compact) ждёт сервис `compaction`. Если наш движок
 * его не предоставит, команда не активируется (проверено: "pending (waiting for
 * service: compaction)"). Поэтому Service здесь обязателен, а не опционален.
 */

export const name = 'scc-compaction'

/**
 * ВНЕДРЯЕМЫЕ СЕРВИСЫ.
 *
 * ЖИВОЙ УРОК 2026-10-26. Здесь стояло `inject: ['sessions', 'compaction',
 * 'tokenMeter']` — то есть плагин ЖДАЛ сервис `compaction`. Но он же обязан его
 * ПРЕДОСТАВИТЬ (мы отключили штатную компакцию, чтобы занять её место).
 * Результат — плагин не активировался вовсе:
 *
 *     scc-compaction (...): pending (waiting for service: compaction)
 *
 * Свой движок НЕ ДОЛЖЕН внедрять `compaction`: он его регистрирует. Внедрять
 * нужно только то, чем движок пользуется.
 */
export const inject = ['sessions', 'tokenMeter']

export function apply(ctx, config = {}) {
  ctx.logger?.info?.('SCC compaction: монтирование', { config })

  // ── Параметры ─────────────────────────────────────────────────────────────
  const thresholdRatio = config.thresholdRatio ?? 0.7
  const sccStage = config.sccStage ?? 3
  const timeoutMs = config.sccTimeoutMs ?? 300000

  // ── Движок ────────────────────────────────────────────────────────────────
  class SccCompactionEngine {
    constructor(context) {
      this.ctx = context
      ctx.logger?.info?.('SCC compaction: движок создан', { thresholdRatio, sccStage })
    }

    /**
     * УСЛОВИЕ автокомпоновки. Вызывается на каждом `agent/pre-step`.
     *
     * Здесь решается: пора ли сжимать. Штатная логика считает долю занятого
     * окна; SCC добавляет своё — не сжимать то, что уже помечено как ядро.
     *
     * @returns результат компакции либо null, если сжатие не нужно
     */
    async compactIfNeeded(agent, trigger, signal) {
      // ШАГ 1. Измерить занятое окно (через tokenMeter).
      // ШАГ 2. Если доля ниже thresholdRatio — вернуть null.
      // ШАГ 3. Выбрать диапазон для сжатия:
      //        - исключить уже свёрнутое ядро (маркер в тексте события);
      //        - границы сбалансировать по tool-парам
      //          (toolPairingBalancedBefore / toolPairingBalancedAfter);
      //        - НЕ трогать узел 0 (system/message) — он защищён.
      // ШАГ 4. Собрать поток событий и вызвать ядро SCC (см. runScc ниже).
      // ШАГ 5. Выполнить поверхностную замену диапазона.
      ctx.logger?.info?.('SCC compaction: compactIfNeeded', { trigger })
      return null // каркас: ничего не делает
    }

    /** Ручной запуск (/compact). Требует ПРОСТОЯ агента. */
    async compactNow(agent, signal, sourceCommandId) {
      ctx.logger?.info?.('SCC compaction: compactNow', { sourceCommandId })
      return null
    }

    /** Принудительная замена диапазона. Требует ОТКРЫТОГО хода. */
    async compactRegion(start, end, agent, signal) {
      ctx.logger?.info?.('SCC compaction: compactRegion', { start, end })
      // Проверить: диапазон существует, не развёрнут, сбалансирован по парам.
      // Затем — поверхностная замена:
      //   session.append("user/message", summary, {
      //     surfaceOp: { op: 'replace', startSeq: start, endSeq: end },
      //     sourceEventSeqs: [...],   // плотно покрыть ВСЕ затеняемые узлы
      //   })
      throw new Error('SCC compaction: compactRegion не реализован (каркас)')
    }
  }

  // ── Вызов ядра SCC ──────────────────────────────────────────────────────────
  /**
   * Прогнать ядро SCC по потоку событий.
   *
   * ДВА СПОСОБА, оба допустимы:
   *
   *   1. ЧЕРЕЗ CLI (проще, но пишет во временный файл — для транзитной схемы
   *      это нарушение, см. раздел исследований):
   *        spawn('python3', ['core/scc_run.py', 'stage3', wr, filled,
   *                          '--to-stdout', '--no-ssc'])
   *
   *   2. ЧЕРЕЗ МОДУЛЬ (правильнее для транзита): ядро Python-совместимо, но
   *      вызывается из Node — нужен либо sidecar-процесс, либо порт ядра на JS.
   *      Для транзитной схемы «на диск не пишем» подходит ТОЛЬКО stdin/stdout.
   *
   * ВАЖНО: три этапа — это три вызова модели. 148 секунд на небольшом
   * контексте (измерено). Таймаут по умолчанию надо поднимать.
   */
  async function runScc(eventsText, signal) {
    // TODO: реализовать. Пока — заглушка, возвращающая вход без изменений,
    // чтобы каркас не ломал ход агента.
    ctx.logger?.warn?.('SCC compaction: runScc не реализован, вход возвращён как есть')
    return eventsText
  }

  // ── Регистрация ─────────────────────────────────────────────────────────────
  // Слушатель шага: точка, откуда штатная компакция вызывает compactIfNeeded.
  ctx.on('agent/pre-step', async ({ agent, signal }, next) => {
    if (!signal.aborted) {
      try {
        const result = await this.compactIfNeeded(agent, 'pressure', signal)
        if (result !== null) ctx.logger?.info?.('SCC compaction: сжатие выполнено')
      } catch (error) {
        // Ошибка сжатия НЕ должна ломать ход агента: компакция — обслуживание,
        // а не работа. Логируем и продолжаем.
        ctx.logger?.error?.('SCC compaction: ошибка', error)
      }
    }
    return next()
  })

  // ── Регистрация сервиса `compaction` ────────────────────────────────────────
  // ОБЯЗАТЕЛЬНО: `command-compact` (/compact) ждёт этот сервис. Без регистрации
  // команда не активируется, и это видно в предупреждениях запуска.
  //
  // В реальной реализации здесь будет:
  //   ctx.provide('compaction', new SccCompactionEngine(ctx))
  // Точная форма зависит от базового класса Service из @deepseek-ai/cordis —
  // в каркасе не воспроизводится, чтобы не давать неверного примера.
  ctx.logger?.info?.('SCC compaction: смонтирован (сервис compaction не зарегистрирован — каркас)')
}

export default { name, inject, apply }
