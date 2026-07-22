"""
main.py — Cloud Function backend для «Помогатора»
====================================================
Единственная причина, по которой это отдельная функция, а не часть браузера:
OpenDataLoader написан на Java и требует JVM — в браузере это физически не
запустить (см. обсуждение). Всё остальное (чтение векторного текста, Gemini)
по-прежнему работает прямо в браузере без этого сервера.

Деплой (Google Cloud Functions, 2-е поколение, тот же способ через GitHub,
которым вы уже пользуетесь для остального):
  Точка входа: extract_and_verify_tables
  Runtime: Python 3.11+
  Требуется: Java 17+ в среде выполнения (см. requirements.txt и README ниже)

Вызов из браузера:
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
"""

from __future__ import annotations

import json
import os
import tempfile
import traceback

import functions_framework
from flask import Request, jsonify

import opendataloader_pdf
from verify_table import verify_table, _grid  # тот же модуль, что и в takeoff_pipeline.py


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


@functions_framework.http
def extract_and_verify_tables(request: Request):
    # CORS preflight
    if request.method == "OPTIONS":
        return ("", 204, _cors_headers())

    headers = _cors_headers()

    if "file" not in request.files:
        return (jsonify({"error": "Поле 'file' (PDF) не найдено в запросе"}), 400, headers)

    upload = request.files["file"]
    section_code = request.form.get("section_code", "")
    pages = request.form.get("pages", "")  # опционально: "1,3,5-7"

    warnings: list[str] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = os.path.join(tmpdir, "input.pdf")
        upload.save(pdf_path)

        try:
            kwargs = dict(input_path=pdf_path, output_dir=tmpdir, format="json", quiet=True)
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
        })

    return (jsonify({"tables": out_tables, "warnings": warnings, "section_code": section_code}), 200, headers)
