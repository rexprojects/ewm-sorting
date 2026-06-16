from __future__ import annotations

import csv
import io
import json
import re
import sys
import zipfile
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


BASE_DIR = Path(__file__).resolve().parent
SOURCE_DIR = BASE_DIR / "sorting files original"
ENCODING = "cp1252"
DELIMITER = ";"


COL_WAREHOUSE = 0
COL_BIN = 1
COL_ACTIVITY = 2
COL_RUNNING_NO = 3
COL_ACTIVITY_AREA = 4
COL_STORAGE_TYPE = 5
COL_AISLE = 7
COL_SORT_SEQUENCE = 8


STORAGE_PREFIX_ALIASES = {
    "UP": "ÜP",
    "AKF1": "ÜB1",
    "AKF2": "ÜB2",
}


@dataclass
class SortingFile:
    path: Path
    header: list[str]
    rows: list[list[str]]

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def data_rows(self) -> list[list[str]]:
        return [row for row in self.rows if cell(row, COL_BIN)]


def cell(row: list[str], index: int) -> str:
    return row[index] if index < len(row) else ""


def set_cell(row: list[str], index: int, value: str, width: int) -> None:
    while len(row) < width:
        row.append("")
    row[index] = value


def load_sorting_files() -> list[SortingFile]:
    files: list[SortingFile] = []
    for path in sorted(SOURCE_DIR.glob("*.csv")):
        with path.open("r", encoding=ENCODING, newline="") as handle:
            reader = csv.reader(handle, delimiter=DELIMITER)
            header = next(reader)
            rows = [normalize_width(row, len(header)) for row in reader]
        files.append(SortingFile(path, header, rows))
    return files


def normalize_width(row: list[str], width: int) -> list[str]:
    if len(row) < width:
        return row + [""] * (width - len(row))
    return row[:width]


def natural_parts(value: str) -> tuple[Any, ...]:
    parts: list[Any] = []
    for part in re.findall(r"\d+|[A-Za-zÄÖÜäöü]+|[^A-Za-zÄÖÜäöü\d]+", value.upper()):
        if part.isdigit():
            parts.append((0, int(part), len(part)))
        elif part.strip():
            parts.append((1, part))
    return tuple(parts)


def parse_bin(bin_name: str) -> dict[str, Any]:
    raw = bin_name.strip()
    compact = re.sub(r"\s+", " ", raw.upper())

    storage = re.match(r"^([A-Z]+)(\d{4,5})\s+([A-Z])(\d{2})$", compact)
    if storage:
        prefix, coordinate, level, slot = storage.groups()
        coordinate_number = int(coordinate)
        if prefix in {"MUS", "SPN"}:
            storage_type = prefix
        else:
            storage_type = f"{prefix}{coordinate[0]}"
        return {
            "kind": "storage",
            "prefix": prefix,
            "coordinate": coordinate_number,
            "level": level,
            "slot": int(slot),
            "storage_type": storage_type,
            "sort_key": (10, coordinate_number, level, int(slot), prefix),
        }

    hyphen = re.match(r"^([A-Z]+)(\d+)-(\d+)$", compact)
    if hyphen:
        prefix, group, number = hyphen.groups()
        prefix_group = f"{prefix}{group}"
        return {
            "kind": "hyphen",
            "prefix": prefix,
            "group": int(group),
            "number": int(number),
            "storage_type": STORAGE_PREFIX_ALIASES.get(prefix_group, STORAGE_PREFIX_ALIASES.get(prefix, prefix_group)),
            "sort_key": (0, prefix, int(group), int(number)),
        }

    simple = re.match(r"^([A-Z]+)(\d+)$", compact)
    if simple:
        prefix, number = simple.groups()
        return {
            "kind": "simple",
            "prefix": prefix,
            "number": int(number),
            "storage_type": f"{prefix}{number[0]}",
            "sort_key": (0, prefix, int(number)),
        }

    return {
        "kind": "generic",
        "prefix": re.match(r"^[A-Z]+", compact).group(0) if re.match(r"^[A-Z]+", compact) else "",
        "storage_type": "",
        "sort_key": (99, natural_parts(compact)),
    }


def infer_storage_type(bin_name: str) -> str:
    parsed = parse_bin(bin_name)
    storage_type = parsed.get("storage_type") or ""
    if storage_type.startswith("SPN"):
        return "SPN"
    return storage_type


def bin_sort_key(row_or_name: list[str] | str) -> tuple[Any, ...]:
    name = row_or_name if isinstance(row_or_name, str) else cell(row_or_name, COL_BIN)
    parsed = parse_bin(name)
    return parsed["sort_key"], natural_parts(name)


def row_activity(file: SortingFile) -> str:
    for row in file.data_rows:
        value = cell(row, COL_ACTIVITY)
        if value:
            return value
    return ""


def summarize_file(file: SortingFile) -> dict[str, Any]:
    rows = file.data_rows
    storage_types = ordered_unique(cell(row, COL_STORAGE_TYPE) for row in rows if cell(row, COL_STORAGE_TYPE))
    areas = ordered_unique(cell(row, COL_ACTIVITY_AREA) for row in rows if cell(row, COL_ACTIVITY_AREA))
    activity = row_activity(file)
    return {
        "name": file.name,
        "rows": len(rows),
        "activity": activity,
        "areas": areas,
        "storageTypes": storage_types,
        "firstBin": cell(rows[0], COL_BIN) if rows else "",
        "lastBin": cell(rows[-1], COL_BIN) if rows else "",
    }


def ordered_unique(values: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def nearest_template(file: SortingFile, bin_name: str, storage_type: str) -> tuple[list[str] | None, int]:
    target_key = bin_sort_key(bin_name)
    same_type_insert_at: int | None = None
    last_same_type_index: int | None = None
    same_type_previous: list[str] | None = None
    same_type_next: list[str] | None = None
    insert_at = len(file.rows)

    for index, row in enumerate(file.rows):
        if not cell(row, COL_BIN):
            continue
        current_key = bin_sort_key(row)
        if insert_at == len(file.rows) and target_key < current_key:
            insert_at = index
        if storage_type and cell(row, COL_STORAGE_TYPE) == storage_type:
            last_same_type_index = index
            if same_type_insert_at is None and target_key < current_key:
                same_type_insert_at = index
                same_type_next = row
            if same_type_insert_at is None and current_key <= target_key:
                same_type_previous = row

    if same_type_insert_at is not None:
        insert_at = same_type_insert_at
    elif last_same_type_index is not None:
        insert_at = last_same_type_index + 1

    if same_type_previous or same_type_next:
        return same_type_previous or same_type_next, insert_at

    previous = None
    for index, row in enumerate(file.rows):
        if not cell(row, COL_BIN):
            continue
        if index <= insert_at:
            previous = row
    return previous, insert_at

def file_accepts_bin(file: SortingFile, bin_name: str, selected_files: set[str] | None) -> bool:
    if selected_files is not None:
        return file.name in selected_files
    storage_type = infer_storage_type(bin_name)
    if not storage_type:
        return False
    candidates = {storage_type, f"{storage_type}P"}
    return any(
        cell(row, COL_STORAGE_TYPE) in candidates or cell(row, COL_ACTIVITY_AREA) in candidates
        for row in file.data_rows
    )


def effective_storage_type(file: SortingFile, bin_name: str) -> str:
    inferred = infer_storage_type(bin_name)
    if not inferred:
        return ""
    storage_types = {cell(row, COL_STORAGE_TYPE) for row in file.data_rows if cell(row, COL_STORAGE_TYPE)}
    if inferred in storage_types:
        return inferred
    if f"{inferred}P" in storage_types:
        return f"{inferred}P"
    return inferred


def build_new_row(file: SortingFile, bin_name: str) -> tuple[list[str], int, list[str]]:
    storage_type = effective_storage_type(file, bin_name)
    template, insert_at = nearest_template(file, bin_name, storage_type)
    width = len(file.header)
    if template:
        new_row = template.copy()
    else:
        new_row = [""] * width
        set_cell(new_row, COL_WAREHOUSE, "4901", width)
        set_cell(new_row, COL_ACTIVITY, row_activity(file), width)
    set_cell(new_row, COL_BIN, bin_name, width)
    template_storage_type = cell(template, COL_STORAGE_TYPE) if template else ""
    new_storage_type = "" if template and not template_storage_type else storage_type
    set_cell(new_row, COL_STORAGE_TYPE, new_storage_type, width)
    set_cell(new_row, COL_ACTIVITY, row_activity(file), width)
    return normalize_width(new_row, width), insert_at, template or []


def apply_bins(files: list[SortingFile], bins: list[str], selected_files: set[str] | None = None) -> tuple[list[SortingFile], list[dict[str, Any]]]:
    cloned = [SortingFile(file.path, file.header[:], [row[:] for row in file.rows]) for file in files]
    changes: list[dict[str, Any]] = []
    existing_by_file = {file.name: {canonical_bin_key(cell(row, COL_BIN)) for row in file.data_rows} for file in cloned}

    for bin_name in bins:
        normalized_bin = normalize_bin_name(bin_name)
        if not normalized_bin:
            continue
        for file in cloned:
            if not file_accepts_bin(file, normalized_bin, selected_files):
                continue
            if canonical_bin_key(normalized_bin) in existing_by_file[file.name]:
                changes.append({"file": file.name, "bin": normalized_bin, "action": "skipped", "reason": "bereits vorhanden"})
                continue
            row, insert_at, template = build_new_row(file, normalized_bin)
            file.rows.insert(insert_at, row)
            existing_by_file[file.name].add(canonical_bin_key(normalized_bin))
            changes.append({
                "file": file.name,
                "bin": normalized_bin,
                "action": "inserted",
                "position": insert_at + 1,
                "storageType": cell(row, COL_STORAGE_TYPE),
                "activityArea": cell(row, COL_ACTIVITY_AREA),
                "templateBin": cell(template, COL_BIN) if template else "",
            })
    for file in cloned:
        renumber(file)
    return cloned, changes


def normalize_bin_name(value: str) -> str:
    compact = re.sub(r"\s+", " ", value.strip().upper())
    storage = re.match(r"^([A-Z]+)(\d{4,5}) ([A-Z])(\d{2})$", compact)
    if storage:
        prefix, coordinate, level, slot = storage.groups()
        return f"{prefix}{coordinate}  {level}{slot}"
    return compact


def canonical_bin_key(value: str) -> str:
    return normalize_bin_name(value).upper()


def renumber(file: SortingFile) -> None:
    sequence = first_sequence(file)
    running = 1
    for row in file.rows:
        if not cell(row, COL_BIN):
            continue
        set_cell(row, COL_RUNNING_NO, str(running), len(file.header))
        set_cell(row, COL_SORT_SEQUENCE, str(sequence), len(file.header))
        running += 1
        sequence += 1


def first_sequence(file: SortingFile) -> int:
    for row in file.data_rows:
        value = cell(row, COL_SORT_SEQUENCE)
        if value.isdigit():
            return int(value)
    return 1


def make_zip(files: list[SortingFile]) -> bytes:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file in files:
            buffer = io.StringIO(newline="")
            writer = csv.writer(buffer, delimiter=DELIMITER, lineterminator="\r\n")
            writer.writerow(file.header)
            writer.writerows(file.rows)
            zf.writestr(file.name, buffer.getvalue().encode(ENCODING, errors="replace"))
    return archive.getvalue()


def parse_bins(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("bins", "")
    if isinstance(raw, list):
        values = raw
    else:
        values = re.split(r"[\n,;]+", str(raw))
    return [normalize_bin_name(value) for value in values if normalize_bin_name(value)]


def rules_summary(files: list[SortingFile]) -> dict[str, Any]:
    storage_map: dict[str, set[str]] = {}
    for file in files:
        for row in file.data_rows:
            storage_type = cell(row, COL_STORAGE_TYPE)
            if storage_type:
                storage_map.setdefault(storage_type, set()).add(file.name)
    return {
        "sourceDir": str(SOURCE_DIR),
        "encoding": ENCODING,
        "delimiter": DELIMITER,
        "files": [summarize_file(file) for file in files],
        "storageTypeRouting": {key: sorted(value) for key, value in sorted(storage_map.items())},
    }


class AppHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_html(INDEX_HTML)
            return
        if parsed.path == "/api/summary":
            self.send_json(rules_summary(load_sorting_files()))
            return
        self.send_error(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        files = load_sorting_files()
        selected = payload.get("files") or None
        selected_files = set(selected) if selected else None
        bins = parse_bins(payload)
        changed, changes = apply_bins(files, bins, selected_files)

        if parsed.path == "/api/preview":
            self.send_json({"changes": changes, "files": [summarize_file(file) for file in changed]})
            return
        if parsed.path == "/api/export":
            body = make_zip(changed)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="ewm-sorting-export.zip"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def send_json(self, data: Any) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


INDEX_HTML = r"""
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SAP EWM Sorting Manager</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #040606;
      --panel: #fff0b8;
      --panel-2: #024c40;
      --panel-3: #f7f1df;
      --line: rgba(255, 240, 184, .28);
      --text: #062f2b;
      --text-on-dark: #fff0b8;
      --muted: #5f766f;
      --primary: #024c40;
      --primary-dark: #013a31;
      --accent: #c75b39;
      --soft: rgba(2, 76, 64, .12);
      --warn: #fff0b8;
      font-family: "Segoe UI", Arial, sans-serif;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text-on-dark); }
    header { background: var(--bg); color: var(--text-on-dark); padding: 24px 28px 10px; display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; }
    h1 { font-size: 26px; margin: 0; font-weight: 800; letter-spacing: 0; }
    header p { margin: 5px 0 0; color: #d4c895; font-size: 13px; }
    #source { border: 1px solid var(--line); border-radius: 999px; padding: 7px 12px; color: var(--text-on-dark); font-size: 12px; white-space: nowrap; }
    main { padding: 18px 28px 34px; display: grid; grid-template-columns: minmax(320px, 430px) 1fr; gap: 22px; }
    section { background: var(--panel); color: var(--text); border: 1px solid rgba(255,255,255,.08); border-radius: 8px; overflow: hidden; box-shadow: 0 18px 40px rgba(0,0,0,.28); }
    section.dark { background: var(--panel-2); color: var(--text-on-dark); }
    section h2 { margin: 0; padding: 16px 18px; font-size: 16px; border-bottom: 1px solid rgba(2, 76, 64, .18); }
    section.dark h2 { border-color: rgba(255, 240, 184, .22); }
    h3 { margin: 0 0 10px; font-size: 13px; color: inherit; }
    .body { padding: 16px; }
    textarea { width: 100%; min-height: 154px; resize: vertical; border: 1px solid rgba(2, 76, 64, .26); border-radius: 8px; padding: 12px; font: 14px Consolas, monospace; background: rgba(255,255,255,.45); color: var(--text); outline-color: var(--primary); }
    label { display: block; font-size: 13px; font-weight: 650; margin: 0 0 8px; }
    .hint { color: var(--muted); font-size: 12px; line-height: 1.45; margin: 8px 0 0; }
    section.dark .hint { color: #cfc28d; }
    .actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; }
    button { border: 1px solid rgba(255,255,255,.18); border-radius: 999px; padding: 10px 15px; font-weight: 750; cursor: pointer; background: var(--primary); color: var(--text-on-dark); }
    button.secondary { background: transparent; color: var(--primary); border-color: rgba(2, 76, 64, .45); }
    button:hover { background: var(--primary-dark); }
    button.secondary:hover { background: rgba(2, 76, 64, .08); }
    .grid { display: grid; grid-template-columns: repeat(4, minmax(120px, 1fr)); gap: 12px; }
    .metric { background: rgba(255, 240, 184, .12); border: 1px solid rgba(255, 240, 184, .22); border-radius: 8px; padding: 12px; color: var(--text-on-dark); }
    .metric b { display: block; font-size: 22px; margin-bottom: 4px; color: var(--text-on-dark); }
    .metric span { color: #d4c895; font-size: 12px; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { border-bottom: 1px solid var(--line); padding: 9px 10px; text-align: left; vertical-align: top; }
    th { background: rgba(255, 240, 184, .09); color: #f5e9ae; font-size: 12px; position: sticky; top: 0; }
    .table-wrap { max-height: 410px; overflow: auto; border: 1px solid rgba(255, 240, 184, .2); border-radius: 8px; }
    .tag { display: inline-block; padding: 2px 7px; border-radius: 999px; background: rgba(255, 240, 184, .16); color: var(--text-on-dark); margin: 1px 3px 1px 0; font-size: 12px; }
    .file-list { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 8px; max-height: 220px; overflow: auto; padding: 2px; }
    .file-option { display: flex; gap: 8px; align-items: center; border: 1px solid rgba(2, 76, 64, .22); border-radius: 8px; padding: 8px; font-size: 12px; background: rgba(255,255,255,.28); }
    .file-option input { margin: 0; }
    .split { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 16px; }
    .status { margin-top: 12px; padding: 10px 12px; border-radius: 8px; background: rgba(2, 76, 64, .12); color: var(--text); font-size: 13px; display: none; }
    .status.show { display: block; }
    .preview-summary { display: grid; grid-template-columns: minmax(150px, .9fr) 1.4fr 1fr; gap: 12px; margin-bottom: 14px; }
    .preview-hero { background: var(--panel); color: var(--text); border-radius: 8px; padding: 18px; min-height: 118px; display: flex; flex-direction: column; justify-content: center; }
    .preview-hero.green { background: var(--panel-2); color: var(--text-on-dark); }
    .preview-hero .big { font-size: 34px; line-height: 1; font-weight: 850; margin-bottom: 8px; }
    .preview-hero .sub { font-size: 13px; color: inherit; opacity: .82; }
    .preview-groups { display: grid; gap: 12px; }
    .bin-group { border: 1px solid rgba(255, 240, 184, .22); border-radius: 8px; overflow: hidden; background: rgba(255,255,255,.03); }
    .bin-head { display: flex; justify-content: space-between; gap: 12px; align-items: center; padding: 12px 14px; background: rgba(255, 240, 184, .1); color: var(--text-on-dark); }
    .bin-title { font: 750 18px Consolas, monospace; }
    .bin-route { font-size: 12px; color: #d4c895; }
    .change-list { display: grid; }
    .change-row { display: grid; grid-template-columns: minmax(210px, 1.5fr) 86px 110px minmax(160px, 1fr); gap: 10px; align-items: center; padding: 10px 14px; border-top: 1px solid rgba(255, 240, 184, .14); color: #f7f1df; }
    .change-file { font-weight: 700; color: var(--text-on-dark); overflow-wrap: anywhere; }
    .change-meta { font-size: 12px; color: #d4c895; }
    .pill { display: inline-flex; align-items: center; justify-content: center; min-height: 26px; border: 1px solid rgba(255, 240, 184, .32); border-radius: 999px; padding: 4px 9px; font-size: 12px; color: var(--text-on-dark); }
    .empty-preview { border: 1px dashed rgba(255, 240, 184, .28); border-radius: 8px; padding: 18px; color: #d4c895; }
    @media (max-width: 960px) {
      main { grid-template-columns: 1fr; padding: 16px; }
      header { padding: 16px; align-items: flex-start; flex-direction: column; }
      .grid, .split, .preview-summary, .change-row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>SAP EWM Sorting Manager</h1>
      <p>Neue Lagerplätze einordnen, Sequenzen neu berechnen und CSV-Dateien exportieren</p>
    </div>
    <div id="source"></div>
  </header>
  <main>
    <div>
      <section>
        <h2>Neue Bins</h2>
        <div class="body">
          <label for="bins">Lagerplätze</label>
          <textarea id="bins" placeholder="KL12004  A01&#10;KL12004  A02&#10;AKF1-10"></textarea>
          <p class="hint">Ein Bin pro Zeile. Komma oder Semikolon funktionieren ebenfalls. Ohne Dateiauswahl routet das Tool automatisch über den erkannten Lagertyp.</p>
          <div class="actions">
            <button id="preview">Vorschau</button>
            <button id="export">Export ZIP</button>
            <button class="secondary" id="clearFiles">Dateiauswahl zurücksetzen</button>
          </div>
          <div id="routingMode" class="hint">Auto-Routing ist aktiv, solange keine Datei manuell ausgewählt ist.</div>
          <div id="status" class="status"></div>
        </div>
      </section>
      <section style="margin-top: 18px;">
        <h2>Dateien optional eingrenzen</h2>
        <div class="body">
          <div class="file-list" id="fileList"></div>
        </div>
      </section>
    </div>
    <div>
      <section class="dark">
        <h2>Abgeleitete Regeln</h2>
        <div class="body">
          <div class="grid" id="metrics"></div>
          <div class="split">
            <div>
              <h3>CSV-Dateien</h3>
              <div class="table-wrap"><table id="filesTable"></table></div>
            </div>
            <div>
              <h3>Lagertyp-Routing</h3>
              <div class="table-wrap"><table id="routingTable"></table></div>
            </div>
          </div>
        </div>
      </section>
      <section class="dark" style="margin-top: 18px;">
        <h2>Vorschau</h2>
        <div class="body">
          <div id="previewCards" class="empty-preview">Noch keine Vorschau berechnet.</div>
        </div>
      </section>
    </div>
  </main>
  <script>
    let summary = null;
    const $ = (id) => document.getElementById(id);

    async function loadSummary() {
      summary = await fetch('/api/summary').then(r => r.json());
      $('source').textContent = summary.encoding + ' / "' + summary.delimiter + '" / ' + summary.files.length + ' Dateien';
      renderSummary();
    }

    function selectedFiles() {
      const checked = [...document.querySelectorAll('.file-option input:checked')].map(input => input.value);
      return checked.length ? checked : null;
    }

    function updateRoutingMode() {
      const files = selectedFiles();
      $('routingMode').textContent = files
        ? `Manuelle Auswahl aktiv: ${files.length} Datei(en). Vorschau und Export nutzen nur diese Auswahl.`
        : 'Auto-Routing ist aktiv: Das Tool wählt passende Dateien über den erkannten Lagertyp.';
    }

    function renderSummary() {
      const totalRows = summary.files.reduce((sum, file) => sum + file.rows, 0);
      const storageTypes = Object.keys(summary.storageTypeRouting).length;
      $('metrics').innerHTML = [
        metric(summary.files.length, 'Sorting-Dateien'),
        metric(totalRows.toLocaleString('de-DE'), 'aktive Zeilen'),
        metric(storageTypes, 'erkannte Lagertypen'),
        metric('cp1252', 'Export-Encoding')
      ].join('');

      $('fileList').innerHTML = summary.files.map(file => `
        <label class="file-option">
          <input type="checkbox" value="${escapeHtml(file.name)}">
          <span>${escapeHtml(file.name)}<br><small>${file.rows.toLocaleString('de-DE')} Zeilen</small></span>
        </label>`).join('');
      document.querySelectorAll('.file-option input').forEach(input => input.addEventListener('change', updateRoutingMode));
      updateRoutingMode();

      $('filesTable').innerHTML = `
        <thead><tr><th>Datei</th><th>Activity</th><th>Bereiche</th><th>Lagertypen</th></tr></thead>
        <tbody>${summary.files.map(file => `
          <tr>
            <td>${escapeHtml(file.name)}<br><small>${escapeHtml(file.firstBin)} → ${escapeHtml(file.lastBin)}</small></td>
            <td>${escapeHtml(file.activity)}</td>
            <td>${tags(file.areas)}</td>
            <td>${tags(file.storageTypes)}</td>
          </tr>`).join('')}</tbody>`;

      $('routingTable').innerHTML = `
        <thead><tr><th>Lagertyp</th><th>betroffene Dateien</th></tr></thead>
        <tbody>${Object.entries(summary.storageTypeRouting).map(([type, files]) => `
          <tr><td><b>${escapeHtml(type)}</b></td><td>${files.map(escapeHtml).join('<br>')}</td></tr>`).join('')}</tbody>`;
    }

    function metric(value, label) {
      return `<div class="metric"><b>${value}</b><span>${label}</span></div>`;
    }

    function tags(values) {
      return values.slice(0, 8).map(value => `<span class="tag">${escapeHtml(value)}</span>`).join('') +
        (values.length > 8 ? `<span class="tag">+${values.length - 8}</span>` : '');
    }

    async function preview() {
      setStatus('Berechne Vorschau...');
      const manualFiles = selectedFiles();
      const response = await fetch('/api/preview', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({bins: $('bins').value, files: manualFiles})
      });
      const data = await response.json();
      renderPreview(data.changes, manualFiles);
      setStatus(statusText(data.changes, manualFiles));
    }

    async function exportZip() {
      setStatus('Erstelle ZIP...');
      const response = await fetch('/api/export', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({bins: $('bins').value, files: selectedFiles()})
      });
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = 'ewm-sorting-export.zip';
      link.click();
      URL.revokeObjectURL(url);
      setStatus('ZIP exportiert.');
    }

    function statusText(changes, manualFiles) {
      if (!changes.length) return 'Keine passenden neuen Bins gefunden.';
      const inserted = changes.filter(change => change.action === 'inserted').length;
      const skipped = changes.length - inserted;
      const files = new Set(changes.map(change => change.file)).size;
      const mode = manualFiles ? 'manuelle Dateiauswahl' : 'Auto-Routing';
      return `${inserted} Einf\u00fcgung(en) in ${files} Datei(en) per ${mode}. ${skipped ? skipped + ' bereits vorhandene Eintr\u00e4ge \u00fcbersprungen.' : 'Beim Export werden die Sequenzen je Datei neu berechnet.'}`;
    }

    function renderPreview(changes, manualFiles) {
      if (!changes.length) {
        $('previewCards').className = 'empty-preview';
        $('previewCards').textContent = 'Keine passenden Dateien gefunden. Pr\u00fcfe den Bin-Namen oder w\u00e4hle Dateien manuell aus.';
        return;
      }

      const inserted = changes.filter(change => change.action === 'inserted').length;
      const files = new Set(changes.map(change => change.file)).size;
      const grouped = groupBy(changes, change => change.bin);
      $('previewCards').className = '';
      $('previewCards').innerHTML = `
        <div class="preview-summary">
          <div class="preview-hero">
            <div class="big">${inserted}</div>
            <div class="sub">Einf\u00fcgungen</div>
          </div>
          <div class="preview-hero green">
            <div class="big">${files}</div>
            <div class="sub">betroffene Sorting-Dateien</div>
          </div>
          <div class="preview-hero">
            <div class="big">${manualFiles ? 'Manuell' : 'Auto'}</div>
            <div class="sub">${manualFiles ? 'nur ausgew\u00e4hlte Dateien' : 'Routing \u00fcber erkannten Lagertyp'}</div>
          </div>
        </div>
        <div class="preview-groups">
          ${Object.entries(grouped).map(([bin, binChanges]) => renderBinGroup(bin, binChanges)).join('')}
        </div>`;
    }

    function renderBinGroup(bin, changes) {
      const firstInsert = changes.find(change => change.action === 'inserted') || changes[0];
      const route = [firstInsert.storageType, firstInsert.activityArea].filter(Boolean).join(' / ') || firstInsert.reason || 'keine Regel';
      return `
        <div class="bin-group">
          <div class="bin-head">
            <div>
              <div class="bin-title">${escapeHtml(bin)}</div>
              <div class="bin-route">Erkannte Regel: ${escapeHtml(route)}</div>
            </div>
            <span class="pill">${changes.length} Datei(en)</span>
          </div>
          <div class="change-list">
            ${changes.map(renderChange).join('')}
          </div>
        </div>`;
    }

    function renderChange(change) {
      const isInserted = change.action === 'inserted';
      const action = isInserted ? 'wird eingef\u00fcgt' : 'bereits vorhanden';
      const position = isInserted ? `Zeile ${change.position}` : 'keine \u00c4nderung';
      const rule = isInserted
        ? `${change.storageType || '-'} / ${change.activityArea || '-'}`
        : change.reason;
      const template = change.templateBin ? `Vorlage: ${change.templateBin}` : (change.reason || '');
      return `
        <div class="change-row">
          <div>
            <div class="change-file">${escapeHtml(change.file)}</div>
            <div class="change-meta">${escapeHtml(template)}</div>
          </div>
          <span class="pill">${escapeHtml(action)}</span>
          <span class="pill">${escapeHtml(position)}</span>
          <div class="change-meta">${escapeHtml(rule)}</div>
        </div>`;
    }

    function groupBy(values, getKey) {
      return values.reduce((groups, value) => {
        const key = getKey(value);
        groups[key] = groups[key] || [];
        groups[key].push(value);
        return groups;
      }, {});
    }

    function setStatus(text) {
      $('status').textContent = text;
      $('status').classList.add('show');
    }

    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>"']/g, char => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      })[char]);
    }

    $('preview').addEventListener('click', preview);
    $('export').addEventListener('click', exportZip);
    $('clearFiles').addEventListener('click', () => {
      document.querySelectorAll('.file-option input').forEach(input => input.checked = false);
      updateRoutingMode();
      setStatus('Dateiauswahl zurückgesetzt. Auto-Routing ist wieder aktiv.');
    });
    loadSummary();
  </script>
</body>
</html>
"""


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = ThreadingHTTPServer(("127.0.0.1", port), AppHandler)
    print(f"SAP EWM Sorting Manager läuft auf http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
