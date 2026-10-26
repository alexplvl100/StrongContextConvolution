#!/usr/bin/env python3
"""
SCC — тесты состояний методологии
===================================

ЧТО ПРОВЕРЯЕТСЯ
---------------
Состояние методологии (`METHODOLOGY_STATE.md`) существует **против потери
контекста агентом**. Если правило держится на «помни и не трогай», оно
обходится тем самым сбоем, от которого поставлено. Поэтому проверяется
машинно, и сам проверяющий обязан быть проверен.

Тесты покрывают четыре случая:

1. все ПРИНЯТ              -> код 0, реализация разрешена;
2. хотя бы одна В_РАБОТЕ   -> код 1, реализация **запрещена**;
3. состояние НЕ УКАЗАНО    -> код 2, и это **не** «разрешено»;
4. состояние НЕИЗВЕСТНО    -> код 2, неизвестность не равна разрешению.

Случай 3 — самый важный. «Отсутствует» легко прочитать как «ограничений нет»;
именно так правило и обходится.

ЖИВОЙ УРОК. При написании проверялся и сам скрипт: код возврата сначала
показался нулевым при «НЕ УКАЗАНО», но это была ошибка **измерения** — в
конвейере `$?` возвращал код `tail`, а не скрипта. Проверять надо код самого
скрипта, а не последней команды конвейера.

Запуск:
    python3 tests/test_state.py -v
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CHECKER = ROOT / "tools" / "check_state.py"

# Документы, которые скрипт обязан проверять. Дублируется здесь намеренно:
# если список в скрипте изменится, тест это заметит.
EXPECTED_DOCUMENTS = (
    "METHODOLOGY.md",
    "METHODOLOGY_CORE.md",
    "METHODOLOGY_MEMORY.md",
    "METHODOLOGY_CAS.md",
    "METHODOLOGY_HEAD.md",
    "METHODOLOGY_STATE.md",
)


def load_checker():
    """Загрузить check_state.py как модуль."""
    spec = importlib.util.spec_from_file_location("check_state", CHECKER)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_state"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class TestCheckerModule(unittest.TestCase):
    """Внутренние правила скрипта проверки."""

    def setUp(self) -> None:
        self.checker = load_checker()

    def test_two_states_only(self) -> None:
        """Состояний ровно два: В_РАБОТЕ и ПРИНЯТ."""
        self.assertEqual(self.checker.VALID_STATES, ("В_РАБОТЕ", "ПРИНЯТ"))

    def test_closed_document_list(self) -> None:
        """
        Список методологий закрытый и известный.

        Новый документ обязан попасть в список вместе с состоянием, иначе он
        не проверяется и правило контроля целостности его не видит.
        """
        self.assertEqual(tuple(self.checker.METHODOLOGIES), EXPECTED_DOCUMENTS)

    def test_state_document_in_list(self) -> None:
        """METHODOLOGY_STATE.md сам подчиняется состояниям: не исключение."""
        self.assertIn("METHODOLOGY_STATE.md", self.checker.METHODOLOGIES)

    def test_codes_distinct(self) -> None:
        """Три кода возврата различны: разрешено, запрещено, не измерено."""
        codes = {self.checker.CODE_OK, self.checker.CODE_BLOCKED, self.checker.CODE_UNMEASURED}
        self.assertEqual(len(codes), 3)

    def test_header_scoped_read(self) -> None:
        """
        Состояние читается из ШАПКИ, а не по всему тексту.

        В METHODOLOGY_STATE.md строка состояния есть ещё и в примере (§3.1).
        Поиск по всему файлу нашёл бы пример вместо шапки.
        """
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "doc.md"
            # в шапке состояния нет; глубже — есть (как пример в тексте)
            doc.write_text("текст без шапки\n" * 20 + "**Состояние:** ПРИНЯТ\n", encoding="utf-8")
            self.assertIsNone(
                self.checker.read_state(doc),
                "состояние прочитано из тела документа, а не из шапки",
            )
            doc.write_text("**Состояние:** ПРИНЯТ\n" + "текст\n" * 20, encoding="utf-8")
            self.assertEqual(self.checker.read_state(doc), "ПРИНЯТ")

    def test_unreadable_file_is_none(self) -> None:
        """Нечитаемый файл даёт None (не измерено), а не исключение."""
        missing = Path("/нет/такого/файла.md")
        self.assertIsNone(self.checker.read_state(missing))


class TestCheckerOnRealRepo(unittest.TestCase):
    """Скрипт на настоящем репозитории."""

    def test_all_accepted_now(self) -> None:
        """Текущее состояние всех методологий — ПРИНЯТ, реализация разрешена."""
        result = subprocess.run(
            [sys.executable, str(CHECKER), "--json"],
            capture_output=True, text=True, cwd=ROOT,
        )
        self.assertTrue(result.stdout.strip(), f"нет вывода: {result.stderr}")
        data = json.loads(result.stdout)
        self.assertTrue(data["implementation_allowed"], "реализация запрещена")
        self.assertEqual(data["code"], 0)
        for row in data["methodologies"]:
            self.assertEqual(row["verdict"], "ПРИНЯТ", f"{row['document']}: {row['verdict']}")

    def test_gate_silent_and_zero(self) -> None:
        """--gate молчит и возвращает 0, когда всё ПРИНЯТ."""
        result = subprocess.run(
            [sys.executable, str(CHECKER), "--gate"],
            capture_output=True, text=True, cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "", "--gate не должен ничего печатать")

    def test_every_documented_state_is_valid(self) -> None:
        """У каждого документа состояние из допустимого набора."""
        checker = load_checker()
        for name in EXPECTED_DOCUMENTS:
            state = checker.read_state(ROOT / name)
            self.assertIn(state, checker.VALID_STATES, f"{name}: состояние {state!r}")


class TestGateBlocks(unittest.TestCase):
    """
    Проверка ЗАПРЕТА на копии репозитория.

    Тесты мутируют документы, поэтому работают с копией: настоящую методологию
    они не трогают. Это тот же принцип, что и в rollout DSH.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        shutil.copytree(
            ROOT, self.repo,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
        )
        self.checker = self.repo / "tools" / "check_state.py"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _gate(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(self.checker), "--gate"],
            capture_output=True, text=True, cwd=self.repo,
        )

    def _set_state(self, document: str, state: str) -> None:
        """Заменить состояние в шапке документа."""
        path = self.repo / document
        text = path.read_text(encoding="utf-8")
        for old in ("ПРИНЯТ", "В_РАБОТЕ"):
            text = text.replace(f"**Состояние:** {old}", f"**Состояние:** {state}", 1)
        path.write_text(text, encoding="utf-8")

    def test_all_accepted_allows(self) -> None:
        """Все ПРИНЯТ — код 0."""
        self.assertEqual(self._gate().returncode, 0)

    def test_work_blocks_all(self) -> None:
        """
        Одна методология в В_РАБОТЕ запрещает реализацию ПОЛНОСТЬЮ.

        Правило контроля целостности: методологии связаны, и реализовывать по
        «остальным» документам, пока один меняется, значит строить на подвижном
        основании.
        """
        self._set_state("METHODOLOGY_HEAD.md", "В_РАБОТЕ")
        result = self._gate()
        self.assertEqual(result.returncode, 1, "реализация не заблокирована")

    def test_work_blocks_regardless_of_which_document(self) -> None:
        """Блокирует любая методология, не только HEAD."""
        for document in EXPECTED_DOCUMENTS:
            with self.subTest(document=document):
                # откатить предыдущую правку
                self._set_state(document, "ПРИНЯТ")
                self._set_state(document, "В_РАБОТЕ")
                self.assertEqual(self._gate().returncode, 1, f"{document} не блокирует")
                self._set_state(document, "ПРИНЯТ")

    def test_missing_state_is_not_permission(self) -> None:
        """
        Отсутствие состояния даёт код 2, а не 0.

        Это самое важное свойство: «нет ограничений» и «неизвестно» — разные
        вещи, и путать их значит обходить правило.
        """
        path = self.repo / "METHODOLOGY_CAS.md"
        text = path.read_text(encoding="utf-8")
        text = text.replace("**Состояние:** ПРИНЯТ\n", "", 1)
        path.write_text(text, encoding="utf-8")
        result = self._gate()
        self.assertEqual(result.returncode, 2, "отсутствие состояния принято за разрешение")

    def test_unknown_state_is_not_permission(self) -> None:
        """Неизвестное состояние даёт код 2."""
        self._set_state("METHODOLOGY_MEMORY.md", "ЧТО_ТО")
        self.assertEqual(self._gate().returncode, 2)

    def test_missing_document_is_not_permission(self) -> None:
        """Пропавший документ методологии даёт код 2."""
        (self.repo / "METHODOLOGY_CORE.md").unlink()
        self.assertEqual(self._gate().returncode, 2)


def main() -> int:
    """Запустить тесты с кодом возврата по соглашению SeverPact."""
    if not CHECKER.exists():
        print(f"НЕ ИЗМЕРЕНО: нет скрипта {CHECKER}", file=sys.stderr)
        return 2
    verbosity = 2 if "-v" in sys.argv else 1
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
