"""
Clean and tidy REE grid connection request data.

Input files:
  all_data_2026-04-13.csv                        Raw scrape from REE
  georef-spain-comunidad-autonoma.geojson         CCAA boundaries
  spain-provinces.geojson                         Province boundaries
  spain-municipalities.json                       Municipality boundaries (TopoJSON, INE codes)
  diccionario25.xlsx                              INE municipality code → name lookup

Output:
  ree_nodes.csv     Node-level data with ccaa, provincia, municipio columns
  ree_ccaa.csv      CCAA aggregates (from REE)
  ree_national.csv  National/peninsular aggregates (from REE)

Dependencies: pandas, geopandas, shapely, openpyxl
  pip install pandas geopandas shapely openpyxl
"""

import pandas as pd
import geopandas as gpd
from shapely.geometry import Point
from pathlib import Path
import warnings
warnings.filterwarnings("ignore", category=UserWarning)

# =====================================================================
# Config — adjust paths as needed
# =====================================================================
def _find_latest_raw():
    candidates = sorted(Path("output/raw").glob("all_data_*.csv"))
    if not candidates:
        raise FileNotFoundError("No all_data_*.csv found in output/raw/")
    return candidates[-1]

INPUT           = _find_latest_raw()
CCAA_GEOJSON    = Path("geo/georef-spain-comunidad-autonoma.geojson")
PROV_GEOJSON    = Path("geo/spain-provinces.geojson")
MUNI_TOPOJSON   = Path("geo/spain-municipalities.json")
INE_DICT        = Path("geo/diccionario25.xlsx")
OUTPUT_DIR      = Path("output/clean/")
PROJ_CRS        = "EPSG:25830"  # ETRS89 / UTM 30N — for accurate nearest-joins

# CCAA name normalization: GeoJSON official names → REE short names
CCAA_NAME_MAP = {
    "Principado de Asturias":    "Asturias",
    "Illes Balears":             "Baleares",
    "Castilla-La Mancha":        "Castilla La Mancha",
    "Comunitat Valenciana":      "Comunidad Valenciana",
    "Comunidad de Madrid":       "Madrid",
    "Región de Murcia":          "Murcia",
    "Comunidad Foral de Navarra":"Navarra",
}

# =====================================================================
# Helpers
# =====================================================================

def spatial_assign(points, polygons, field, proj_crs=PROJ_CRS):
    """Assign a polygon attribute to points via spatial join.
    Uses 'within' first, falls back to 'nearest' for edge cases."""
    joined = gpd.sjoin(
        points, polygons[[field, "geometry"]], how="left", predicate="within"
    ).drop_duplicates(subset="node_id", keep="first")

    unmatched = joined[joined[field].isna()]
    if len(unmatched) > 0:
        print(f"  ⚠ {len(unmatched)} nodes outside {field} polygons — assigning by nearest")
        nearest = gpd.sjoin_nearest(
            points.loc[unmatched.index].to_crs(proj_crs),
            polygons[[field, "geometry"]].to_crs(proj_crs),
            how="left",
        ).drop_duplicates(subset="node_id", keep="first")
        joined.loc[unmatched.index, field] = nearest[field].values

    return joined.set_index("node_id")[field]


# =====================================================================
# Load raw data
# =====================================================================
df = pd.read_csv(INPUT, encoding="utf-8-sig")

# Save technology label lookup, then drop the redundant column
TECH_LABELS = df.set_index("technology")["technology_label"].drop_duplicates().to_dict()
df = df.drop(columns=["technology_label"])

# Split by observational unit
nodes_raw  = df[df["zone"] == "NODES"].copy()
ccaa_raw   = df[df["zone"] == "CCAA"].copy()
nat_raw    = df[df["zone"].isin(["NACIONAL", "PENINSULAR"])].copy()


# =====================================================================
# 1. NODES — geographic enrichment + pivot
# =====================================================================
nodes_raw = nodes_raw.drop(columns=["zone", "zone_detail"])  # zone_detail == node_name

# Build GeoDataFrame of unique node locations
node_locs = nodes_raw.drop_duplicates(subset="node_id")[["node_id", "lat", "lng"]].copy()
node_points = gpd.GeoDataFrame(
    node_locs,
    geometry=[Point(lng, lat) for lng, lat in zip(node_locs["lng"], node_locs["lat"])],
    crs="EPSG:4326",
)

# Load boundary files
ccaa_polys = gpd.read_file(CCAA_GEOJSON).to_crs("EPSG:4326")
ccaa_polys = ccaa_polys.rename(columns={"acom_name": "ccaa"})
ccaa_polys["ccaa"] = ccaa_polys["ccaa"].replace(CCAA_NAME_MAP)

prov_polys = gpd.read_file(PROV_GEOJSON).to_crs("EPSG:4326")
prov_polys = prov_polys.rename(columns={"name": "provincia"})

muni_polys = gpd.read_file(MUNI_TOPOJSON, layer="municipalities").set_crs("EPSG:4326")
muni_polys = muni_polys.rename(columns={"id": "municipio_ine"})

# Load INE municipality code → name lookup
ine = pd.read_excel(INE_DICT, skiprows=1)
ine.columns = ["codauto", "cpro", "cmun", "dc", "nombre"]
ine["municipio_ine"] = ine["cpro"].astype(str).str.zfill(2) + ine["cmun"].astype(str).str.zfill(3)
ine_lookup = ine.set_index("municipio_ine")["nombre"].to_dict()

# Run spatial joins
print("Assigning CCAA...")
ccaa_col  = spatial_assign(node_points, ccaa_polys, "ccaa")
print("Assigning provincia...")
prov_col  = spatial_assign(node_points, prov_polys, "provincia")
print("Assigning municipio...")
muni_col  = spatial_assign(node_points, muni_polys, "municipio_ine")

# Build lookup and merge into nodes
geo_lookup = pd.DataFrame({
    "ccaa": ccaa_col,
    "provincia": prov_col,
    "municipio": muni_col.map(ine_lookup),
}).reset_index()

# Flag any codes that didn't resolve to a name
unmapped = geo_lookup["municipio"].isna().sum()
if unmapped > 0:
    print(f"  ⚠ {unmapped} municipality codes not found in INE dictionary — keeping raw codes")
    geo_lookup.loc[geo_lookup["municipio"].isna(), "municipio"] = muni_col[geo_lookup["municipio"].isna()].values

nodes_raw = nodes_raw.merge(geo_lookup, on="node_id", how="left")

# Pivot section → columns (use pivot, not pivot_table, to preserve rows where value_mw is NaN)
nodes = nodes_raw.pivot(
    index=[
        "node_id", "node_name", "ccaa", "provincia", "municipio",
        "voltage", "lat", "lng", "technology", "status", "grid",
    ],
    columns="section",
    values="value_mw",
).reset_index()

nodes.columns.name = None
nodes = nodes.rename(columns={
    "capacidad_acceso":    "capacidad_acceso_mw",
    "potencia_instalada":  "potencia_instalada_mw",
})
nodes = nodes.sort_values(
    ["ccaa", "provincia", "municipio", "node_id", "technology", "status", "grid"]
).reset_index(drop=True)


# =====================================================================
# 2. CCAA table
# =====================================================================
ccaa_raw = ccaa_raw.drop(columns=["zone", "node_id", "node_name", "voltage", "lat", "lng"])
ccaa_raw = ccaa_raw.rename(columns={"zone_detail": "ccaa"})

ccaa_agg = ccaa_raw.pivot(
    index=["ccaa", "technology", "status", "grid"],
    columns="section",
    values="value_mw",
).reset_index()

ccaa_agg.columns.name = None
ccaa_agg = ccaa_agg.rename(columns={
    "capacidad_acceso":   "capacidad_acceso_mw",
    "potencia_instalada": "potencia_instalada_mw",
})
ccaa_agg = ccaa_agg.sort_values(["ccaa", "technology", "status", "grid"]).reset_index(drop=True)


# =====================================================================
# 3. NATIONAL table
# =====================================================================
nat_raw = nat_raw.drop(columns=["node_id", "node_name", "voltage", "lat", "lng"])
nat_raw = nat_raw.rename(columns={"zone": "scope", "zone_detail": "scope_detail"})

national = nat_raw.pivot(
    index=["scope", "scope_detail", "technology", "status", "grid"],
    columns="section",
    values="value_mw",
).reset_index()

national.columns.name = None
national = national.rename(columns={
    "capacidad_acceso":   "capacidad_acceso_mw",
    "potencia_instalada": "potencia_instalada_mw",
})
national = national.sort_values(["scope", "technology", "status", "grid"]).reset_index(drop=True)


# =====================================================================
# Validation: node sums vs CCAA aggregates
# =====================================================================
print("\n--- Validation: node sums vs CCAA aggregates ---")
node_sums = nodes.groupby(["ccaa", "technology", "status", "grid"])[
    ["capacidad_acceso_mw", "potencia_instalada_mw"]
].sum().reset_index()

check = ccaa_agg.merge(
    node_sums, on=["ccaa", "technology", "status", "grid"],
    suffixes=("_ccaa", "_nodes"), how="outer",
)
for col in ["capacidad_acceso_mw", "potencia_instalada_mw"]:
    diff = (check[f"{col}_ccaa"].fillna(0) - check[f"{col}_nodes"].fillna(0)).abs()
    mismatches = diff[diff > 0.1]
    if len(mismatches) > 0:
        print(f"  {col}: {len(mismatches)} mismatches (max diff: {mismatches.max():.2f} MW)")
    else:
        print(f"  {col}: ✓ all match")


# =====================================================================
# Save
# =====================================================================
nodes.to_csv(OUTPUT_DIR / "ree_nodes.csv", index=False, encoding="utf-8-sig")
ccaa_agg.to_csv(OUTPUT_DIR / "ree_ccaa.csv", index=False, encoding="utf-8-sig")
national.to_csv(OUTPUT_DIR / "ree_national.csv", index=False, encoding="utf-8-sig")

print(f"\nnodes:    {nodes.shape[0]:>5} rows × {nodes.shape[1]} cols  → ree_nodes.csv")
print(f"ccaa:     {ccaa_agg.shape[0]:>5} rows × {ccaa_agg.shape[1]} cols  → ree_ccaa.csv")
print(f"national: {national.shape[0]:>5} rows × {national.shape[1]} cols  → ree_national.csv")
print(f"\nTech label lookup:\n{TECH_LABELS}")