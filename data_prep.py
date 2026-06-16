"""
data_prep.py
------------
Run during Render build: pip install -r requirements.txt && python data_prep.py
"""

import os
import io
import shutil
import json
import zipfile
import tempfile
import requests
import geopandas as gpd
from shapely.geometry import Polygon, Point
import pandas as pd

os.makedirs("static", exist_ok=True)

PSRD_COORDS = [
    (-94.50, 39.58),
    (-94.44, 39.58),
    (-94.44, 39.54),
    (-94.50, 39.54),
    (-94.50, 39.58),
]

STATE_FIPS  = "29"
COUNTY_FIPS = "049"


def download_zip_to_gdf(url):
    """Download a zip containing a shapefile, extract to temp dir, return GeoDataFrame."""
    print(f"  Downloading {url}...")
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    print(f"  Downloaded {len(r.content) / 1024:.0f} KB")
    tmpdir = tempfile.mkdtemp()
    try:
        z = zipfile.ZipFile(io.BytesIO(r.content))
        z.extractall(tmpdir)
        shp_files = [f for f in z.namelist() if f.endswith(".shp")]
        shp_path = os.path.join(tmpdir, shp_files[0])
        print(f"  Reading {shp_files[0]}...")
        gdf = gpd.read_file(shp_path)
        return gdf.to_crs("EPSG:4326")
    finally:
        shutil.rmtree(tmpdir)


def fetch_tiger_roads():
    print("Fetching Census TIGER roads for Clinton County MO (FIPS 29049)...")
    url = f"https://www2.census.gov/geo/tiger/TIGER2023/ROADS/tl_2023_{STATE_FIPS}{COUNTY_FIPS}_roads.zip"
    gdf = download_zip_to_gdf(url)
    print(f"  Loaded {len(gdf)} road segments")
    if "RTTYP" in gdf.columns:
        print(f"  RTTYP value counts:")
        print(gdf["RTTYP"].value_counts(dropna=False).to_string())
    return gdf


def fetch_municipal_boundaries():
    print("Fetching municipal boundaries...")
    url = f"https://www2.census.gov/geo/tiger/TIGER2023/PLACE/tl_2023_{STATE_FIPS}_place.zip"
    gdf = download_zip_to_gdf(url)

    # Load the Clinton County boundary polygon for accurate spatial filtering
    boundary_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clinton_boundary.geojson")
    if os.path.exists(boundary_path):
        county_gdf = gpd.read_file(boundary_path)
    else:
        # Fallback to tight bounding box matching actual Clinton County bounds
        county_gdf = gpd.GeoDataFrame(geometry=[Polygon([
            (-94.603, 39.455), (-94.134, 39.455),
            (-94.134, 39.748), (-94.603, 39.748),
            (-94.603, 39.455),
        ])], crs="EPSG:4326")

    # Filter to incorporated places only (exclude CDPs)
    if "CLASSFP" in gdf.columns:
        gdf = gdf[gdf["CLASSFP"].isin(["C1","C5","C6","C7"])]

    clinton_places = gpd.sjoin(
        gdf, county_gdf[["geometry"]], how="inner", predicate="intersects"
    ).drop(columns=["index_right"])

    print(f"  Found {len(clinton_places)} municipalities:")
    for name in sorted(clinton_places["NAME"].tolist()):
        print(f"    - {name}")
    return clinton_places


def build_psrd_boundary():
    print("Building PSRD boundary (placeholder)...")
    poly = Polygon(PSRD_COORDS)
    return gpd.GeoDataFrame(
        [{"NAME": "Plattsburg Special Road District"}],
        geometry=[poly], crs="EPSG:4326"
    )




def fetch_osm_road_names():
    """
    Query OpenStreetMap Overpass API for road names in Clinton County.
    Returns a dict mapping OSM way id -> {name, ref} and a GeoDataFrame
    of way centroids for spatial joining.
    """
    print("Fetching OSM road names from Overpass API...")

    query = """
[out:json][timeout:90];
way[highway](39.45,-94.61,39.75,-94.13);
out tags geom;
"""

    import urllib.parse as urlparse
    overpass_url = "https://overpass-api.de/api/interpreter"

    try:
        data = urlparse.urlencode({"data": query}).encode()
        req = requests.post(overpass_url, data={"data": query}, timeout=90,
                           headers={"User-Agent": "clinton-county-gis/1.0"})
        req.raise_for_status()
        result = req.json()
    except Exception as e:
        print(f"  WARNING: OSM fetch failed: {e}")
        print("  Road names will use TIGER data only")
        return None

    elements = result.get("elements", [])
    print(f"  Got {len(elements)} OSM ways")

    # Build lookup: osm_id -> {name, ref, centroid_lon, centroid_lat}
    rows = []
    for el in elements:
        tags = el.get("tags", {})
        name = tags.get("name", "")
        ref  = tags.get("ref", "")
        if not name and not ref:
            continue
        # Get centroid from geometry
        geom = el.get("geometry", [])
        if not geom:
            continue
        lons = [n["lon"] for n in geom if "lon" in n]
        lats = [n["lat"] for n in geom if "lat" in n]
        if not lons:
            continue
        cx = sum(lons) / len(lons)
        cy = sum(lats) / len(lats)
        rows.append({
            "osm_id": el["id"],
            "OSM_NAME": name,
            "OSM_REF":  ref,
            "geometry": Point(cx, cy)
        })

    if not rows:
        print("  No named OSM ways found")
        return None

    gdf = gpd.GeoDataFrame(rows, crs="EPSG:4326")
    print(f"  Built {len(gdf)} named OSM way centroids")
    return gdf


def enrich_with_osm_names(roads_gdf, osm_gdf):
    """
    Join OSM road names to TIGER roads by nearest centroid.
    Adds OSM_NAME and OSM_REF columns.
    """
    if osm_gdf is None or len(osm_gdf) == 0:
        roads_gdf["OSM_NAME"] = ""
        roads_gdf["OSM_REF"]  = ""
        return roads_gdf

    print("Enriching roads with OSM names...")

    roads = roads_gdf.to_crs("EPSG:3857").copy()
    osm   = osm_gdf.to_crs("EPSG:3857").copy()

    # Get road centroids
    road_centroids = roads[["geometry"]].copy()
    road_centroids["geometry"] = roads.geometry.centroid

    # Nearest join — each road gets the closest OSM way name
    joined = gpd.sjoin_nearest(
        road_centroids,
        osm[["geometry", "OSM_NAME", "OSM_REF"]],
        how="left",
        distance_col="osm_dist"
    )

    # Only keep matches within 500 meters to avoid wrong-road assignments
    joined.loc[joined["osm_dist"] > 500, ["OSM_NAME", "OSM_REF"]] = ""

    roads_gdf = roads_gdf.copy()
    roads_gdf["OSM_NAME"] = joined["OSM_NAME"].fillna("").values
    roads_gdf["OSM_REF"]  = joined["OSM_REF"].fillna("").values

    named = (roads_gdf["OSM_NAME"] != "") | (roads_gdf["OSM_REF"] != "")
    print(f"  Enriched {named.sum()} of {len(roads_gdf)} roads with OSM names")
    return roads_gdf

def classify_roads(roads_gdf, municipal_gdf):
    """
    Classification rules (priority order):
      1. RTTYP I/U/S or MTFCC S1100  -> MoDOT  (state route designation)
      2. Centroid inside city limits  -> Municipal
      3. Everything else              -> County Commission

    IMPORTANT: RTTYP=M in TIGER means "municipal route number system" (a
    numbering convention), NOT "maintained by a municipality."  Many rural
    county roads carry RTTYP=M simply because they follow a municipal route
    number.  Spatial containment within incorporated city limits is the only
    reliable signal for municipal maintenance responsibility.
    """
    print("Classifying road segments...")

    roads_proj = roads_gdf.to_crs("EPSG:3857").copy()
    munis_proj = municipal_gdf.to_crs("EPSG:3857")

    rttyp = roads_gdf["RTTYP"].fillna("") if "RTTYP" in roads_gdf.columns else pd.Series("", index=roads_gdf.index)
    mtfcc = roads_gdf["MTFCC"].fillna("") if "MTFCC" in roads_gdf.columns else pd.Series("", index=roads_gdf.index)

    # Spatial join: which road centroids fall inside a city boundary?
    centroids = roads_proj[["geometry"]].copy()
    centroids["geometry"] = roads_proj.geometry.centroid

    in_muni = gpd.sjoin(
        centroids,
        munis_proj[["geometry"]],
        how="left",
        predicate="within"
    )
    inside_city = pd.Series(False, index=roads_gdf.index)
    inside_city[in_muni[in_muni["index_right"].notna()].index] = True

    # Apply priority rules
    jurisdiction = pd.Series("County", index=roads_gdf.index)
    jurisdiction[rttyp.isin(["I", "U", "S"]) | mtfcc.isin(["S1100"])] = "MoDOT"
    jurisdiction[inside_city & (jurisdiction != "MoDOT")] = "Municipal"

    roads_out = roads_gdf.copy()
    roads_out["JURISDICTION"] = jurisdiction

    print("  Results:")
    for k, v in jurisdiction.value_counts().items():
        print(f"    {k}: {v} segments")
    return roads_out


def round_coords(geojson_dict, precision=5):
    def round_ring(ring):
        return [[round(c[0], precision), round(c[1], precision)] for c in ring]
    for feature in geojson_dict.get("features", []):
        geom = feature.get("geometry", {})
        gtype = geom.get("type", "")
        coords = geom.get("coordinates", [])
        if gtype == "LineString":
            geom["coordinates"] = round_ring(coords)
        elif gtype == "MultiLineString":
            geom["coordinates"] = [round_ring(r) for r in coords]
        elif gtype == "Polygon":
            geom["coordinates"] = [round_ring(r) for r in coords]
        elif gtype == "MultiPolygon":
            geom["coordinates"] = [[round_ring(r) for r in poly] for poly in coords]
    return geojson_dict


def write_geojson(gdf, path, keep_cols=None):
    if keep_cols:
        cols = [c for c in keep_cols if c in gdf.columns] + ["geometry"]
        gdf = gdf[cols]
    tmp = path + ".tmp"
    gdf.to_file(tmp, driver="GeoJSON")
    with open(tmp) as f:
        data = json.load(f)
    os.remove(tmp)
    data = round_coords(data, precision=5)
    with open(path, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    kb = os.path.getsize(path) / 1024
    print(f"  Wrote {path} ({len(data.get('features', []))} features, {kb:.0f} KB)")


if __name__ == "__main__":
    roads_raw = fetch_tiger_roads()
    munis     = fetch_municipal_boundaries()
    psrd      = build_psrd_boundary()
    roads     = classify_roads(roads_raw, munis)

    osm_gdf = fetch_osm_road_names()
    roads   = enrich_with_osm_names(roads, osm_gdf)

    keep = ["JURISDICTION", "FULLNAME", "MTFCC", "RTTYP", "OSM_NAME", "OSM_REF"]

    print("\nWriting GeoJSON files...")
    os.makedirs("static/Roads", exist_ok=True)
    write_geojson(roads[roads["JURISDICTION"] == "MoDOT"],     "static/Roads/roads_modot.geojson",    keep)
    write_geojson(roads[roads["JURISDICTION"] == "PSRD"],      "static/Roads/roads_psrd.geojson",      keep)
    write_geojson(roads[roads["JURISDICTION"] == "Municipal"], "static/Roads/roads_municipal.geojson", keep)
    write_geojson(roads[roads["JURISDICTION"] == "County"],    "static/Roads/roads_county.geojson",    keep)
    write_geojson(munis, "static/Roads/boundaries_municipal.geojson", ["NAME", "GEOID"])
    write_geojson(psrd,  "static/Roads/boundary_psrd.geojson",        ["NAME"])

    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clinton_boundary.geojson")
    dst = os.path.join("static", "clinton_boundary.geojson")
    if os.path.exists(src):
        shutil.copy(src, dst)
        print("  Copied clinton_boundary.geojson to static/")
    else:
        print("  WARNING: clinton_boundary.geojson not found in repo root")

    print("\nDone.")
