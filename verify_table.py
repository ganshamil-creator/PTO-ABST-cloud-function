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
дополняет, а не заменяет, сверку с независимым источником.

Версия 2 (пакет 1 по ревью трёх агентов). Главные отличия от первой версии:
  * отчёт имеет три состояния (passed / nothing_to_verify / mismatch), а не
    булево "всё совпало" (раньше при нуле проверок было all_match=True);
  * "Итого" ищется в любой ячейке строки, а не только в первой непустой
    (раньше колонка "№ п/п" ломала поиск);
  * "Итого" (подытог) и "Всего" (общий итог) различаются; общий итог сверяется
    с суммой строк данных (подытоги не считаются повторно);
  * колонки-индексы (№, поз., марка), цены, проценты, колонки-произведения
    ("Сумма, руб") и агрегатные колонки ("Итого по разделу") не сверяются
    как обычные суммы;
  * допуск зависит от величины: для целых штук — точное совпадение, иначе
    0.1% плюс допуск на округление;
  * числа разбираются явно (неразрывные пробелы, юникод-минус, "1.234,5",
    "1,234.5", "1.234.567"), нераспознанные числоподобные ячейки считаются
    и выводятся в отчёт, а не пропадают молча;
  * estimate_volume_from_mass: число строк шапки определяется по содержимому,
    единицы массы (кг / т) берутся из заголовка, "масса ед." и общая
    "масса" различаются, плотность проверяется, пропущенные строки
    перечисляются.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

# Слова общего итога (сверяется с суммой строк данных) и подытога.
_GRAND_WORDS = ("всего", "общий итог", "итого по таблице", "total")
_TOTAL_WORDS = ("итого", "всего", "total", "общий итог")

# Колонки, которые НЕЛЬЗЯ складывать как обычные количества.
_INDEX_HEADER_RE = re.compile(
    r"(№|п/п|\bпоз\b|\bпоз\.|позиц|марка|обозначен|наименован|лист\b|номер)", re.IGNORECASE
)
_RATE_HEADER_RE = re.compile(
    r"(цена|тариф|расценк|коэфф|норма|%|процент|плотност|удельн)", re.IGNORECASE
)
# Колонка-агрегат: подытог внутри таблицы. Её значение — не сумма листовых
# колонок слева, а свой собственный агрегат.
_AGGREGATE_HEADER_RE = re.compile(
    r"(по разделу|подытог|в т\.\s?ч|в том числе|по этапу|по корпусу|по секци)", re.IGNORECASE
)
_COUNT_HEADER_RE = re.compile(r"(шт|кол\.|кол-во|колич)", re.IGNORECASE)

_NUMLIKE_RE = re.compile(r"^[−–—-]?\s*\d[\d\s.,]*$")


def _norm_text(text: str) -> str:
    """Нормализует пробелы (в т.ч. неразрывные и узкие) и юникод-минус."""
    text = str(text)
    for ch in (" ", " ", " ", " ", " ", " "):
        text = text.replace(ch, " ")
    for ch in ("−", "–", "—"):
        text = text.replace(ch, "-")
    return text.strip()


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


def parse_number(text: str) -> tuple[float | None, str | None]:
    """Разбирает число. Возвращает (значение, заметка). Заметка непуста, если
    разбор неоднозначен (напр. "1.234" — это 1,234 или 1234?). Для строки,
    которая не является числом, возвращает (None, None).

    Правила (русские документы):
      * пробелы (в т.ч. неразрывные) — разделители разрядов;
      * одна запятая — десятичная;
      * запятая и точка вместе: последний символ-разделитель — десятичный;
      * несколько одинаковых разделителей ("1.234.567") — разряды;
      * одна точка с ровно тремя цифрами после — неоднозначно: считаем
        десятичной (1.234 = 1,234), но отмечаем заметкой."""
    t = _norm_text(text)
    if not t or not _NUMLIKE_RE.fullmatch(t):
        return None, None
    neg = t.startswith("-")
    t = t.lstrip("-").strip().replace(" ", "")
    if not t or not t[0].isdigit():
        return None, None
    note = None
    commas, dots = t.count(","), t.count(".")
    try:
        if commas and dots:
            if t.rfind(",") > t.rfind("."):          # 1.234,5
                t = t.replace(".", "").replace(",", ".")
            else:                                     # 1,234.5
                t = t.replace(",", "")
        elif commas > 1:                              # 1,234,567
            t = t.replace(",", "")
        elif dots > 1:                                # 1.234.567
            t = t.replace(".", "")
        elif commas == 1:                             # 12,5 или 1,234
            head, tail = t.split(",")
            if len(tail) == 3 and 1 <= len(head) <= 3 and head != "0":
                note = f"«{text.strip()}»: запятая — десятичная (РУ) или разряды (EN)? принято РУ"
            t = t.replace(",", ".")
        elif dots == 1:
            head, tail = t.split(".")
            if len(tail) == 3 and 1 <= len(head) <= 3 and head != "0":
                note = f"«{text.strip()}»: точка — десятичная или разряды? принята десятичная"
        val = float(t)
    except ValueError:
        return None, None
    if not math.isfinite(val):
        return None, None
    return (-val if neg else val), note


def _to_number(text: str) -> float | None:
    return parse_number(text)[0]


@dataclass
class CheckResult:
    kind: str            # "row" | "row_subtotals" | "column"
    index: int            # номер строки/колонки с итогом
    label: str             # текст ячейки-подписи ("Итого", "Всего" и т.п.)
    stated: float
    computed: float
    match: bool
    tolerance: float = 0.0
    column: int | None = None          # для kind="row": по какой колонке сверяли
    cells: list = field(default_factory=list)  # [(row, col), ...] — какие ячейки сложены


@dataclass
class VerifyReport:
    table_rows: int
    table_cols: int
    checks: list[CheckResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)       # неоднозначности, пропуски
    unparsed_cells: list[tuple] = field(default_factory=list)  # (row, col, text) числоподобные, но не разобрались
    skipped_columns: list[dict] = field(default_factory=list)  # {col, header, reason}

    @property
    def status(self) -> str:
        """passed — проверки были и все сошлись; mismatch — есть расхождение;
        nothing_to_verify — сверять было нечего (это НЕ то же, что «сошлось»)."""
        if not self.checks:
            return "nothing_to_verify"
        return "mismatch" if any(not c.match for c in self.checks) else "passed"

    @property
    def all_match(self) -> bool:
        # True только если проверки реально были и все сошлись.
        return self.status == "passed"

    @property
    def mismatches(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.match]


def _grid(table: dict) -> list[list[str]]:
    """Строит сетку текстов по явным row/column number OpenDataLoader.
    Объединённые ячейки (row/column span) заполняют ВСЕ покрываемые клетки,
    а не только левую верхнюю — иначе значение «съезжало» из-за пустых клеток.
    Схема проверяется: если нет ключа rows — это явная ошибка, а не пустая
    сетка."""
    if not isinstance(table, dict) or "rows" not in table:
        raise ValueError("Таблица без ключа 'rows' — схема OpenDataLoader изменилась?")
    rows_meta = table.get("rows") or []
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
            rspan = max(1, cell.get("row span", 1) or 1)
            cspan = max(1, cell.get("column span", 1) or 1)
            txt = _cell_text(cell)
            for rr in range(r, r + rspan):
                for cc in range(c, c + cspan):
                    if 0 <= rr < nrows and 0 <= cc < ncols and not grid[rr][cc]:
                        # span-копии помечаем только если ячейка-владелец
                        # непуста; сама левая верхняя пишется всегда
                        grid[rr][cc] = txt
    return grid


# ---------------------------------------------------------------------------
# Шапка таблицы
# ---------------------------------------------------------------------------

def _is_numbering_row(row: list[str]) -> bool:
    """Строка нумерации колонок ГОСТ-таблиц: 1 | 2 | 3 | 4 ... (подряд)."""
    vals = [_to_number(c) for c in row if c]
    if len(vals) < 3 or any(v is None for v in vals):
        return False
    ints = [int(v) for v in vals if float(v).is_integer()]
    return len(ints) == len(vals) and ints == list(range(ints[0], ints[0] + len(ints))) and ints[0] in (0, 1)


def detect_header_rows(grid: list[list[str]], max_rows: int = 4) -> int:
    """Число строк шапки по содержимому: ведущие строки, в которых числовых
    ячеек меньше трети от непустых (или пустые / строка нумерации колонок).
    Первая строка с преобладанием чисел — уже данные."""
    count = 0
    for row in grid[:max_rows]:
        filled = [c for c in row if c]
        if not filled:
            count += 1
            continue
        if _is_numbering_row(row):
            count += 1
            continue
        numeric = sum(1 for c in filled if _to_number(c) is not None)
        if numeric / len(filled) < 1 / 3:
            count += 1
        else:
            break
    return count


def _merged_header(grid: list[list[str]], header_rows: int) -> list[str]:
    """Склеивает первые header_rows строк в один заголовок на колонку —
    у сложных спецификаций подпись колонки разбита на 2 строки."""
    ncols = max((len(r) for r in grid), default=0)
    merged = ["" for _ in range(ncols)]
    for ri in range(min(header_rows, len(grid))):
        row = grid[ri]
        for ci in range(ncols):
            cell = row[ci] if ci < len(row) else ""
            if cell and cell not in merged[ci]:
                merged[ci] = (merged[ci] + " " + cell).strip()
    return merged


# ---------------------------------------------------------------------------
# Классификация строк/колонок
# ---------------------------------------------------------------------------

def _row_total_kind(row: list[str]) -> str | None:
    """None | "sub" | "grand". Слово-итог ищется в ЛЮБОЙ текстовой ячейке строки
    (не только в первой — там часто «№ п/п»). Ячейка с большим количеством
    слов не считается подписью итога (длинное описание, где случайно
    встретилось слово «всего»)."""
    kind = None
    for c in row:
        if not c or _to_number(c) is not None:
            continue
        low = _norm_text(c).lower()
        if len(low.split()) > 6:
            continue
        if any(w in low for w in _GRAND_WORDS):
            return "grand"
        if any(w in low for w in _TOTAL_WORDS):
            kind = "sub"
    return kind


def _column_role(header: str) -> str:
    """index | rate | aggregate | count | quantity — роль колонки по заголовку."""
    h = _norm_text(header)
    if _AGGREGATE_HEADER_RE.search(h):
        return "aggregate"
    if _INDEX_HEADER_RE.search(h):
        return "index"
    if _RATE_HEADER_RE.search(h):
        return "rate"
    if _COUNT_HEADER_RE.search(h):
        return "count"
    return "quantity"


def _is_product_column(grid, ci, first_data, last_data, excluded_rows) -> bool:
    """Значение колонки ≈ произведению двух других числовых колонок в ≥80%
    строк данных (Кол-во × Цена = Сумма) — это не сумма, сверять как итог
    нельзя."""
    ncols = len(grid[0]) if grid else 0
    hits = total = 0
    for ri in range(first_data, last_data + 1):
        if ri in excluded_rows:
            continue
        row = grid[ri]
        v = _to_number(row[ci] if ci < len(row) else "")
        if v is None or v == 0:
            continue
        others = [(cj, _to_number(row[cj])) for cj in range(ncols) if cj != ci and cj < len(row)]
        others = [(cj, x) for cj, x in others if x is not None and x != 0]
        total += 1
        found = False
        for i in range(len(others)):
            for j in range(i + 1, len(others)):
                p = others[i][1] * others[j][1]
                if abs(p - v) <= max(abs(v) * 0.002, 0.01):
                    found = True
                    break
            if found:
                break
        hits += found
    return total >= 2 and hits / total >= 0.8


def _decimals(vals: list[float]) -> int:
    d = 0
    for v in vals:
        s = repr(float(v))
        if "." in s and not s.endswith(".0"):
            d = max(d, len(s.split(".")[1].rstrip("0")))
    return min(d, 6)


def _tolerance(stated: float, comps: list[float], count_like: bool) -> float:
    vals = [stated] + comps
    if count_like and all(float(v).is_integer() for v in vals):
        return 0.0  # штуки — только точное совпадение
    # 0.1% от итога плюс допуск на округление каждого слагаемого. Для целых
    # чисел поправки на округление нет — иначе 31 «сходилось» бы с 30.
    d = _decimals(vals)
    rounding = len(comps) * 0.5 * 10 ** (-d) if d > 0 else 0.0
    return max(0.001 * abs(stated), rounding, 1e-9)


# ---------------------------------------------------------------------------
# Основная проверка
# ---------------------------------------------------------------------------

def verify_table(table: dict, tolerance_ratio: float | None = None) -> VerifyReport:
    """tolerance_ratio оставлен для совместимости со старой сигнатурой и
    игнорируется: допуск теперь зависит от величины (см. _tolerance)."""
    grid = _grid(table)
    nrows = len(grid)
    ncols = max((len(r) for r in grid), default=0)
    report = VerifyReport(table_rows=nrows, table_cols=ncols)
    if nrows == 0 or ncols == 0:
        return report

    # считаем числоподобные ячейки, которые не разобрались (не молчим о них)
    for ri, row in enumerate(grid):
        for ci, cell in enumerate(row):
            if not cell:
                continue
            val, note = parse_number(cell)
            if note:
                report.notes.append(note)
            if val is None and re.search(r"\d", cell) and _NUMLIKE_RE.fullmatch(_norm_text(cell)):
                report.unparsed_cells.append((ri, ci, cell))

    hdr_rows = detect_header_rows(grid)
    headers = _merged_header(grid, hdr_rows)
    roles = [_column_role(h) for h in headers]
    row_kind = [_row_total_kind(r) for r in grid]
    total_rows = {ri for ri, k in enumerate(row_kind) if k}

    # --- итоговые СТРОКИ ("Итого"/"Всего") ---------------------------------
    for ri in range(hdr_rows, nrows):
        kind = row_kind[ri]
        if not kind:
            continue
        row = grid[ri]
        label = next((c for c in row if c and _to_number(c) is None and any(w in c.lower() for w in _TOTAL_WORDS)), "")
        for ci in range(ncols):
            if roles[ci] in ("index", "rate"):
                continue
            stated = _to_number(row[ci] if ci < len(row) else "")
            if stated is None:
                continue
            # границы блока данных
            if kind == "grand":
                start = hdr_rows
                use_rows = [rj for rj in range(start, ri) if rj not in total_rows]
            else:
                rj = ri - 1
                use_rows = []
                while rj >= hdr_rows and rj not in total_rows:
                    use_rows.append(rj)
                    rj -= 1
                use_rows.reverse()
            comps, cells = [], []
            for rj in use_rows:
                v = _to_number(grid[rj][ci] if ci < len(grid[rj]) else "")
                if v is not None:
                    comps.append(v)
                    cells.append((rj, ci))
            if not comps:
                continue
            computed = sum(comps)
            tol = _tolerance(stated, comps, roles[ci] == "count")
            report.checks.append(CheckResult(
                kind="row", index=ri, label=label or "Итого", stated=stated,
                computed=round(computed, 6), match=abs(stated - computed) <= tol,
                tolerance=tol, column=ci, cells=cells,
            ))
            # общий итог дополнительно сверяем с суммой подытогов (если они есть)
            if kind == "grand":
                sub_vals = [(rj, _to_number(grid[rj][ci] if ci < len(grid[rj]) else ""))
                            for rj in range(hdr_rows, ri) if row_kind[rj] == "sub"]
                sub_vals = [(rj, v) for rj, v in sub_vals if v is not None]
                if sub_vals:
                    comp2 = [v for _, v in sub_vals]
                    s2 = sum(comp2)
                    tol2 = _tolerance(stated, comp2, roles[ci] == "count")
                    report.checks.append(CheckResult(
                        kind="row_subtotals", index=ri, label=label or "Всего", stated=stated,
                        computed=round(s2, 6), match=abs(stated - s2) <= tol2,
                        tolerance=tol2, column=ci, cells=[(rj, ci) for rj, _ in sub_vals],
                    ))

    # --- итоговые КОЛОНКИ --------------------------------------------------
    # заголовок колонки с «Итого»/«Всего» (слово «Сумма» больше не считается
    # итогом: «Сумма, руб» — это обычно произведение Кол-во × Цена)
    col_total_positions = []
    for ci in range(ncols):
        h = headers[ci].lower()
        if roles[ci] == "aggregate":
            report.skipped_columns.append({"col": ci, "header": headers[ci],
                                           "reason": "агрегат внутри таблицы (подытог), не сумма листовых колонок"})
            continue
        if any(w in h for w in _TOTAL_WORDS):
            col_total_positions.append(ci)

    data_first = hdr_rows
    data_last = nrows - 1
    for idx, ci in enumerate(col_total_positions):
        if _is_product_column(grid, ci, data_first, data_last, total_rows):
            report.skipped_columns.append({"col": ci, "header": headers[ci],
                                           "reason": "значение ≈ произведению двух колонок — не сумма"})
            continue
        prev = col_total_positions[idx - 1] if idx > 0 else -1
        leaf_cols = [cj for cj in range(prev + 1, ci) if roles[cj] in ("quantity", "count")]
        for ri in range(data_first, nrows):
            if ri in total_rows:
                continue
            row = grid[ri]
            stated = _to_number(row[ci] if ci < len(row) else "")
            if stated is None:
                continue
            comps = []
            for cj in leaf_cols:
                v = _to_number(row[cj] if cj < len(row) else "")
                if v is not None:
                    comps.append(v)
            if not comps and idx > 0:
                for cj in col_total_positions[:idx]:
                    v = _to_number(row[cj] if cj < len(row) else "")
                    if v is not None:
                        comps.append(v)
            if len(comps) < 2:
                continue  # одно слагаемое — это не сверка суммы
            computed = sum(comps)
            tol = _tolerance(stated, comps, roles[ci] == "count")
            report.checks.append(CheckResult(
                kind="column", index=ci, label=headers[ci], stated=stated,
                computed=round(computed, 6), match=abs(stated - computed) <= tol,
                tolerance=tol, column=ci, cells=[(ri, cj) for cj in leaf_cols],
            ))

    if report.unparsed_cells:
        report.notes.append(f"Не распознано числоподобных ячеек: {len(report.unparsed_cells)} — они не участвовали в сверке")
    return report


# ---------------------------------------------------------------------------
# Резервная оценка объёма по массе элементов
# ---------------------------------------------------------------------------
# Многие спецификации (например, "Спецификация к схеме расположения свай")
# не содержат строки/колонки "Итого"/"Всего" вообще. Если есть колонки
# "Кол-во" и "Масса ед." — суммарную массу можно перевести в объём через
# плотность. Это ОЦЕНКА, а не данные чертежа.

_QTY_HEADER_WORDS = ("кол.", "кол-во", "количество", "шт.")
_UNIT_MASS_HEADER_WORDS = ("масса ед", "масса 1", "масса одн", "вес ед", "масса единицы", "масса изделия")
_TOTAL_MASS_HEADER_WORDS = ("общая масса", "масса общ", "масса всего", "масса, всего", "всего масса")

DEFAULT_DENSITY_KG_M3 = 2500.0  # тяжёлый бетон/ж/б


def _mass_unit_factor(header: str) -> tuple[float | None, str | None]:
    """Множитель к кг по единице из заголовка. (None, None) если единицы нет."""
    h = _norm_text(header).lower()
    if re.search(r"(?<![а-яa-z])(тонн\w*|т)(?![а-яa-z])", h):
        return 1000.0, "т"
    if re.search(r"(?<![а-яa-z])(кг|kg)(?![а-яa-z])", h):
        return 1.0, "кг"
    return None, None


@dataclass
class MassEstimate:
    quantity_col: int
    mass_col: int
    quantity_label: str
    mass_label: str
    total_mass_kg: float
    volume_m3: float
    density_kg_m3: float
    rows_used: int
    rows_skipped: int
    mass_unit: str = "кг"
    header_rows: int = 0
    skipped_rows: list = field(default_factory=list)   # [(индекс строки, причина)]
    warnings: list = field(default_factory=list)
    needs_confirmation: str | None = None   # причина, по которой оценку нельзя давать без подтверждения


def estimate_volume_from_mass(
    grid: list[list[str]],
    density_kg_m3: float = DEFAULT_DENSITY_KG_M3,
    header_rows: int | None = None,
) -> MassEstimate | None:
    """Ищет колонки количества и массы единицы, считает суммарную массу и
    переводит в объём. Возвращает None, если подходящих колонок нет или ни
    одной строки не удалось посчитать. Бросает ValueError при некорректной
    плотности (0, отрицательная, inf, nan) — молчаливой подстановки нет."""
    if not isinstance(density_kg_m3, (int, float)) or not math.isfinite(density_kg_m3) or density_kg_m3 <= 0:
        raise ValueError(f"Некорректная плотность: {density_kg_m3!r} (нужно конечное число > 0)")

    if header_rows is None:
        header_rows = detect_header_rows(grid)
    headers = _merged_header(grid, header_rows)

    qty_col = mass_col = None
    ambiguous_col = None
    for ci, h in enumerate(headers):
        hl = _norm_text(h).lower()
        if qty_col is None and any(w in hl for w in _QTY_HEADER_WORDS):
            qty_col = ci
        if any(w in hl for w in _TOTAL_MASS_HEADER_WORDS):
            continue  # общая масса строки нам не нужна — не умножаем её на количество
        if mass_col is None and any(w in hl for w in _UNIT_MASS_HEADER_WORDS):
            mass_col = ci
        elif ambiguous_col is None and re.search(r"(масса|вес)", hl) and not any(w in hl for w in _UNIT_MASS_HEADER_WORDS):
            ambiguous_col = ci
    if qty_col is None:
        return None

    warnings: list[str] = []
    needs = None
    if mass_col is None:
        if ambiguous_col is None:
            return None
        # просто «Масса, кг»: непонятно, это масса одной штуки или всей строки.
        mass_col = ambiguous_col
        needs = (f"колонка «{headers[ambiguous_col]}» не говорит, масса ли это единицы или всей строки — "
                 "умножение на количество может завысить массу в N раз. Подтвердите.")

    factor, unit = _mass_unit_factor(headers[mass_col])
    if factor is None:
        factor, unit = 1.0, "кг"
        warnings.append(f"В заголовке «{headers[mass_col]}» нет единицы массы — принято кг")

    total_mass = 0.0
    rows_used = 0
    skipped: list[tuple] = []
    for ri in range(header_rows, len(grid)):
        row = grid[ri]
        if not any(row):
            continue
        if _row_total_kind(row):
            skipped.append((ri, "строка итога — не складывается повторно"))
            continue
        qty = _to_number(row[qty_col] if qty_col < len(row) else "")
        mass = _to_number(row[mass_col] if mass_col < len(row) else "")
        if qty is None or mass is None:
            skipped.append((ri, "нет числа в колонке количества или массы"))
            continue
        if qty < 0 or mass < 0:
            skipped.append((ri, "отрицательное значение"))
            continue
        total_mass += qty * mass * factor
        rows_used += 1

    if rows_used == 0:
        return None
    real_skipped = [s for s in skipped if not s[1].startswith("строка итога")]
    if real_skipped:
        warnings.append(f"Пропущено строк: {len(real_skipped)} — их масса не вошла в сумму")

    return MassEstimate(
        quantity_col=qty_col,
        mass_col=mass_col,
        quantity_label=headers[qty_col],
        mass_label=headers[mass_col],
        total_mass_kg=round(total_mass, 2),
        volume_m3=round(total_mass / density_kg_m3, 4),
        density_kg_m3=float(density_kg_m3),
        rows_used=rows_used,
        rows_skipped=len(skipped),
        mass_unit=unit,
        header_rows=header_rows,
        skipped_rows=skipped[:20],
        warnings=warnings,
        needs_confirmation=needs,
    )


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
        print(f"\nТаблица {t.get('id')} — {rep.table_rows}x{rep.table_cols}, статус: {rep.status}, проверок: {len(rep.checks)}")
        for c in rep.checks:
            status = "OK" if c.match else "!! РАСХОЖДЕНИЕ"
            print(f"  [{status}] {c.kind} '{c.label}': заявлено {c.stated}, сумма компонентов {c.computed}")
        for n in rep.notes:
            print("  примечание:", n)
        if not rep.checks:
            grid = _grid(t)
            try:
                est = estimate_volume_from_mass(grid)
            except ValueError as e:
                print("  ", e)
                est = None
            if est:
                print(f"  оценка по массе: {est.total_mass_kg} кг, {est.volume_m3} м3 (строк {est.rows_used}, пропущено {est.rows_skipped})")
                if est.needs_confirmation:
                    print("  ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ:", est.needs_confirmation)
                for w in est.warnings:
                    print("  !", w)
