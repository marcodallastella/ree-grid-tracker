# REE Grid Access Requests

Scrapes and tidies grid access & connection request data from [Red Eléctrica de España](https://www.ree.es/es/clientes/generador/acceso-conexion/conoce-el-estado-de-las-solicitudes). Data is published monthly by REE and covers renewable energy projects applying for grid connection across Spain.

## Project structure

```
REE/
├── scrape_ree.py       Scrapes raw data from REE's API
├── clean_ree.py        Cleans, geo-enriches, and tidies the raw data
├── requirements.txt    Pinned Python dependencies
├── geo/                Reference files used by clean_ree.py
│   ├── georef-spain-comunidad-autonoma.geojson
│   ├── spain-provinces.geojson
│   ├── spain-municipalities.json
│   └── diccionario25.xlsx
└── output/
    ├── raw/
    │   ├── all_data.csv            Current scraper output
    │   ├── aggregates.csv
    │   ├── nodes_data.csv
    │   ├── nodes_geo.csv
    │   ├── nodes_geo.json
    │   ├── metadata.json
    │   └── snapshots/              Month-stamped archive
    │       ├── all_data_2026-04.csv
    │       ├── aggregates_2026-04.csv
    │       └── ...
    └── clean/
        ├── ree_nodes.csv           Current tidy outputs
        ├── ree_ccaa.csv
        ├── ree_national.csv
        └── snapshots/              Month-stamped archive
            ├── ree_nodes_2026-04.csv
            ├── ree_ccaa_2026-04.csv
            └── ...
```

## Automated workflow (GitHub Actions)

A GitHub Actions workflow runs the full scrape → clean pipeline automatically:

- **Schedule:** Daily at 06:00 UTC during days 1–15 of each month (when REE publishes new data), plus every Monday as a safety net.
- **Manual trigger:** Use the "Run workflow" button in the Actions tab.
- **Commits:** If the cleaned outputs differ from the previous run, the workflow commits the update. If nothing changed, it exits cleanly with no commit.
- **Failure handling:** If the scraper fails or returns empty data, the workflow opens a GitHub issue labeled `scraper-broken` with a link to the run logs and a pointer to the Maintenance section below.

```
scrape_ree.py  →  output/raw/all_data_{date}.csv  →  clean_ree.py  →  output/clean/
```

All date-stamped raw files are kept as an archive in `output/raw/`. `clean_ree.py` auto-discovers the latest `all_data_*.csv` by filename.

### Running locally

```bash
pip install -r requirements.txt

# Full scrape (~677 nodes + aggregates, ~5 min at default delay)
python scrape_ree.py

# Aggregates only (nacional + peninsular + 17 CCAAs, ~30 sec)
python scrape_ree.py --aggregates-only

# Node-level only
python scrape_ree.py --nodes-only

# Custom delay and output directory
python scrape_ree.py --delay 0.6 --output-dir ./output/raw

# Clean (auto-discovers latest raw file)
python clean_ree.py
```

Produces three tidy CSVs in `output/clean/`.

## Output files

### `output/raw/` — scraper outputs

| File | Contents |
|------|----------|
| `all_data_{date}.csv` | All rows combined (aggregates + nodes) — primary input for `clean_ree.py` |
| `aggregates_{date}.csv` | Nacional / Peninsular / CCAA rows only |
| `nodes_data_{date}.csv` | Node-level rows only |
| `nodes_geo.csv` | Node ID, name, voltage (kV), lat, lng |
| `nodes_geo.json` | Same, as JSON (keyed by node ID) |
| `metadata.json` | Field definitions and scrape config |

### `output/clean/` — tidy outputs

| File | Grain | Contents |
|------|-------|----------|
| `ree_nodes.csv` | node × technology × status × grid | Node-level data enriched with CCAA, provincia, municipio |
| `ree_ccaa.csv` | ccaa × technology × status × grid | CCAA-level aggregates as reported by REE |
| `ree_national.csv` | scope × technology × status × grid | Nacional and Peninsular aggregates |

All three files share the same column schema for the measure columns:

| Column | Description |
|--------|-------------|
| `capacidad_acceso_mw` | Access capacity requested (MW) |
| `potencia_instalada_mw` | Installed module power (MW) |

### Raw CSV schema

| Column | Values |
|--------|--------|
| `zone` | `NACIONAL` · `PENINSULAR` · `CCAA` · `NODES` |
| `zone_detail` | Zone name (e.g. "Andalucía", "ALCORES 220") |
| `node_id` | REE internal node ID (empty for aggregates) |
| `node_name` | Node name (nodes only) |
| `voltage` | kV: `400` · `220` · `132` · `66` |
| `lat`, `lng` | Coordinates (nodes only) |
| `section` | `capacidad_acceso` · `potencia_instalada` |
| `technology` | `eolica` · `fotovoltaica` · `almacenamiento` · `hibridacion` · `termosolar` · `hidraulica` · `bombeo` · `otras` |
| `status` | `En curso` · `Con permisos` · `Puesta en servicio` |
| `grid` | `RdT` (Red de Transporte) · `RdD` (Red de Distribución) |
| `value_mw` | MW (may be null) |

## API endpoints

| Method | URL | Params |
|--------|-----|--------|
| GET | `/es/access_grid/getnodes` | `vol={400\|220\|132\|66}&group=GI` |
| GET | `/es/access_grid/getprovince` | `tax={province_name}` |
| POST | `/access_grid/getdata?_wrapper_format=drupal_ajax` | Form body with zone, ccaa, node |

## Maintenance

The `AJAX_LIBRARIES` constant in `scrape_ree.py` is a Drupal cache key. If REE rebuilds their cache (typically after site deployments), requests return empty data. To fix: open DevTools → Network → XHR, click any node on the map, copy the new `ajax_page_state[libraries]` value from the POST body, and update `AJAX_LIBRARIES` in the script.
