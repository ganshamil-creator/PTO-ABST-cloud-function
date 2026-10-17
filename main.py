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
  встретилось: измерено 28285мм, показано "46570"). Оси дополнительно размечены
  по подписи на самом чертеже как "numbered" (цифровые 1,2,3...) или "lettered"
  (буквенные А,Б,В...) — по ГОСТ это и есть надёжный признак, какое направление
  длина, а какое ширина, вместо догадки по сырой ориентации в координатах DWG.

  ТРЕБУЕТСЯ В ОБРАЗЕ: бинарь dwg2dxf (пакет LibreDWG, github.com/LibreDWG/libredwg,
  свободная лицензия, без Autodesk) должен быть собран и доступен в PATH контейнера
  — см. отдельные инструкции для Dockerfile, которые нужно добавить руками (этот
  main.py их не содержит, т.к. сборка — это shell-команды уровня Docker, а не Python).

  Ответ (JSON):
  {
    "axes": [{"orientation": "horizontal"|"vertical", "coord": 16740.0, "label": "Д"|null, "axis_type": "numbered"|"lettered"|null}, ...],
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
import logging
import math
import os
import re
import subprocess
import tempfile
import traceback
from collections import defaultdict

import ezdxf
import functions_framework
import fitz  # PyMuPDF — добавьте "pymupdf" в requirements.txt
import requests
from flask import Request, jsonify

logger = logging.getLogger("pto-cloud")

# Лимиты на вход (пакет 1 по ревью). Подберите под ваш Cloud Run (по умолчанию
# 512 МиБ памяти; временные файлы тоже лежат в памяти).
MAX_UPLOAD_BYTES = 60 * 1024 * 1024   # 60 МБ на файл
MAX_PAGES_PER_REQUEST = 40            # листов PDF за один запрос

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
    # Линии с нулевой/отсутствующей толщиной (частый случай в CAD-экспортах)
    # не отбрасываем целиком: считаем их толщиной 0.
    strokes = [d for d in drawings if d.get("items")]
    if not strokes:
        return []
    for d in strokes:
        d["_w"] = d.get("width") or 0.0
    max_width = max(d["_w"] for d in strokes)
    thick = [d for d in strokes if d["_w"] >= max_width * 0.85]  # тот же "толстый" класс, с запасом на округление
    edges = []
    for d in thick:
        segs = []
        for it in d["items"]:
            if it[0] == "l":
                segs.append((it[1] * rot, it[2] * rot))
            elif it[0] == "re":
                # прямоугольник, записанный одной операцией, — 4 стороны
                r = it[1]
                c = [fitz.Point(r.x0, r.y0) * rot, fitz.Point(r.x1, r.y0) * rot,
                     fitz.Point(r.x1, r.y1) * rot, fitz.Point(r.x0, r.y1) * rot]
                segs.extend((c[i], c[(i + 1) % 4]) for i in range(4))
        for p1, p2 in segs:
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
    if len(edges) > 400:  # защита от лавины рёбер, если толщины у линий нет вовсе
        edges.sort(key=lambda e: -((e.get("x1", 0) - e.get("x0", 0)) + (e.get("y1", 0) - e.get("y0", 0))))
        edges = edges[:400]
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
    if len(ticks) < 2:  # меньше двух засечек цепочки не бывает; шум отсекает клиент (n_points)
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
    """Разбирает "1,3,5-7" без разворачивания больших диапазонов в список:
    границы обрезаются по page_count ДО range(). Бросает ValueError с понятным
    текстом на мусорный ввод. Не больше MAX_PAGES_PER_REQUEST листов."""
    if not pages_str:
        return list(range(min(page_count, default_max)))
    result: list[int] = []
    seen = set()
    for part in str(pages_str).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                a_s, b_s = part.split("-", 1)
                a, b = int(a_s), int(b_s)
            else:
                a = b = int(part)
        except ValueError:
            raise ValueError(f"Некорректный номер/диапазон страниц: {part!r}")
        if a < 1 or b < 1 or b < a:
            raise ValueError(f"Некорректный диапазон страниц: {part!r}")
        a, b = a, min(b, page_count)           # обрезаем ДО range()
        for p in range(a - 1, b):
            if p not in seen:
                seen.add(p)
                result.append(p)
            if len(result) > MAX_PAGES_PER_REQUEST:
                raise ValueError(f"Слишком много страниц за один запрос (максимум {MAX_PAGES_PER_REQUEST})")
    return result


def _check_upload_size(upload):
    """None, если размер допустим; иначе текст ошибки."""
    try:
        upload.stream.seek(0, os.SEEK_END)
        size = upload.stream.tell()
        upload.stream.seek(0)
    except Exception:
        return None
    if size > MAX_UPLOAD_BYTES:
        return f"Файл слишком большой: {size // (1024 * 1024)} МБ (максимум {MAX_UPLOAD_BYTES // (1024 * 1024)} МБ)"
    return None


def _vector_geometry(request: Request, headers: dict):
    if "file" not in request.files:
        return (jsonify({"error": "Поле 'file' (PDF) не найдено в запросе"}), 400, headers)

    upload = request.files["file"]
    size_err = _check_upload_size(upload)
    if size_err:
        return (jsonify({"error": size_err}), 413, headers)
    pages_param = request.form.get("pages", "")
    warnings: list[str] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = os.path.join(tmpdir, "input.pdf")
        upload.save(pdf_path)
        try:
            doc = fitz.open(pdf_path)
        except Exception as e:
            return (jsonify({"error": f"Не удалось открыть PDF (PyMuPDF): {e}"}), 400, headers)

        try:
            page_indices = _parse_pages_param(pages_param, len(doc))
        except ValueError as e:
            return (jsonify({"error": str(e)}), 400, headers)
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

    if page_indices and not all_chains:
        warnings.append("Векторные размерные цепочки не найдены — вероятно, скан или чертёж без векторных засечек; опирайтесь на распознавание и ручной ввод.")
    return (jsonify({"chains": all_chains, "contour_edges": all_edges, "warnings": warnings}), 200, headers)


def _dwg_to_dxf(dwg_path: str, out_dir: str):
    """Конвертирует DWG -> DXF через LibreDWG (бинарь dwg2dxf, должен быть
    собран в образ — см. примечание в докстринге модуля у /dwg-extract).
    Возвращает (путь_к_dxf, предупреждения). Код возврата проверяется:
    >= 128 — критическая ошибка (RuntimeError), 1..127 — файл создан, но
    LibreDWG сообщил о проблемах (часть объектов могла потеряться) —
    возвращаем это в предупреждениях, а не объявляем результат полным."""
    dxf_path = os.path.join(out_dir, "out.dxf")
    result = subprocess.run(
        ["dwg2dxf", "-y", dwg_path, "-o", dxf_path],
        capture_output=True, text=True, timeout=120,
    )
    stderr_tail = (result.stderr or "")[-2000:]
    if result.returncode >= 128 or not os.path.exists(dxf_path):
        raise RuntimeError(f"dwg2dxf код {result.returncode}: {stderr_tail or 'без вывода в stderr'}")
    warns = []
    if result.returncode != 0:
        logger.warning("dwg2dxf вернул код %s: %s", result.returncode, stderr_tail)
        warns.append(
            f"Конвертер DWG→DXF завершился с кодом {result.returncode}: часть объектов могла не "
            "прочитаться. Результат может быть неполным — перепроверьте размеры/сваи вручную."
        )
    return dxf_path, warns


# $INSUNITS (AutoCAD) -> миллиметров в одной единице чертежа
_INSUNITS_TO_MM = {
    1: 25.4, 2: 304.8, 3: 1609344.0, 4: 1.0, 5: 10.0, 6: 1000.0, 7: 1_000_000.0,
    8: 0.0000254, 9: 0.0254, 10: 914.4, 14: 100.0, 15: 10_000.0, 16: 100_000.0,
}


def _dwg_units_scale(doc) -> tuple[float, dict]:
    """(мм на единицу чертежа, описание). INSUNITS=0 (не задано) трактуем как
    мм, как раньше, но явно отмечаем это допущение в ответе."""
    try:
        code = int(doc.header.get("$INSUNITS", 0) or 0)
    except Exception:
        code = 0
    if code in _INSUNITS_TO_MM:
        return _INSUNITS_TO_MM[code], {"insunits": code, "scale_to_mm": _INSUNITS_TO_MM[code], "assumed": False}
    return 1.0, {"insunits": code, "scale_to_mm": 1.0, "assumed": True}


def _dwg_axis_label_kind(label: str) -> str | None:
    """По ГОСТ цифровые оси (1, 2, 3...) идут в одном направлении (обычно
    длина здания), буквенные (А, Б, В...) — в перпендикулярном (обычно
    ширина). Подтверждено на реальном файле проекта: 1-18 вертикальные в
    координатах DWG, А-Ж горизонтальные. Само по себе это не говорит, что
    "цифровые = length_axis_grid_mm" универсально для абсолютно любого
    чертежа (возможна и обратная ориентация листа), но даёт однозначный,
    проверяемый признак — гораздо надёжнее, чем полагаться на то, какая
    ориентация в сырых DWG-координатах горизонтальная, а какая вертикальная."""
    clean = label.strip().rstrip("*").strip()
    if not clean:
        return None
    if clean.isdigit():
        return "numbered"
    if len(clean) <= 2 and clean.isalpha():
        return "lettered"
    return None


def _dwg_extract_axes(msp, k: float = 1.0, notes: list | None = None) -> list[dict]:
    """Оси — длинные прямые линии на слое, название которого совпадает с одной
    из DWG_AXIS_LAYER_HINTS (см. константу выше). У проверенных файлов проекта
    это был слой "S-GRID". Дополнительно ищем подписи осей (MTEXT на том же
    или соседнем "-IDEN" слое, например "S-GRID-IDEN") и сопоставляем их с
    линией оси по совпадению координаты — это даёт "numbered"/"lettered" для
    каждой оси, чтобы клиент мог надёжно понять, какое направление — длина,
    а какое — ширина, не полагаясь на сырую ориентацию в координатах DWG."""
    axes = []
    skipped_diag = 0
    for e in msp.query("LINE"):
        layer_lower = (e.dxf.layer or "").lower()
        if not any(h in layer_lower for h in DWG_AXIS_LAYER_HINTS):
            continue
        start, end = e.dxf.start, e.dxf.end
        dx, dy = abs(end.x - start.x), abs(end.y - start.y)
        if min(dx, dy) > 0.02 * max(dx, dy, 1e-9):
            # линия не параллельна осям координат (диагональ / повёрнутая
            # сетка) — не записываем её как горизонтальную/вертикальную
            skipped_diag += 1
            continue
        horiz = dy < dx
        axes.append({
            "orientation": "horizontal" if horiz else "vertical",
            "coord": round((start.y if horiz else start.x) * k, 1),
            "label": None,
            "axis_type": None,
        })

    if skipped_diag and notes is not None:
        notes.append(f"Пропущено линий слоя осей, не параллельных осям координат: {skipped_diag} "
                     "(повёрнутая сетка не поддерживается — проверьте результат).")

    # Подписи осей — обычно MTEXT (реже TEXT), очищаем инлайн-форматирование
    # через plain_text(), чтобы из '\fISOCPEUR|...;1' получить просто '1'.
    labels = []
    for e in msp.query("MTEXT"):
        layer_lower = (e.dxf.layer or "").lower()
        if not any(h in layer_lower for h in DWG_AXIS_LAYER_HINTS):
            continue
        text = e.plain_text().strip()
        if text:
            labels.append((text, e.dxf.insert.x * k, e.dxf.insert.y * k))
    for e in msp.query("TEXT"):
        layer_lower = (e.dxf.layer or "").lower()
        if not any(h in layer_lower for h in DWG_AXIS_LAYER_HINTS):
            continue
        text = (e.dxf.text or "").strip()
        if text:
            labels.append((text, e.dxf.insert.x * k, e.dxf.insert.y * k))

    # Подпись стоит не точно на линии оси, а со смещением (кружок с буквой/
    # цифрой вынесен за пределы листа) — на практике смещение варьируется от
    # ~90 до ~165мм в зависимости от чертежа. Вместо жёсткого допуска берём
    # БЛИЖАЙШУЮ ось по перпендикулярной координате — это устойчиво, пока
    # подпись ближе к своей оси, чем к соседней (то есть смещение меньше
    # половины минимального шага сетки — на практике так и есть).
    for text, lx, ly in labels:
        kind = _dwg_axis_label_kind(text)
        if kind is None:
            continue
        best, best_dist = None, None
        for a in axes:
            coord = ly if a["orientation"] == "horizontal" else lx
            dist = abs(coord - a["coord"])
            if best_dist is None or dist < best_dist:
                best, best_dist = a, dist
        if best is not None and best["label"] is None:
            best["label"] = text.rstrip("*").strip()
            best["axis_type"] = kind

    # Проверка на согласованность (реально понадобилась на практике): если
    # для целой ориентации подавляющее большинство осей получили один и тот
    # же axis_type ("numbered" или "lettered"), а конкретная ось — другой,
    # это почти наверняка не её собственная подпись, а случайно ближайшая
    # подпись от постороннего элемента листа (например узла/детали в другом
    # месте чертежа). Сбрасываем такие — лучше честный null, чем неверный тип.
    for orientation in ("horizontal", "vertical"):
        group = [a for a in axes if a["orientation"] == orientation and a["axis_type"]]
        if len(group) < 3:
            continue
        counts = {"numbered": 0, "lettered": 0}
        for a in group:
            counts[a["axis_type"]] += 1
        dominant = "numbered" if counts["numbered"] >= counts["lettered"] else "lettered"
        for a in group:
            if a["axis_type"] != dominant:
                a["label"], a["axis_type"] = None, None

    return axes


def _dwg_extract_dimensions(msp, k: float = 1.0, notes: list | None = None) -> list[dict]:
    """DIMENSION — настоящий объект с точками замера и значением, не текст,
    который нужно распознавать. Геометрическое значение ВСЕГДА сохраняется
    (geometry_mm); text_override (когда он явно задан и не "<>") имеет
    приоритет в value_mm, как и раньше, но расхождение с геометрией больше
    1% помечается conflict=True, чтобы инженер видел его, а не получал
    молча выбранное число. Только линейные размеры (linear/aligned/rotated):
    угловые, радиальные и диаметральные не имеют смысла как «мм».
    k — мм на единицу чертежа ($INSUNITS)."""
    dims = []
    skipped_types = 0
    for e in msp.query("DIMENSION"):
        dxf = e.dxf
        try:
            dimtype = int(getattr(dxf, "dimtype", 0)) & 0x0F
        except Exception:
            dimtype = 0
        if dimtype not in (0, 1):  # 0 = linear/rotated, 1 = aligned
            skipped_types += 1
            continue

        try:
            measurement = e.get_measurement()
        except Exception:
            measurement = None
        if measurement is not None:
            try:
                measurement = float(measurement) * k
            except (TypeError, ValueError):
                measurement = None

        def _pt(name):
            v = getattr(dxf, name, None)
            return v  # Vec3 с нулевыми координатами — валидная точка; проверяем только на None

        p1 = _pt("defpoint2")
        if p1 is None:
            p1 = _pt("defpoint")
        p2 = _pt("defpoint3")

        text_override = (getattr(dxf, "text", "") or "").strip()
        value_mm, value_source = measurement, "geometry"
        override_mm = None
        if text_override and text_override != "<>":
            try:
                override_mm = float(text_override.replace(",", ".").rstrip("*").strip())
                value_mm, value_source = override_mm, "text_override"
            except ValueError:
                pass  # нечитаемый override ("46570*", "~46570") — остаётся геометрия

        if value_mm is None:
            continue  # размер без геометрии и без читаемого override — пропускаем, а не гадаем

        conflict = False
        if override_mm is not None and measurement:
            conflict = abs(override_mm - measurement) > 0.01 * max(abs(measurement), 1.0)

        dims.append({
            "layer": dxf.layer,
            "value_mm": round(value_mm, 1),
            "value_source": value_source,
            "geometry_mm": round(measurement, 1) if measurement is not None else None,
            "conflict": conflict,
            "p1": [round(p1.x * k, 1), round(p1.y * k, 1)] if p1 is not None else None,
            "p2": [round(p2.x * k, 1), round(p2.y * k, 1)] if p2 is not None else None,
        })
    if notes is not None:
        if skipped_types:
            notes.append(f"Пропущено нелинейных размеров (угловые/радиальные/диаметральные и т.п.): {skipped_types}.")
        nconf = sum(1 for d in dims if d["conflict"])
        if nconf:
            notes.append(f"Размеров, где надпись чертёжника расходится с геометрией больше чем на 1%: {nconf} "
                         "(см. поля conflict / geometry_mm). Возможна ошибка в чертеже.")
    return dims


def _dwg_classify_dimensions(dims: list[dict], axes: list[dict], tol_mm: float = 5.0) -> list[dict]:
    """Главный приём (проверен на реальном чертеже — см. докстринг модуля):
    размер с РОВНО ОДНОЙ точкой на известной оси — это искомая добавка
    (length_extra_*/width_extra_*), не нужно ни искать контур котлована, ни
    гадать через vision, какой линии касается число. Обе точки на РАЗНЫХ осях —
    обычный межосевой пролёт. Обе точки на одной и той же оси — не пролёт
    (unrelated). Ни одной — не относится к сетке осей вообще."""
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
        if a1 and a2 and a1 is a2:
            d["kind"], d["from_axis"] = "unrelated", None
        elif a1 and a2:
            d["kind"], d["from_axis"] = "inter_axis", None
        elif a1 or a2:
            d["kind"], d["from_axis"] = "axis_to_point", (a1 or a2)
        else:
            d["kind"], d["from_axis"] = "unrelated", None
    return dims


_ELEV_RE = re.compile(r"^-?\d{1,3}[.,]\d{2,3}$")
_SLOPE_RE = re.compile(r"^\d+\s*[:xх]\s*[\d.,]+$", re.IGNORECASE)
_STATED_VOLUME_RE = re.compile(
    r"объ[её]м[а-я\s]*грунта[а-я\s:\-–—=]*?(\d[\d\s]*[.,]?\d*)\s*(м\s*³|м\s*3|м\s*\^?\s*3|куб\.?\s*м|м\s*²|м\s*2)",
    re.IGNORECASE,
)


def _dwg_extract_depth_slope(msp) -> dict:
    """Ищет в тексте чертежа то же, что на PDF брали из разреза 1-1: отметку
    глубины дна и уклон откоса (1:N) — плюс, если повезёт, явно написанный в
    примечаниях объём выемки, который можно использовать как перекрёстную
    проверку (см. докстринг модуля — на реальном файле проекта расчёт по
    найденным Д/Ш/глубине/уклону разошёлся с явно написанным объёмом всего на
    3.3%, это сильное подтверждение, что находки относятся именно к этому
    котловану, а не к чему-то постороннему на том же листе).

    НЕ пытаемся угадать, какая именно отметка — "та самая" глубина, если их
    несколько — возвращаем все найденные как кандидаты с координатами, разбор
    (одна ли это отметка, согласуются ли между собой несколько уклонов и т.п.)
    — на стороне клиента, как и с добавками length_extra_*/width_extra_*."""
    depth_candidates = []
    slope_candidates = []
    stated_volume = None

    for e in msp.query("TEXT MTEXT"):
        text = (e.plain_text() if e.dxftype() == "MTEXT" else (e.dxf.text or "")).strip()
        if not text:
            continue
        pos = e.dxf.insert

        if _ELEV_RE.match(text):
            try:
                value = float(text.replace(",", "."))
            except ValueError:
                continue
            depth_candidates.append({
                "text": text, "value": value,
                # отрицательные — похоже на относительную отметку дна (от условного
                # 0.000); положительные в районе 100+ — похоже на чёрные отметки
                # земли по углам (как в PDF-варианте). Классификация приблизительная,
                # клиент показывает обе группы, а не выбирает молча одну.
                "guess": "relative_depth" if value < 0 else ("ground_mark" if value > 50 else "other"),
                "x": round(pos.x, 1), "y": round(pos.y, 1), "layer": e.dxf.layer,
            })

        if _SLOPE_RE.match(text):
            slope_candidates.append({"text": text, "x": round(pos.x, 1), "y": round(pos.y, 1), "layer": e.dxf.layer})

        if stated_volume is None:
            m = _STATED_VOLUME_RE.search(text)
            if m and not re.search(r"м\s*(²|2)", m.group(2)):  # площадь (м²) — не объём
                raw = m.group(1).replace(" ", "").replace(",", ".")
                try:
                    stated_volume = {"value_m3": float(raw), "quote": text[:300]}
                except ValueError:
                    pass

    return {"depth_candidates": depth_candidates, "slope_candidates": slope_candidates, "stated_volume": stated_volume}


_PILE_NUM_RE = re.compile(r"^\d+$")
# ГОСТ 19804-91: С<длина в дм>-<сечение в см>-<номер армирования>, например "С100-35-10"
# означает длину 10.0 м (100 дм), квадратное сечение 350х350 мм (35 см).
_PILE_MARK_RE = re.compile(r"^С\s?(\d+)[.\-](\d+)[.\-](\d+)", re.IGNORECASE)
_PILE_RANGE_RE = re.compile(r"^(\d+)\s*-\s*(\d+)$")
_CUT_ELEV_RE = re.compile(r"на\s+отм\.?\s*(-?[\d.,]+)", re.IGNORECASE)
_DATUM_RE = re.compile(r"абсолютной\s+отметке\s+([\d.,]+)", re.IGNORECASE)


def _dwg_extract_piles(msp) -> dict:
    """Считает сваи по номерным текстовым подписям на плане свайного поля (не
    по точкам/блокам — проверено на реальном файле проекта: сами сваи никак
    не обозначены отдельными блоками, только номер рядом с каждой) и находит
    марку сваи в примечаниях/спецификации, чтобы по ГОСТ 19804-91 определить
    длину и сечение — дальше количество × объём одной сваи = общий объём.

    НЕ пытается сама решить, какие числовые подписи на листе — номера свай, а
    какие — что-то другое (координаты, отметки и т.п.): считает ВСЕ чисто
    числовые подписи, а разбор на стороне клиента (обычно диапазон 1..N без
    пропусков — довольно характерный, легко отличимый паттерн от случайных
    чисел на чертеже)."""
    numbers = []
    excluded_numbers = 0
    marks = {}
    ranges = []
    cut_elev = None
    datum_abs = None
    test_piles_note = None

    for e in msp.query("TEXT MTEXT"):
        text = (e.plain_text() if e.dxftype() == "MTEXT" else (e.dxf.text or "")).strip()
        if not text:
            continue

        layer_lower = (e.dxf.layer or "").lower()
        # подписи осей и размерные слои — не номера свай (раньше номера осей
        # 1..18 попадали в счёт, если свай с такими номерами не было)
        is_axis_or_dim_layer = any(h in layer_lower for h in DWG_AXIS_LAYER_HINTS) or "dim" in layer_lower or "размер" in layer_lower

        if _PILE_NUM_RE.match(text) and len(text) <= 6:
            if is_axis_or_dim_layer:
                excluded_numbers += 1
            else:
                numbers.append(int(text))

        m = _PILE_MARK_RE.match(text)
        if m:
            length_dm, section_cm = int(m.group(1)), int(m.group(2))
            # ключ — полная марка (С100-35-10 и С100-35-11 — разные позиции),
            # а не только длина+сечение
            mark_key = re.sub(r"\s+", "", text.upper())
            marks[mark_key] = {
                "text": text, "length_m": length_dm / 10, "section_m": section_cm / 100,
            }

        if _PILE_RANGE_RE.match(text):
            ranges.append(text)

        m3 = _CUT_ELEV_RE.search(text)
        if m3 and cut_elev is None:
            try:
                cut_elev = float(m3.group(1).replace(",", "."))
            except ValueError:
                pass

        m4 = _DATUM_RE.search(text)
        if m4 and datum_abs is None:
            try:
                datum_abs = float(m4.group(1).replace(",", "."))
            except ValueError:
                pass

        low = text.lower()
        if test_piles_note is None and "пробн" in low and "сва" in low:
            test_piles_note = text

    unique_numbers = sorted(set(numbers))
    has_gaps = None
    if unique_numbers:
        has_gaps = unique_numbers != list(range(unique_numbers[0], unique_numbers[-1] + 1))
    # значения, подписанные более одного раза: либо дубль подписи, либо две
    # сваи с одним номером — в любом случае инженеру стоит посмотреть
    counts: dict = {}
    for n in numbers:
        counts[n] = counts.get(n, 0) + 1
    duplicates = sorted(n for n, c in counts.items() if c > 1)[:30]
    # длина непрерывного ряда 1..N — второй независимый признак «это нумерация свай»
    contiguous_from_1 = 0
    nums_set = set(unique_numbers)
    while (contiguous_from_1 + 1) in nums_set:
        contiguous_from_1 += 1

    return {
        "count": len(unique_numbers),
        "number_range": [unique_numbers[0], unique_numbers[-1]] if unique_numbers else None,
        "has_gaps": has_gaps,
        "contiguous_from_1": contiguous_from_1,
        "duplicate_numbers": duplicates,
        "excluded_axis_or_dim_numbers": excluded_numbers,
        "marks": list(marks.values()),
        "spec_ranges": ranges,
        "cut_elevation": cut_elev,
        "datum_absolute": datum_abs,
        "test_piles_note": test_piles_note,
    }


def _dwg_extract(request: Request, headers: dict):
    if "file" not in request.files:
        return (jsonify({"error": "Поле 'file' (DWG) не найдено в запросе"}), 400, headers)

    upload = request.files["file"]
    size_err = _check_upload_size(upload)
    if size_err:
        return (jsonify({"error": size_err}), 413, headers)

    with tempfile.TemporaryDirectory() as tmpdir:
        dwg_path = os.path.join(tmpdir, "input.dwg")
        upload.save(dwg_path)

        try:
            dxf_path, conv_warnings = _dwg_to_dxf(dwg_path, tmpdir)
        except FileNotFoundError:
            # dwg2dxf не найден в PATH — образ ещё не пересобран с LibreDWG.
            return (jsonify({"error": "dwg2dxf не установлен в образе — см. инструкцию по добавлению LibreDWG в Dockerfile"}), 500, headers)
        except (RuntimeError, subprocess.SubprocessError, subprocess.TimeoutExpired) as e:
            logger.exception("dwg2dxf failed")
            return (jsonify({"error": f"Не удалось сконвертировать DWG в DXF: {e}"}), 400, headers)

        try:
            doc = ezdxf.readfile(dxf_path)
        except Exception as e:
            return (jsonify({"error": f"ezdxf не смог прочитать сконвертированный DXF: {e}"}), 500, headers)

        msp = doc.modelspace()
        k, units_info = _dwg_units_scale(doc)
        extract_notes: list[str] = list(conv_warnings)
        if units_info["assumed"]:
            extract_notes.append(
                f"В файле не задан/неизвестен $INSUNITS={units_info['insunits']} — единицы чертежа приняты за мм. "
                "Если чертёж в метрах, все размеры будут неверны в 1000 раз."
            )
        elif k != 1.0:
            extract_notes.append(f"Единицы чертежа ($INSUNITS={units_info['insunits']}) приведены к мм (×{k}).")
        axes = _dwg_extract_axes(msp, k, extract_notes)
        dims = _dwg_extract_dimensions(msp, k, extract_notes)
        dims = _dwg_classify_dimensions(dims, axes)
        n_blocks = sum(1 for _ in msp.query("INSERT"))
        if n_blocks:
            extract_notes.append(
                f"В модели {n_blocks} вставок блоков (INSERT): размеры и подписи внутри блоков, а также на листах "
                "(layouts) НЕ разбираются — если данных не хватает, проверьте вручную."
            )
        depth_slope = _dwg_extract_depth_slope(msp)
        piles = _dwg_extract_piles(msp)

    warnings: list[str] = list(extract_notes)
    if not axes:
        warnings.append(
            "Оси не найдены по известным названиям слоёв (DWG_AXIS_LAYER_HINTS) — "
            "возможно, на этом чертеже слой осей называется иначе; добавьте его "
            "название в DWG_AXIS_LAYER_HINTS в main.py."
        )
    elif not any(a["axis_type"] for a in axes):
        warnings.append(
            "Ни одна ось не получила подпись numbered/lettered — сопоставление "
            "длины/ширины ниже может быть ненадёжным. Проверьте, что слой с "
            "подписями осей (обычно *-IDEN, например S-GRID-IDEN) действительно "
            "есть в этом DWG и назван похоже на один из DWG_AXIS_LAYER_HINTS."
        )

    return (jsonify({
        "axes": axes, "dimensions": dims, "warnings": warnings,
        "depth_candidates": depth_slope["depth_candidates"],
        "slope_candidates": depth_slope["slope_candidates"],
        "stated_volume": depth_slope["stated_volume"],
        "piles": piles,
        "units": units_info,
    }), 200, headers)


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
    size_err = _check_upload_size(upload)
    if size_err:
        return (jsonify({"error": size_err}), 413, headers)
    section_code = request.form.get("section_code", "")
    pages = request.form.get("pages", "")  # опционально: "1,3,5-7"
    # Плотность материала для резервной оценки объёма по массе (кг/м3).
    # По умолчанию — тяжёлый бетон/ж/б (2500). Для металлопроката, например,
    # нужно передать 7850.
    raw_density = request.form.get("density_kg_m3", "") or "2500"
    try:
        density_kg_m3 = float(str(raw_density).replace(",", "."))
        if not math.isfinite(density_kg_m3) or density_kg_m3 <= 0:
            raise ValueError
    except ValueError:
        return (jsonify({"error": f"Некорректная плотность density_kg_m3={raw_density!r} (нужно число > 0)"}), 400, headers)

    warnings: list[str] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = os.path.join(tmpdir, "input.pdf")
        upload.save(pdf_path)

        try:
            kwargs = dict(input_path=pdf_path, output_dir=tmpdir, format="json", quiet=True, table_method="cluster")
            if pages:
                if not re.fullmatch(r"[0-9,\-\s]+", pages):
                    return (jsonify({"error": f"Некорректный параметр pages: {pages!r}"}), 400, headers)
                kwargs["pages"] = pages
            opendataloader_pdf.convert(**kwargs)
        except Exception as e:
            logger.exception("OpenDataLoader failed")  # трассировка — только в лог, не клиенту
            return (jsonify({"error": f"Ошибка OpenDataLoader: {e}"}), 500, headers)

        json_path = os.path.join(tmpdir, "input.json")
        if not os.path.exists(json_path):
            return (jsonify({"error": "OpenDataLoader не создал JSON — проверьте, что файл действительно PDF"}), 500, headers)

        with open(json_path, encoding="utf-8") as f:
            odl_data = json.load(f)

    tables_raw: list[dict] = []
    _find_tables(odl_data, tables_raw)

    out_tables = []
    for t in tables_raw:
        try:
            grid = _grid(t)
        except ValueError as e:
            warnings.append(f"Таблица {t.get('id')}: {e}")
            continue
        # пропускаем совсем маленький мусор (2x2 "таблицы" — обычно это
        # ложные срабатывания на подписях чертежа, не настоящие таблицы).
        # Размеры считаем по самой сетке, а не только по метаданным ODL: если
        # ключи "number of rows/columns" переименуют, таблицы не пропадут молча.
        n_rows = len(grid)
        n_cols = max((len(r) for r in grid), default=0)
        if n_rows < 3 or n_cols < 2:
            continue
        report = verify_table(t)

        # Если в таблице нет строки/колонки "Итого"/"Всего" — сверить нечего,
        # но объём часто всё равно можно оценить через Кол-во x Масса ед.
        # (типичный случай — спецификации свай/подушек/приямков без итоговой
        # строки). Явно помечаем это как расчётную оценку, а не факт с чертежа.
        mass_estimate = None
        if not report.checks:
            est = estimate_volume_from_mass(grid, density_kg_m3=density_kg_m3)
            if est is not None and est.needs_confirmation:
                warnings.append(f"Таблица {t.get('id')}: оценка по массе не выдана — {est.needs_confirmation}")
                est = None
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
                    "mass_unit": est.mass_unit,
                    "header_rows": est.header_rows,
                    "skipped_rows": est.skipped_rows,
                    "warnings": est.warnings,
                }

        out_tables.append({
            "id": t.get("id"),
            "page": t.get("page number"),
            "rows": t.get("number of rows"),
            "cols": t.get("number of columns"),
            "grid": grid,
            "verification": {
                "status": report.status,  # passed | mismatch | nothing_to_verify
                "all_match": report.all_match,
                "notes": report.notes,
                "unparsed_cells": len(report.unparsed_cells),
                "skipped_columns": report.skipped_columns,
                "checks": [
                    {
                        "kind": c.kind, "label": c.label,
                        "stated": c.stated, "computed": c.computed,
                        "match": c.match, "tolerance": c.tolerance,
                        "column": c.column, "cells": c.cells,
                    }
                    for c in report.checks
                ],
            },
            "mass_estimate": mass_estimate,
        })

    if tables_raw and not out_tables:
        warnings.append(f"Найдено таблиц: {len(tables_raw)}, но все отфильтрованы как слишком малые или нечитаемые.")
    return (jsonify({"tables": out_tables, "warnings": warnings, "section_code": section_code}), 200, headers)
