"""
main.py — Cloud Function backend для «Помогатора»
====================================================
Единственная причина, по которой это отдельная функция, а не часть браузера:
OpenDataLoader написан на Java и требует JVM — в браузере это физически не
запустить (см. обсуждение). Всё остальное (чтение векторного текста) по-прежнему
работает прямо в браузере без этого сервера.

Второй эндпоинт этой же функции — /gemini-proxy — появился позже: браузер бьётся
в Gemini API напрямую своим IP, а Google блокирует запросы по гео-локации этого
IP ("User location is not supported for the API use") для ряда регионов. Через
Cloud Run запрос до Gemini идёт с IP датацентра Google Cloud (обычно в
разрешённом регионе), а не с IP пользователя — так что для тех, кого блокирует
напрямую, это единственный обходной путь без постоянного VPN.

БЕЗОПАСНОСТЬ КЛЮЧА (важное изменение): раньше клиент (браузер) присылал свой
Gemini API-ключ в теле каждого запроса к /gemini-proxy — то есть ключ был виден
в DevTools/Network любому, у кого открыт HTML-файл, даже без учёта того, что
Google его так и не видел напрямую. Теперь ключ живёт ТОЛЬКО на сервере — в
переменной окружения GEMINI_API_KEY этой Cloud Function — и клиент его вообще
не присылает. Поле "apiKey" в теле запроса от браузера больше не требуется и
игнорируется, если всё же придёт (например от старой версии HTML-файла).

Как задать ключ на сервере (Cloud Run / Cloud Functions 2-го поколения):
  Быстрый способ: в консоли сервиса → "Переменные среды и секреты" → добавить
    переменную окружения GEMINI_API_KEY со значением ключа → Deploy.
  Более безопасный способ: положить ключ в Google Secret Manager, затем в том
    же разделе консоли выбрать "Reference a secret" вместо обычной переменной —
    тогда сырое значение ключа не будет храниться в конфигурации сервиса.

Деплой (Google Cloud Functions, 2-е поколение, тот же способ через GitHub,
которым вы уже пользуетесь для остального):
  Точка входа: extract_and_verify_tables
  Runtime: Python 3.11+
  Требуется: Java 17+ в среде выполнения (см. requirements.txt и README ниже);
  requirements.txt должен включать "requests" (для /gemini-proxy) и "ezdxf"
  (для /dwg-extract, новое — см. ниже); Docker-образ должен включать бинарь
  dwg2dxf (LibreDWG) в PATH — см. примечание у /dwg-extract.
  Обязательная переменная окружения: GEMINI_API_KEY (см. выше — без неё
  /gemini-proxy будет отвечать 500 с понятным текстом ошибки, а не тихо падать)

Вызов из браузера (таблицы, как и раньше):
  POST <URL функции>
  Content-Type: multipart/form-data
  Поле файла: "file" (сам PDF)
  Необязательное поле: "section_code" (например "1.2." — для подсказки, какие
  строки таблицы вероятно относятся к запрошенному разделу классификатора)

Ответ (JSON):
  {
    "tables": [
      {
        "id": ..., "rows": N, "cols": M,
        "grid": [["заголовок1", "заголовок2", ...], [...]],
        "verification": {
          "all_match": true/false,
          "checks": [{"kind":"column","label":"Итого","stated":52404.0,"computed":52404.0,"match":true}, ...]
        }
      }, ...
    ],
    "warnings": ["..."]   # например, если конвертация упала на конкретной странице
  }

Вызов из браузера (Gemini-прокси):
  POST <URL функции>/gemini-proxy
  Content-Type: application/json
  Тело: {"model": "gemini-2.5-flash", "body": {...тело запроса к Gemini как есть...}}
  (поле "apiKey" от клиента больше не требуется — см. "БЕЗОПАСНОСТЬ КЛЮЧА" выше)

Вызов из браузера (OpenAI-прокси, новое):
  POST <URL функции>/openai-proxy
  Content-Type: application/json
  Тело: {"body": {...тело запроса к OpenAI Chat Completions как есть, включая
         "model", "messages", "response_format" и т.п...}}
  Требуется переменная окружения OPENAI_API_KEY (тот же принцип, что у
  GEMINI_API_KEY — ключ только на сервере, клиент его никогда не видит).
  Второй, независимый от Gemini источник для Расчёта 4 — на реальных тестах
  на нескольких чертежах GPT в среднем ошибался заметно меньше, чем голая
  Gemini без нашей защитной обвязки, поэтому его стоит запрашивать параллельно
  и показывать как отдельную, независимую сверку, а не смешивать с ответом
  Gemini в одно число.

Вызов из браузера (векторная геометрия размерных цепочек):
  POST <URL функции>/vector-geometry
  Content-Type: multipart/form-data
  Поле файла: "file" (сам PDF)
  Необязательное поле: "pages" (например "1,2" — по умолчанию первые 3 страницы)

  ЗАЧЕМ: на чертежах по ГОСТ 21.501 засечки размерных линий — короткие отрезки
  под 45° (а не стрелки, как в машиностроительных чертежах) — рисуются в PDF как
  самая обычная векторная геометрия с точными координатами. Если чертёж
  экспортирован из CAD (не скан), эти координаты можно измерить напрямую и
  перевести в миллиметры арифметикой — без OCR/vision, без риска перепутать
  цифру или пропустить короткий отрезок в цепочке (см. обсуждение и валидацию
  на реальном чертеже — geometrически найденная сумма разошлась с проверенным
  вручную значением всего на 10мм из 53100). Браузер сам не может пройтись по
  низкоуровневому потоку операций PDF так же удобно, как PyMuPDF на сервере —
  поэтому это отдельный серверный эндпоинт, а не код в самом HTML.

  Ответ (JSON):
  {
    "chains": [
      {"page": 1, "orientation": "row"|"col", "pos_pt": 133.1, "points_pt": [x0, x1, ...], "n_points": 6}
    ],
    "contour_edges": [
      {"page": 1, "orientation": "h"|"v", "pos_pt": 910.4, "x0": 312.4, "x1": 1817.5}
    ],
    "warnings": ["..."]
  }
  points_pt — координаты последовательных засечек вдоль цепочки в pt (1/72 дюйма,
  стандартная единица PDF); orientation "row" — цепочка идёт горизонтально
  (points_pt — это X-координаты, все на одной Y), "col" — вертикально (points_pt
  — Y-координаты, все на одном X). Дальнейшая калибровка масштаба (мм за pt) и
  сопоставление с конкретным полем (length_axis_grid_mm и т.п.) — на стороне
  браузера, там уже есть показания Gemini для калибровки.

  contour_edges — прямые отрезки САМОЙ ТОЛСТОЙ линии на листе (по ГОСТ 2.303 это
  видимый контур объекта, край дна котлована) — кандидаты в стороны прямоугольника
  котлована. "h" — горизонтальный отрезок (x0/x1 — диапазон по X, pos_pt — общий Y),
  "v" — вертикальный (аналогично, pos_pt — общий X). Нужны, чтобы сопоставлять
  цепочку размеров не по близости к (возможно неверному) показанию Gemini, а по
  тому, реально ли она идёт параллельно и рядом с настоящим краем котлована —
  структурная проверка, не зависящая от того, ошиблась ли модель в порядке
  величины.

Вызов из браузера (DWG — точное извлечение размеров, БЕЗ AI, новое):
  POST <URL функции>/dwg-extract
  Content-Type: multipart/form-data
  Поле файла: "file" (сам DWG)

  ЗАЧЕМ: если чертёж есть в DWG (а не только в PDF), размеры на нём — это не
  нарисованные кривыми цифры (как в векторном PDF, экспортированном "в кривые"),
  а настоящие объекты DIMENSION с готовым числовым значением и точными
  геометрическими точками замера. Не нужно ни распознавать текст, ни гадать,
  какой линии он принадлежит и какой оси касается — координаты точек либо
  СОВПАДАЮТ с координатой известной оси (с точностью до долей мм), либо нет,
  это факт, а не оценка модели. Это на порядок надёжнее любого способа читать
  PDF (см. историю отладки длиной в две недели вокруг одного и того же угла
  одного и того же чертежа, которую этот эндпоинт должен закрыть).

  Проверено на реальных файлах проекта (котлован.dwg, сваи.dwg): конвертация
  через LibreDWG (dwg2dxf) отработала полностью, разобрано 42 объекта DIMENSION
  на котловане, включая корректную обработку text_override (когда чертёжник
  вручную вписал число, отличное от геометрической длины — на практике реально
  встретилось: измерено 28285мм, показано "46570").

  ТРЕБУЕТСЯ В ОБРАЗЕ: бинарь dwg2dxf (пакет LibreDWG, github.com/LibreDWG/libredwg,
  свободная лицензия, без Autodesk) должен быть собран и доступен в PATH контейнера
  — см. отдельные инструкции для Dockerfile, которые нужно добавить руками (этот
  main.py их не содержит, т.к. сборка — это shell-команды уровня Docker, а не Python).

  Ответ (JSON):
  {
    "axes": [{"orientation": "horizontal"|"vertical", "coord": 16740.0}, ...],
    "dimensions": [
      {
        "layer": "Размерные цепочки",
        "value_mm": 1300.0,
        "value_source": "geometry"|"text_override",
        "p1": [x, y] | null, "p2": [x, y] | null,
        "kind": "inter_axis"|"axis_to_point"|"unrelated",
        "from_axis": {"orientation": ..., "coord": ...} | null
      }, ...
    ],
    "warnings": ["..."]
  }
  "inter_axis" — обе точки размера лежат на известных осях (обычный межосевой
  пролёт, сумма таких по нужному направлению даёт length_axis_grid_mm/
  width_axis_grid_mm). "axis_to_point" — ровно одна точка на оси, другая нет:
  это и есть искомые length_extra_left/right_mm, width_extra_top/bottom_mm —
  никакого чтения текста, дальше просто сопоставить with уже известным полем по
  стороне контура. "unrelated" — ни одна точка не совпала с известной осью
  (например размер внутри детали, не относящийся к габаритам котлована вообще)
  — такие в текущей версии не используются, но отдаются для полноты картины.

Функция ничего не сохраняет — тело запроса просто пробрасывается в Gemini вместе
с серверным ключом, и ответ возвращается как есть (плюс поле error.proxyError
при сетевой/конфигурационной ошибке самого прокси, отдельно от обычных ошибок
Gemini API).
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
import traceback
from collections import defaultdict

import ezdxf
import functions_framework
import fitz  # PyMuPDF — добавьте "pymupdf" в requirements.txt
import requests
from flask import Request, jsonify

import opendataloader_pdf
from verify_table import verify_table, _grid, estimate_volume_from_mass  # тот же модуль, что и в takeoff_pipeline.py

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# Ключ читается ОДИН РАЗ при старте инстанса из переменной окружения — клиент
# его больше не присылает (см. докстринг модуля). Если переменная не задана,
# /gemini-proxy отвечает понятной ошибкой 500 вместо непонятного 401 от Gemini.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Второй, независимый источник для Расчёта 4 — GPT (см. сравнение нескольких
# моделей на реальных чертежах, которое показало, что ни одна модель поодиночке
# не даёт надёжного результата на этих чертежах, но GPT в среднем ошибался
# заметно меньше Gemini). Тот же принцип безопасности — ключ только на сервере.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_API_BASE = "https://api.openai.com/v1/chat/completions"

# Подсказки для поиска слоя осей в DWG (см. /dwg-extract). У проверенных файлов
# проекта это был слой "S-GRID" (экспорт из Revit, американская номенклатура
# слоёв AIA), но у других отделов/подрядчиков может называться иначе —
# добавляйте новые варианты сюда по мере встречи на практике, а не переписывайте
# логику.
DWG_AXIS_LAYER_HINTS = ["ось", "оси", "осн.разб", "разбивоч", "axis", "grid", "s-grid"]


def _cors_headers():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }


def _find_tables(node, results):
    if not isinstance(node, dict):
        return
    if node.get("type") == "table":
        results.append(node)
    for kid in node.get("kids") or []:
        _find_tables(kid, results)


def _gemini_proxy(request: Request, headers: dict):
    if not GEMINI_API_KEY:
        return (
            jsonify({"error": {"proxyError": "GEMINI_API_KEY не задан на сервере — добавьте переменную окружения в настройках Cloud Run/Cloud Function и передеплойте"}}),
            500,
            headers,
        )

    try:
        payload = request.get_json(force=True, silent=False)
    except Exception:
        return (jsonify({"error": {"proxyError": "Тело запроса не JSON"}}), 400, headers)

    # "apiKey" от клиента больше не требуется и не используется, даже если
    # придёт (например от старой версии HTML-файла, ещё не обновлённой) —
    # сервер всегда использует свой собственный GEMINI_API_KEY.
    if not payload or not payload.get("model") or "body" not in payload:
        return (jsonify({"error": {"proxyError": "Нужны поля model, body"}}), 400, headers)

    url = f"{GEMINI_API_BASE}/{payload['model']}:generateContent"
    try:
        upstream = requests.post(
            url,
            headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
            json=payload["body"],
            # Раньше здесь стояло 120 секунд — этого хватало, пока запросы были
            # проще. Теперь Расчёт 4 использует thinkingLevel="high" и в некоторых
            # случаях несколько изображений в одном запросе (план/разрез/тайлы
            # проверки) — такой запрос на глубокое рассуждение честно может занять
            # больше 120 секунд, особенно на moделях линейки Pro. 300 секунд даёт
            # разумный запас, оставаясь заметно меньше общего таймаута Cloud Run
            # на сам HTTP-запрос (Request timeout в настройках сервиса — 600с).
            timeout=300,
        )
    except requests.RequestException as e:
        return (jsonify({"error": {"proxyError": f"Не достучались до Gemini API: {e}"}}), 502, headers)

    # Отдаём ответ Gemini как есть (и код статуса, и тело) — браузер сам разбирает
    # data.error/data.candidates ровно так же, как при прямом вызове раньше.
    try:
        body = upstream.json()
    except ValueError:
        body = {"error": {"proxyError": f"Gemini вернула не-JSON (код {upstream.status_code}): {upstream.text[:300]}"}}
    return (jsonify(body), upstream.status_code, headers)


def _openai_proxy(request: Request, headers: dict):
    if not OPENAI_API_KEY:
        return (
            jsonify({"error": {"proxyError": "OPENAI_API_KEY не задан на сервере — добавьте переменную окружения в настройках Cloud Run/Cloud Function и передеплойте"}}),
            500,
            headers,
        )

    try:
        payload = request.get_json(force=True, silent=False)
    except Exception:
        return (jsonify({"error": {"proxyError": "Тело запроса не JSON"}}), 400, headers)

    # Тело запроса ("body") приходит от клиента уже в формате OpenAI Chat
    # Completions ({"model":..., "messages":[...], "response_format":{...}}) —
    # сервер просто добавляет свой ключ и пробрасывает как есть, ничего не
    # интерпретирует, как и /gemini-proxy.
    if not payload or "body" not in payload:
        return (jsonify({"error": {"proxyError": "Нужно поле body"}}), 400, headers)

    try:
        upstream = requests.post(
            OPENAI_API_BASE,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {OPENAI_API_KEY}"},
            json=payload["body"],
            timeout=300,
        )
    except requests.RequestException as e:
        return (jsonify({"error": {"proxyError": f"Не достучались до OpenAI API: {e}"}}), 502, headers)

    try:
        body = upstream.json()
    except ValueError:
        body = {"error": {"proxyError": f"OpenAI вернула не-JSON (код {upstream.status_code}): {upstream.text[:300]}"}}
    return (jsonify(body), upstream.status_code, headers)


def _find_contour_edges(page, min_len=20):
    """Толстая линия по ГОСТ 2.303 — видимый контур реального объекта (край дна
    котлована), а не размерная/выносная линия. Берём самую толстую категорию
    обводки, реально встречающуюся на листе (в разных файлах она разная — где-то
    0.84pt, где-то другое значение, поэтому вычисляем, а не зашиваем число), и
    вытаскиваем из неё длинные прямые горизонтальные/вертикальные отрезки —
    кандидаты в стороны прямоугольного контура котлована."""
    rot = page.rotation_matrix  # см. комментарий у _find_ticks — переводим в видимые координаты
    drawings = page.get_drawings()
    strokes = [d for d in drawings if d.get("width")]
    if not strokes:
        return []
    max_width = max(d["width"] for d in strokes)
    thick = [d for d in strokes if d["width"] >= max_width * 0.85]  # тот же "толстый" класс, с запасом на округление
    edges = []
    for d in thick:
        for it in d["items"]:
            if it[0] != "l":
                continue
            p1, p2 = it[1] * rot, it[2] * rot
            dx, dy = p2.x - p1.x, p2.y - p1.y
            length = math.hypot(dx, dy)
            if length < min_len:
                continue
            if abs(dy) < 1.0 and abs(dx) >= min_len:  # горизонтальный отрезок
                edges.append({"orientation": "h", "pos_pt": round((p1.y + p2.y) / 2, 1),
                              "x0": round(min(p1.x, p2.x), 1), "x1": round(max(p1.x, p2.x), 1)})
            elif abs(dx) < 1.0 and abs(dy) >= min_len:  # вертикальный отрезок
                edges.append({"orientation": "v", "pos_pt": round((p1.x + p2.x) / 2, 1),
                              "y0": round(min(p1.y, p2.y), 1), "y1": round(max(p1.y, p2.y), 1)})
    return edges


def _find_ticks(page, min_len=3, max_len=20):
    """Короткие отрезки под ~45° (катеты примерно равны) — это и есть засечки
    ГОСТ 21.501, которыми в строительных чертежах оканчиваются размерные и
    выносные линии (в отличие от стрелок в машиностроительных чертежах).

    ВАЖНО про поворот страницы: get_drawings() отдаёт координаты в СЫРОМ,
    неповёрнутом пространстве содержимого PDF (page.mediabox) — а не в том виде,
    в каком лист реально показывается (page.rect), если у листа выставлен
    /Rotate (см. page.rotation). У нашего эталонного файла это 270° — то есть
    без поправки "горизонтальное" и "вертикальное" в сырых координатах не
    совпадает с тем, что реально видно на отрендеренном листе (в PyMuPDF
    pixmap(), в pdf.js в браузере, и на том, что видит Gemini). Умножаем каждую
    точку на page.rotation_matrix, чтобы работать в тех же координатах, что и
    всё остальное — это и объясняло периодическую путаницу "почему цепочка
    длины определилась как вертикальная" в более ранних попытках отладки вручную."""
    rot = page.rotation_matrix
    drawings = page.get_drawings()
    ticks = []
    for d in drawings:
        if not d.get("width"):
            continue
        for it in d["items"]:
            if it[0] != "l":
                continue
            p1, p2 = it[1] * rot, it[2] * rot
            dx, dy = p2.x - p1.x, p2.y - p1.y
            length = math.hypot(dx, dy)
            if min_len < length < max_len and abs(dx) > 0.5 and abs(dy) > 0.5:
                ratio = abs(dx) / abs(dy)
                if 0.6 < ratio < 1.6:
                    ticks.append(((p1.x + p2.x) / 2, (p1.y + p2.y) / 2))
    return ticks


def _cluster_1d(ticks, key_idx, tol=1.5):
    """Группирует засечки в строки (по Y) или столбцы (по X) — точки, лежащие
    примерно на одной прямой, перпендикулярной направлению цепочки."""
    other_idx = 1 - key_idx
    sorted_t = sorted(ticks, key=lambda t: t[key_idx])
    groups = []
    cur = [sorted_t[0]]
    for t in sorted_t[1:]:
        if t[key_idx] - cur[-1][key_idx] < tol:
            cur.append(t)
        else:
            groups.append(cur)
            cur = [t]
    groups.append(cur)
    result = []
    for g in groups:
        points = sorted(set(round(t[other_idx], 1) for t in g))
        # Раньше здесь было ">= 4" — двухточечные цепочки (только начало и конец)
        # отбрасывались как "случайность". Но именно так выглядит габаритная
        # размерная линия на всю цепочку целиком (одно число вместо разбивки по
        # пролётам) — такая линия почти всегда есть НАД/ПОД цепочкой отдельных
        # пролётов на чертежах по ГОСТ, и читать её напрямую дешевле и надёжнее,
        # чем читать и складывать все отдельные пролёты. Фильтрация шума теперь
        # только на стороне клиента (там уже знают, что ищут — сетку или габарит).
        if len(points) >= 2:
            pos = sum(t[key_idx] for t in g) / len(g)
            result.append((pos, points))
    return result


def _find_dimension_chains(page):
    """Возвращает построчные и постолбцовые цепочки засечек — кандидаты на
    размерные цепочки (сетка осей, добавки и т.п.). Калибровка масштаба (pt->мм)
    и сопоставление с конкретными полями делается на клиенте, где уже есть
    показания Gemini для калибровки — сервер отдаёт только сырую геометрию."""
    ticks = _find_ticks(page)
    if len(ticks) < 8:
        return []
    rows = _cluster_1d(ticks, key_idx=1)  # группируем по Y -> горизонтальные цепочки
    cols = _cluster_1d(ticks, key_idx=0)  # группируем по X -> вертикальные цепочки
    chains = []
    for y, xs in rows:
        chains.append({"orientation": "row", "pos_pt": round(y, 1), "points_pt": xs, "n_points": len(xs)})
    for x, ys in cols:
        chains.append({"orientation": "col", "pos_pt": round(x, 1), "points_pt": ys, "n_points": len(ys)})
    return chains


def _parse_pages_param(pages_str, page_count, default_max=3):
    if not pages_str:
        return list(range(min(page_count, default_max)))
    result = []
    for part in pages_str.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-")
            result.extend(range(int(a) - 1, int(b)))
        else:
            result.append(int(part) - 1)
    return [p for p in result if 0 <= p < page_count]


def _vector_geometry(request: Request, headers: dict):
    if "file" not in request.files:
        return (jsonify({"error": "Поле 'file' (PDF) не найдено в запросе"}), 400, headers)

    upload = request.files["file"]
    pages_param = request.form.get("pages", "")
    warnings: list[str] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = os.path.join(tmpdir, "input.pdf")
        upload.save(pdf_path)
        try:
            doc = fitz.open(pdf_path)
        except Exception as e:
            return (jsonify({"error": f"Не удалось открыть PDF (PyMuPDF): {e}"}), 400, headers)

        page_indices = _parse_pages_param(pages_param, len(doc))
        all_chains = []
        all_edges = []
        for pidx in page_indices:
            try:
                page = doc[pidx]
                chains = _find_dimension_chains(page)
                for c in chains:
                    c["page"] = pidx + 1
                all_chains.extend(chains)
                edges = _find_contour_edges(page)
                for e in edges:
                    e["page"] = pidx + 1
                all_edges.extend(edges)
            except Exception as e:
                warnings.append(f"Лист {pidx + 1}: ошибка разбора векторной геометрии — {e}")

    return (jsonify({"chains": all_chains, "contour_edges": all_edges, "warnings": warnings}), 200, headers)


def _dwg_to_dxf(dwg_path: str, out_dir: str) -> str:
    """Конвертирует DWG -> DXF через LibreDWG (бинарь dwg2dxf, должен быть
    собран в образ — см. примечание в докстринге модуля у /dwg-extract).
    Бросает RuntimeError с хвостом stderr, если dwg2dxf не смог прочитать файл
    (повреждённый DWG, либо версия формата, которую LibreDWG пока не разбирает
    полностью — на практике редко: LibreDWG уверенно читает AutoCAD 2000-2018).
    """
    dxf_path = os.path.join(out_dir, "out.dxf")
    result = subprocess.run(
        ["dwg2dxf", "-y", dwg_path, "-o", dxf_path],
        capture_output=True, text=True, timeout=120,
    )
    if not os.path.exists(dxf_path):
        raise RuntimeError((result.stderr or "без вывода в stderr")[-2000:])
    return dxf_path


def _dwg_extract_axes(msp) -> list[dict]:
    """Оси — длинные прямые линии на слое, название которого совпадает с одной
    из DWG_AXIS_LAYER_HINTS (см. константу выше). У проверенных файлов проекта
    это был слой "S-GRID"."""
    axes = []
    for e in msp.query("LINE"):
        layer_lower = (e.dxf.layer or "").lower()
        if not any(h in layer_lower for h in DWG_AXIS_LAYER_HINTS):
            continue
        start, end = e.dxf.start, e.dxf.end
        horiz = abs(start.y - end.y) < abs(start.x - end.x)
        axes.append({
            "orientation": "horizontal" if horiz else "vertical",
            "coord": round(start.y if horiz else start.x, 1),
        })
    return axes


def _dwg_extract_dimensions(msp) -> list[dict]:
    """DIMENSION — настоящий объект с точками замера и значением, не текст,
    который нужно распознавать. text_override (см. докстринг модуля) в
    приоритете над геометрическим measurement, когда он есть и явно задан
    (не пустая строка и не служебный плейсхолдер "<>")."""
    dims = []
    for e in msp.query("DIMENSION"):
        try:
            measurement = e.get_measurement()
        except Exception:
            measurement = None

        dxf = e.dxf
        p1 = getattr(dxf, "defpoint2", None) or getattr(dxf, "defpoint", None)
        p2 = getattr(dxf, "defpoint3", None)

        text_override = getattr(dxf, "text", "") or ""
        if text_override and text_override != "<>":
            try:
                value_mm = float(text_override.replace(",", "."))
                value_source = "text_override"
            except ValueError:
                value_mm = measurement
                value_source = "geometry"
        else:
            value_mm = measurement
            value_source = "geometry"

        if value_mm is None:
            continue  # размер без geometрии и без читаемого override — пропускаем, а не гадаем

        dims.append({
            "layer": dxf.layer,
            "value_mm": round(value_mm, 1),
            "value_source": value_source,
            "p1": [round(p1.x, 1), round(p1.y, 1)] if p1 else None,
            "p2": [round(p2.x, 1), round(p2.y, 1)] if p2 else None,
        })
    return dims


def _dwg_classify_dimensions(dims: list[dict], axes: list[dict], tol_mm: float = 5.0) -> list[dict]:
    """Главный приём (проверен на реальном чертеже — см. докстринг модуля):
    размер с РОВНО ОДНОЙ точкой на известной оси — это искомая добавка
    (length_extra_*/width_extra_*), не нужно ни искать контур котлована, ни
    гадать через vision, какой линии касается число. Обе точки на осях —
    обычный межосевой пролёт. Ни одной — не относится к сетке осей вообще."""
    def axis_at(pt):
        if not pt:
            return None
        for a in axes:
            coord = pt[1] if a["orientation"] == "horizontal" else pt[0]
            if abs(coord - a["coord"]) <= tol_mm:
                return a
        return None

    for d in dims:
        a1, a2 = axis_at(d["p1"]), axis_at(d["p2"])
        if a1 and a2:
            d["kind"], d["from_axis"] = "inter_axis", None
        elif a1 or a2:
            d["kind"], d["from_axis"] = "axis_to_point", (a1 or a2)
        else:
            d["kind"], d["from_axis"] = "unrelated", None
    return dims


def _dwg_extract(request: Request, headers: dict):
    if "file" not in request.files:
        return (jsonify({"error": "Поле 'file' (DWG) не найдено в запросе"}), 400, headers)

    upload = request.files["file"]

    with tempfile.TemporaryDirectory() as tmpdir:
        dwg_path = os.path.join(tmpdir, "input.dwg")
        upload.save(dwg_path)

        try:
            dxf_path = _dwg_to_dxf(dwg_path, tmpdir)
        except FileNotFoundError:
            # dwg2dxf не найден в PATH — образ ещё не пересобран с LibreDWG.
            return (jsonify({"error": "dwg2dxf не установлен в образе — см. инструкцию по добавлению LibreDWG в Dockerfile"}), 500, headers)
        except (RuntimeError, subprocess.SubprocessError, subprocess.TimeoutExpired) as e:
            return (jsonify({"error": f"Не удалось сконвертировать DWG в DXF: {e}"}), 400, headers)

        try:
            doc = ezdxf.readfile(dxf_path)
        except Exception as e:
            return (jsonify({"error": f"ezdxf не смог прочитать сконвертированный DXF: {e}"}), 500, headers)

        msp = doc.modelspace()
        axes = _dwg_extract_axes(msp)
        dims = _dwg_extract_dimensions(msp)
        dims = _dwg_classify_dimensions(dims, axes)

    warnings: list[str] = []
    if not axes:
        warnings.append(
            "Оси не найдены по известным названиям слоёв (DWG_AXIS_LAYER_HINTS) — "
            "возможно, на этом чертеже слой осей называется иначе; добавьте его "
            "название в DWG_AXIS_LAYER_HINTS в main.py."
        )

    return (jsonify({"axes": axes, "dimensions": dims, "warnings": warnings}), 200, headers)


@functions_framework.http
def extract_and_verify_tables(request: Request):
    # CORS preflight
    if request.method == "OPTIONS":
        return ("", 204, _cors_headers())

    headers = _cors_headers()

    if request.path.rstrip("/").endswith("/gemini-proxy"):
        return _gemini_proxy(request, headers)

    if request.path.rstrip("/").endswith("/openai-proxy"):
        return _openai_proxy(request, headers)

    if request.path.rstrip("/").endswith("/vector-geometry"):
        return _vector_geometry(request, headers)

    if request.path.rstrip("/").endswith("/dwg-extract"):
        return _dwg_extract(request, headers)

    if "file" not in request.files:
        return (jsonify({"error": "Поле 'file' (PDF) не найдено в запросе"}), 400, headers)

    upload = request.files["file"]
    section_code = request.form.get("section_code", "")
    pages = request.form.get("pages", "")  # опционально: "1,3,5-7"
    # Плотность материала для резервной оценки объёма по массе (кг/м3).
    # По умолчанию — тяжёлый бетон/ж/б (2500). Для металлопроката, например,
    # нужно передать 7850.
    try:
        density_kg_m3 = float(request.form.get("density_kg_m3", "2500") or 2500)
    except ValueError:
        density_kg_m3 = 2500.0

    warnings: list[str] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = os.path.join(tmpdir, "input.pdf")
        upload.save(pdf_path)

        try:
            kwargs = dict(input_path=pdf_path, output_dir=tmpdir, format="json", quiet=True, table_method="cluster")
            if pages:
                kwargs["pages"] = pages
            opendataloader_pdf.convert(**kwargs)
        except Exception as e:
            return (jsonify({"error": f"Ошибка OpenDataLoader: {e}", "trace": traceback.format_exc()}), 500, headers)

        json_path = os.path.join(tmpdir, "input.json")
        if not os.path.exists(json_path):
            return (jsonify({"error": "OpenDataLoader не создал JSON — проверьте, что файл действительно PDF"}), 500, headers)

        with open(json_path, encoding="utf-8") as f:
            odl_data = json.load(f)

    tables_raw: list[dict] = []
    _find_tables(odl_data, tables_raw)

    out_tables = []
    for t in tables_raw:
        grid = _grid(t)
        # пропускаем совсем маленький мусор (2x2 "таблицы" — обычно это
        # ложные срабатывания на подписях чертежа, не настоящие таблицы)
        if t.get("number of rows", 0) < 3 or t.get("number of columns", 0) < 2:
            continue
        report = verify_table(t)

        # Если в таблице нет строки/колонки "Итого"/"Всего" — сверить нечего,
        # но объём часто всё равно можно оценить через Кол-во x Масса ед.
        # (типичный случай — спецификации свай/подушек/приямков без итоговой
        # строки). Явно помечаем это как расчётную оценку, а не факт с чертежа.
        mass_estimate = None
        if not report.checks:
            est = estimate_volume_from_mass(grid, density_kg_m3=density_kg_m3)
            if est is not None:
                mass_estimate = {
                    "note": "Итоговой строки в таблице нет — объём получен расчётом "
                            "(Кол-во x Масса ед.) / плотность, а не взят с чертежа. "
                            "Перепроверьте перед использованием.",
                    "quantity_column": est.quantity_label,
                    "mass_column": est.mass_label,
                    "total_mass_kg": est.total_mass_kg,
                    "density_kg_m3": est.density_kg_m3,
                    "volume_m3": est.volume_m3,
                    "rows_used": est.rows_used,
                    "rows_skipped": est.rows_skipped,
                }

        out_tables.append({
            "id": t.get("id"),
            "page": t.get("page number"),
            "rows": t.get("number of rows"),
            "cols": t.get("number of columns"),
            "grid": grid,
            "verification": {
                "all_match": report.all_match,
                "checks": [
                    {
                        "kind": c.kind, "label": c.label,
                        "stated": c.stated, "computed": c.computed,
                        "match": c.match,
                    }
                    for c in report.checks
                ],
            },
            "mass_estimate": mass_estimate,
        })

    return (jsonify({"tables": out_tables, "warnings": warnings, "section_code": section_code}), 200, headers)
