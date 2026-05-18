#!/usr/bin/env python3
"""
REE Access Grid Scraper
=======================
Scrapes grid access & connection request data from Red Eléctrica de España.
https://www.ree.es/es/clientes/generador/acceso-conexion/conoce-el-estado-de-las-solicitudes

Endpoints:
  GET  /es/access_grid/getnodes?vol={voltage}&group=GI
  GET  /es/access_grid/getprovince?tax={province}
  POST /access_grid/getdata?_wrapper_format=drupal_ajax

Usage:
  python scrape_ree.py                          # full scrape
  python scrape_ree.py --aggregates-only        # nacional/peninsular/CCAAs only
  python scrape_ree.py --nodes-only             # node-level only
  python scrape_ree.py --output-dir ./data      # custom output
  python scrape_ree.py --delay 0.5              # seconds between requests

Requirements: requests (pip install requests)
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests

# ─── CONFIG ────────────────────────────────────────────────────────────────

BASE_URL = "https://www.ree.es"
GETDATA_URL = f"{BASE_URL}/access_grid/getdata"
GETNODES_URL = f"{BASE_URL}/es/access_grid/getnodes"
GETPROVINCE_URL = f"{BASE_URL}/es/access_grid/getprovince"
PAGE_URL = f"{BASE_URL}/es/clientes/generador/acceso-conexion/conoce-el-estado-de-las-solicitudes"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
    "Referer": PAGE_URL,
    "Origin": BASE_URL,
    "X-Requested-With": "XMLHttpRequest",
}

VOLTAGES = [400, 220, 132, 66]
GROUP = "GI"

# Drupal aggregated library hash — recapture from DevTools if requests start failing
AJAX_LIBRARIES = (
    "eJx9ktty4yAMhl-ImKt9HkaAgkkAMZKcrffpl7hJ2tjT3jDo40dniFEJ2mrhcZnOTE2NR1Vkhx-d"
    "BKM75zJMsQkbMhTjiVSUoTvRtaDYXpaU2-QhXBPT0qILVIgnv-QSf5RX4O3Xr6I-UsstPVSBGG0jr"
    "lDyPzRhEaXqIAQUcYlztEf0VIUZWMWpkD2Ql4bomtGT2pI9A6_uL3rGV6QICgVWZLsHB8Xj50G484"
    "g3bCr2zXq-5XYmT0XA7sFT0YEhUO3UNi-pkIdyutM0Wjm_ZIsvOYBmaq9Q35n5qsvjDLdMLCYRpYJ"
    "OIdk0jr09wQU-3mE1wwct6mKWQDfk1Y7ExiqYgnAuqG6M_IocykhhxHrQ0xs1DRTxRq4zXTCM-VToz"
    "8okcO6jQb9qxv6MjTGvLoiNvHQo0xeZlrbVLzNGc5-SB8HPy3MP_5wuciojjuj2sPN-R3WsGCSUzWg"
    "U0QgCh9lBzw4WpftoRoFof-BGKGQormLM4Ibfq9gjmnTGOrTraE_9zFT9ECSo2Ba7vU7fyH9wyoYp"
)

TECHNOLOGIES = [
    "eolica", "fotovoltaica", "almacenamiento", "hibridacion",
    "termosolar", "hidraulica", "bombeo", "otras",
]

TECH_LABELS = {
    "eolica": "Eólica",
    "fotovoltaica": "Fotovoltaica",
    "almacenamiento": "Baterías",
    "hibridacion": "Hibridación",
    "termosolar": "Termosolar",
    "hidraulica": "Hidráulica",
    "bombeo": "Bombeo",
    "otras": "Otras tecnologías",
}

# REE taxonomy names for CCAAs — confirmed working as of April 2026.
# Each entry is (primary_name, [fallback_names...]).
CCAAS_WITH_FALLBACKS = [
    ("Andalucía", []),
    ("Aragón", []),
    ("Asturias", ["Principado de Asturias"]),
    ("Canarias", []),
    ("Cantabria", []),
    ("Castilla y León", []),
    ("Castilla La Mancha", ["Castilla-La Mancha", "Castilla - La Mancha"]),
    ("Cataluña", []),
    ("Comunidad Valenciana", ["Comunitat Valenciana", "Valencia"]),
    ("Extremadura", []),
    ("Galicia", []),
    ("Baleares", ["Islas Baleares", "Illes Balears"]),
    ("La Rioja", []),
    ("Madrid", ["Comunidad de Madrid"]),
    ("Murcia", ["Región de Murcia"]),
    ("Navarra", ["Comunidad Foral de Navarra"]),
    ("País Vasco", []),
]

# Flat list for metadata — will be populated with actual working names during scrape
CCAAS = [name for name, _ in CCAAS_WITH_FALLBACKS]


# ─── HTTP SESSION ──────────────────────────────────────────────────────────

def make_session():
    """Create a requests session with retry logic."""
    s = requests.Session()
    s.headers.update(HEADERS)
    adapter = requests.adapters.HTTPAdapter(
        max_retries=requests.adapters.Retry(
            total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504]
        )
    )
    s.mount("https://", adapter)
    return s


# ─── PARSER ────────────────────────────────────────────────────────────────

FC_REGEX = re.compile(
    r"new FusionCharts\((\{.*?\})\);FusionCharts", re.DOTALL
)


def parse_fusionchart_response(raw_text: str) -> dict | None:
    """
    Parse a Drupal AJAX response containing FusionCharts configs.
    Returns dict with keys 'capacidad_acceso' and 'potencia_instalada',
    each mapping technology → status → {RdT: value, RdD: value}.
    """
    # Unwrap Drupal AJAX JSON envelope
    try:
        ajax_commands = json.loads(raw_text)
        insert_cmd = next(
            (c for c in ajax_commands if c.get("command") == "insert" and c.get("data")),
            None,
        )
        if not insert_cmd:
            return None
        html = insert_cmd["data"]
    except (json.JSONDecodeError, TypeError):
        html = raw_text

    result = {"capacidad_acceso": {}, "potencia_instalada": {}}

    for match in FC_REGEX.finditer(html):
        try:
            config = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue

        chart_id = config.get("id", "")
        ds = config.get("dataSource")
        if not ds or not ds.get("dataset") or not ds.get("categories"):
            continue

        # Categories are typically ["RdT", "RdD"]
        categories = [c["label"] for c in ds["categories"][0].get("category", [])]

        # "eolicaModule" → potencia_instalada, "eolica" → capacidad_acceso
        is_module = chart_id.endswith("Module")
        tech_key = chart_id.replace("Module", "") if is_module else chart_id
        section = "potencia_instalada" if is_module else "capacidad_acceso"

        if tech_key not in TECHNOLOGIES:
            continue

        tech_data = {}
        for series in ds["dataset"]:
            status = series.get("seriesName")
            if not status:
                continue
            values = {}
            for i, cat in enumerate(categories):
                raw_val = series.get("data", [{}])[i].get("value") if i < len(series.get("data", [])) else None
                values[cat] = round(raw_val, 2) if raw_val is not None else None
            tech_data[status] = values

        result[section][tech_key] = tech_data

    # Check we got at least some data
    if not result["capacidad_acceso"] and not result["potencia_instalada"]:
        return None
    return result


def flatten_to_rows(parsed: dict, meta: dict) -> list[dict]:
    """Convert parsed FusionCharts data to flat row dicts for CSV."""
    rows = []
    for section, techs in parsed.items():
        for tech, statuses in techs.items():
            for status, grids in statuses.items():
                for grid, value in grids.items():
                    rows.append({
                        **meta,
                        "section": section,
                        "technology": tech,
                        "technology_label": TECH_LABELS.get(tech, tech),
                        "status": status,
                        "grid": grid,
                        "value_mw": value,
                    })
    return rows


# ─── SCRAPING FUNCTIONS ───────────────────────────────────────────────────

def fetch_all_nodes(session: requests.Session, delay: float) -> dict:
    """GET all grid nodes across voltage levels."""
    print("\n═══ Fetching grid nodes ═══\n")
    all_nodes = {}

    for vol in VOLTAGES:
        try:
            r = session.get(GETNODES_URL, params={"vol": vol, "group": GROUP}, timeout=20)
            r.raise_for_status()
            data = r.json()
            count = len(data)
            print(f"  vol={vol} → {count} nodes")
            for nid, node in data.items():
                node["voltage"] = str(vol)
                all_nodes[nid] = node
        except Exception as e:
            print(f"  vol={vol} → error: {e}")
        time.sleep(delay)

    print(f"\n  Total unique nodes: {len(all_nodes)}")
    return all_nodes


def fetch_getdata(session: requests.Session, *, zone: str, ccaa: str = "", node: str = "") -> dict | None:
    """POST to the Drupal AJAX getdata endpoint and parse the response."""
    body = {
        "group": GROUP,
        "zone": zone,
        "ccaa": ccaa,
        "node": node,
        "_drupal_ajax": "1",
        "ajax_page_state[theme]": "ree",
        "ajax_page_state[theme_token]": "",
        "ajax_page_state[libraries]": AJAX_LIBRARIES,
    }

    r = session.post(
        GETDATA_URL,
        params={"_wrapper_format": "drupal_ajax"},
        data=body,
        headers={
            **HEADERS,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        },
        timeout=30,
    )
    r.raise_for_status()
    return parse_fusionchart_response(r.text)


def scrape_aggregates(session: requests.Session, delay: float) -> list[dict]:
    """Scrape nacional, peninsular, and CCAA-level data."""
    print("\n═══ Fetching aggregate data ═══\n")
    rows = []

    for zone, label in [("NACIONAL", "Nacional"), ("PENINSULAR", "Peninsular")]:
        print(f"  {label}...", end=" ", flush=True)
        try:
            parsed = fetch_getdata(session, zone=zone)
            if parsed:
                r = flatten_to_rows(parsed, {
                    "zone": zone, "zone_detail": label,
                    "node_id": "", "node_name": "", "voltage": "", "lat": "", "lng": "",
                })
                rows.extend(r)
                print(f"→ {len(r)} data points")
            else:
                print("→ no data")
        except Exception as e:
            print(f"→ error: {e}")
        time.sleep(delay)

    for primary_name, fallbacks in CCAAS_WITH_FALLBACKS:
        names_to_try = [primary_name] + fallbacks
        found = False
        for name in names_to_try:
            if name != primary_name:
                print(f"  CCAA: {primary_name} (trying '{name}')...", end=" ", flush=True)
            else:
                print(f"  CCAA: {name}...", end=" ", flush=True)
            try:
                parsed = fetch_getdata(session, zone="CCAA", ccaa=name)
                if parsed:
                    r = flatten_to_rows(parsed, {
                        "zone": "CCAA", "zone_detail": name,
                        "node_id": "", "node_name": "", "voltage": "", "lat": "", "lng": "",
                    })
                    rows.extend(r)
                    print(f"→ {len(r)} data points")
                    found = True
                    break
                else:
                    print("→ no data")
            except Exception as e:
                print(f"→ error: {e}")
            time.sleep(delay)
        if not found:
            print(f"  ⚠ {primary_name}: all name variants failed")

    return rows


def scrape_node_data(session: requests.Session, all_nodes: dict, delay: float) -> list[dict]:
    """Scrape data for every individual node."""
    print("\n═══ Fetching node-level data ═══\n")
    rows = []
    node_ids = list(all_nodes.keys())
    total = len(node_ids)
    scraped = 0
    errors = 0

    for i, nid in enumerate(node_ids, 1):
        node = all_nodes[nid]
        try:
            parsed = fetch_getdata(session, zone="NODES", node=nid)
            if parsed:
                r = flatten_to_rows(parsed, {
                    "zone": "NODES",
                    "zone_detail": node.get("title", ""),
                    "node_id": nid,
                    "node_name": node.get("title", ""),
                    "voltage": node.get("voltage", ""),
                    "lat": node.get("lat", ""),
                    "lng": node.get("lng", ""),
                })
                rows.extend(r)
                scraped += 1
            else:
                errors += 1
        except Exception:
            errors += 1

        if i % 25 == 0:
            print(f"  Progress: {i}/{total} ({scraped} ok, {errors} errors)")

        time.sleep(delay)

    print(f"\n  Completed: {scraped} nodes, {errors} errors, {len(rows)} data points")
    return rows


# ─── OUTPUT ────────────────────────────────────────────────────────────────

CSV_COLUMNS = [
    "zone", "zone_detail", "node_id", "node_name", "voltage", "lat", "lng",
    "section", "technology", "technology_label", "status", "grid", "value_mw",
]

NODE_GEO_COLUMNS = ["node_id", "title", "voltage", "lat", "lng"]


def write_csv(filepath: str, rows: list[dict], columns: list[str] | None = None):
    """Write rows to CSV with UTF-8 BOM for Excel compatibility."""
    if not rows:
        return
    columns = columns or list(rows[0].keys())
    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Saved {filepath} ({len(rows)} rows)")


def write_json(filepath: str, data):
    """Write data to JSON."""
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  Saved {filepath}")


# ─── MAIN ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="REE Access Grid Scraper")
    parser.add_argument("--nodes-only", action="store_true", help="Scrape only node-level data")
    parser.add_argument("--aggregates-only", action="store_true", help="Scrape only nacional/peninsular/CCAA")
    parser.add_argument("--output-dir", default="./output/raw", help="Output directory")
    parser.add_argument("--delay", type=float, default=0.4, help="Seconds between requests")
    args = parser.parse_args()

    today = date.today().isoformat()
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    print("╔═══════════════════════════════════════════════════════╗")
    print("║  REE Access Grid Scraper (Python)                    ║")
    print("║  Red Eléctrica de España — Solicitudes de acceso     ║")
    print("╚═══════════════════════════════════════════════════════╝")
    print(f"\n  Output:  {out}/")
    print(f"  Delay:   {args.delay}s")
    print(f"  Date:    {today}")
    mode = "nodes only" if args.nodes_only else "aggregates only" if args.aggregates_only else "full"
    print(f"  Mode:    {mode}\n")

    session = make_session()

    # ── Step 1: Node coordinates ──
    all_nodes = fetch_all_nodes(session, args.delay)

    node_geo_rows = [
        {"node_id": nid, **{k: n.get(k, "") for k in ["title", "voltage", "lat", "lng"]}}
        for nid, n in all_nodes.items()
    ]
    write_csv(str(out / "nodes_geo.csv"), node_geo_rows, NODE_GEO_COLUMNS)
    write_json(str(out / "nodes_geo.json"), all_nodes)

    all_data_rows = []

    # ── Step 2: Aggregates ──
    if not args.nodes_only:
        agg_rows = scrape_aggregates(session, args.delay)
        all_data_rows.extend(agg_rows)
        write_csv(str(out / f"aggregates_{today}.csv"), agg_rows, CSV_COLUMNS)

    # ── Step 3: Nodes ──
    if not args.aggregates_only:
        node_rows = scrape_node_data(session, all_nodes, args.delay)
        all_data_rows.extend(node_rows)
        write_csv(str(out / f"nodes_data_{today}.csv"), node_rows, CSV_COLUMNS)

    # ── Step 4: Combined ──
    if not args.nodes_only and not args.aggregates_only:
        write_csv(str(out / f"all_data_{today}.csv"), all_data_rows, CSV_COLUMNS)

    # ── Metadata ──
    meta = {
        "scrape_date": today,
        "data_source": PAGE_URL,
        "note": "Data updated monthly by REE, as of end of previous month",
        "total_nodes": len(all_nodes),
        "total_data_points": len(all_data_rows),
        "voltages": VOLTAGES,
        "group": GROUP,
        "technologies": TECH_LABELS,
        "statuses": ["En curso", "Con permisos", "Puesta en servicio"],
        "grids": {"RdT": "Red de Transporte", "RdD": "Red de Distribución"},
        "sections": {
            "capacidad_acceso": "Capacidad de acceso de instalaciones (MW)",
            "potencia_instalada": "Potencia instalada de módulos (MW)",
        },
        "ccaas_scraped": CCAAS,
    }
    write_json(str(out / "metadata.json"), meta)

    # ── Summary ──
    print("\n═══ Summary ═══")
    print(f"  Nodes:        {len(all_nodes)}")
    print(f"  Data points:  {len(all_data_rows)}")
    print(f"  Output:       {out}/")
    print()

    if not args.nodes_only and not args.aggregates_only and len(all_data_rows) == 0:
        print("ERROR: No data scraped — aborting", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
