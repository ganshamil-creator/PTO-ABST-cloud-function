"""
verify_table.py
================
Проверка арифметической согласованности таблицы, извлечённой OpenDataLoader
(или любым другим парсером, отдающим ту же структуру rows/cells/content).

Идея: не доверять вытащенным числам вслепую. Если в таблице есть строка или
колонка "Итого"/"Всего" — она должна сходиться с суммой компонентов. Если не
сходится — либо ошибка распознавания, либо реальная неточность в самом
чертеже/спецификации, и то и другое стоит показать инженеру, а не проглотить
молча.

Не пытается понять СМЫСЛ таблицы — только внутреннюю арифметику. Это
дополняет, а не заменяет, сверку с независимым источником (например,
масса/плотность против размеров — см. пример со сваями в takeoff_pipeline.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_TOTAL_WORDS = ("итого", "всего", "total", "сумма")
_NUM_RE = re.compile(r"^-?\d[\d\s]*[.,]?\d*$")


def _cell_text(cell: dict) -> str:
    """Достаёт текст ячейки OpenDataLoader (поле content, возможно вложенное)."""
    if not isinstance(cell, dict):
        return ""
    if "content" in cell:
        return str(cell["content"]).strip()
    out = []
    for kid in cell.get("kids") or []:
        t = _cell_text(kid)
        if t:
            out.append(t)
    return " ".join(out).strip()


def _to_number(text: str) -> float | None:
    text = text.strip()
    if not _NUM_RE.fullmatch(text):
        return None
    try:
        return float(text.replace(" ", "").replace(",", "."))
    except ValueError:
        return None


@dataclass
class CheckResult:
    kind: str            # "row" | "column"
    index: int            # номер строки/колонки с итогом
    label: str             # текст ячейки-подписи ("Итого", "Всего" и т.п.)
    stated: float
    computed: float
    match: bool
    tolerance: float = 0.05


@dataclass
class VerifyReport:
    table_rows: int
    table_cols: int
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def all_match(self) -> bool:
        return all(c.match for c in self.checks)

    @property
    def mismatches(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.match]


def _grid(table: dict) -> list[list[str]]:
    """Строит правильную сетку текстов, используя явные row/column number и
    row/column span из OpenDataLoader — а не порядковую позицию ячейки в
    списке, которая сбивается при объединённых ячейках (colspan/rowspan)."""
    rows_meta = table.get("rows", [])
    nrows = len(rows_meta)
    ncols = 0
    for row in rows_meta:
        for cell in row.get("cells", []):
            c = cell.get("column number", 1) or 1
            span = cell.get("column span", 1) or 1
            ncols = max(ncols, c + span - 1)
    grid = [["" for _ in range(ncols)] for _ in range(nrows)]
    for ri, row in enumerate(rows_meta):
        for cell in row.get("cells", []):
            r = (cell.get("row number", ri + 1) or ri + 1) - 1
            c = (cell.get("column number", 1) or 1) - 1
            txt = _cell_text(cell)
            if 0 <= r < nrows and 0 <= c < ncols:
                grid[r][c] = txt
    return grid


def verify_table(table: dict, tolerance_ratio: float = 0.005) -> VerifyReport:
    grid = _grid(table)
    nrows = len(grid)
    ncols = max((len(r) for r in grid), default=0)
    report = VerifyReport(table_rows=nrows, table_cols=ncols)

    # --- строки "Итого"/"Всего": сумма чисел в колонке ВЫШЕ до предыдущего
    #     нечислового/пустого разрыва должна совпасть со значением в этой строке
    for ri, row in enumerate(grid):
        label_cell = next((c for c in row if c), "")
        if not any(w in label_cell.lower() for w in _TOTAL_WORDS):
            continue
        for ci in range(ncols):
            stated_text = row[ci] if ci < len(row) else ""
            stated = _to_number(stated_text)
            if stated is None:
                continue
            # собираем числа выше по этой колонке, пока не упрёмся в другую
            # строку-итог, пустую строку или начало таблицы
            components = []
            for rj in range(ri - 1, -1, -1):
                above_row = grid[rj]
                above_label = next((c for c in above_row if c), "")
                if any(w in above_label.lower() for w in _TOTAL_WORDS):
                    break
                val_text = above_row[ci] if ci < len(above_row) else ""
                val = _to_number(val_text)
                if val is None:
                    if val_text == "":
                        continue  # пустая ячейка — пропускаем, не обрываем
                    break  # нечисловой текст — вероятно, конец числового блока
                components.append(val)
            if not components:
                continue
            computed = sum(components)
            tol = max(tolerance_ratio * abs(stated), 0.05)
            report.checks.append(CheckResult(
                kind="row", index=ri, label=label_cell,
                stated=stated, computed=round(computed, 3),
                match=abs(stated - computed) <= tol, tolerance=tol,
            ))

    # --- колонки "Итого"/"Всего": заголовок таких колонок может быть НЕ в первой
    #     строке — у сложных таблиц бывает несколько уровней шапки. Ищем ЛЮБУЮ
    #     строку, где 1+ ячейка содержит "итого"/"всего" — это разметка колонки,
    #     а строки НИЖЕ неё (до следующей такой же разметки) — данные для сверки.
    header_positions: list[tuple[int, int, str]] = []  # (row_idx, col_idx, label)
    for ri, row in enumerate(grid):
        for ci, cell in enumerate(row):
            if cell and any(w in cell.lower() for w in _TOTAL_WORDS):
                header_positions.append((ri, ci, cell))

    # сортируем по колонке слева направо — так соседние итоговые колонки образуют
    # непересекающиеся сегменты (подытог считает только "свои" листовые колонки,
    # а не всё подряд, иначе вложенные подытоги задваиваются в общем "Всего")
    header_positions_sorted = sorted(header_positions, key=lambda p: p[1])
    for idx, (hdr_ri, ci, label) in enumerate(header_positions_sorted):
        segment_start = header_positions_sorted[idx - 1][1] + 1 if idx > 0 else 0
        for ri in range(hdr_ri + 1, nrows):
            row = grid[ri]
            row_label = next((c for c in row if c), "")
            if any(w in row_label.lower() for w in _TOTAL_WORDS) and ci >= len(row):
                continue
            stated_text = row[ci] if ci < len(row) else ""
            stated = _to_number(stated_text)
            if stated is None:
                continue
            components = []
            for cj in range(segment_start, ci):
                val = _to_number(row[cj] if cj < len(row) else "")
                if val is not None:
                    components.append(val)
            if not components and idx > 0:
                # сегмент пуст — вероятно, это "Всего" сразу после подытогов
                # (без сырых колонок между ними). Тогда компоненты — это сами
                # предыдущие подытоговые колонки, а не пустой промежуток.
                prev_cols = [p[1] for p in header_positions_sorted[:idx]]
                for cj in prev_cols:
                    val = _to_number(row[cj] if cj < len(row) else "")
                    if val is not None:
                        components.append(val)
            if not components:
                continue
            computed = sum(components)
            tol = max(tolerance_ratio * abs(stated), 0.05)
            report.checks.append(CheckResult(
                kind="column", index=ci, label=label,
                stated=stated, computed=round(computed, 3),
                match=abs(stated - computed) <= tol, tolerance=tol,
            ))

    return report


if __name__ == "__main__":
    import sys
    import json
    with open(sys.argv[1], encoding="utf-8") as f:
        odl_json = json.load(f)

    def find_tables(node, results):
        if not isinstance(node, dict):
            return
        if node.get("type") == "table":
            results.append(node)
        for kid in node.get("kids") or []:
            find_tables(kid, results)

    tables = []
    find_tables(odl_json, tables)
    print(f"Найдено таблиц: {len(tables)}")
    for t in tables:
        rep = verify_table(t)
        if rep.checks:
            print(f"\nТаблица {t.get('id')} — {rep.table_rows}x{rep.table_cols}, проверок: {len(rep.checks)}")
            for c in rep.checks:
                status = "OK" if c.match else "!! РАСХОЖДЕНИЕ"
                print(f"  [{status}] {c.kind} '{c.label}': заявлено {c.stated}, сумма компонентов {c.computed}")
