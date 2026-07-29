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

Деплой (Google Cloud Functions, 2-е поколение, тот же способ через GitHub,
которым вы уже пользуетесь для остального):
  Точка входа: extract_and_verify_tables
  Runtime: Python 3.11+
  Требуется: Java 17+ в среде выполнения (см. requirements.txt и README ниже);
  requirements.txt должен включать "requests" (для /gemini-proxy)

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

Вызов из браузера (Gemini-прокси, новое):
  POST <URL функции>/gemini-proxy
  Content-Type: application/json
  Тело: {"model": "gemini-2.5-flash", "apiKey": "...", "body": {...тело запроса к Gemini как есть...}}

Функция ничего не сохраняет — ключ и тело просто пробрасываются в Gemini и
ответ возвращается как есть (плюс поле error.proxyError при сетевой ошибке
самого прокси, отдельно от обычных ошибок Gemini API).
"""

from __future__ import annotations

import json
import os
import tempfile
import traceback

import functions_framework
import requests
from flask import Request, jsonify

import opendataloader_pdf
from verify_table import verify_table, _grid, estimate_volume_from_mass  # тот же модуль, что и в takeoff_pipeline.py

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


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
    try:
        payload = request.get_json(force=True, silent=False)
    except Exception:
        return (jsonify({"error": {"proxyError": "Тело запроса не JSON"}}), 400, headers)

    if not payload or not payload.get("model") or not payload.get("apiKey") or "body" not in payload:
        return (jsonify({"error": {"proxyError": "Нужны поля model, apiKey, body"}}), 400, headers)

    url = f"{GEMINI_API_BASE}/{payload['model']}:generateContent"
    try:
        upstream = requests.post(
            url,
            headers={"Content-Type": "application/json", "x-goog-api-key": payload["apiKey"]},
            json=payload["body"],
            timeout=120,
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


@functions_framework.http
def extract_and_verify_tables(request: Request):
    # CORS preflight
    if request.method == "OPTIONS":
        return ("", 204, _cors_headers())

    headers = _cors_headers()

    if request.path.rstrip("/").endswith("/gemini-proxy"):
        return _gemini_proxy(request, headers)

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
