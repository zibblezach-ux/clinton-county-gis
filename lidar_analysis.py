#!/usr/bin/env python3
"""
Clinton County LiDAR Road Quality Analysis Pipeline
====================================================
Queries the MSDIS ArcGIS ImageServer REST API to pull elevation
data for each county road corridor, then scores it for quality.

No file downloads needed. Run this on the same machine as your Flask app.

Usage:
    python lidar_analysis.py

Requirements:
    pip install requests numpy

Output:
    static/Roads/roads_quality.geojson  (overwrites synthetic data with real scores)
"""

import json, math, time, os, sys
import numpy as np
import requests

# ── Service configuration ─────────────────────────────────────────────────────
# Primary: lidar.msdis.missouri.edu (the URL you found)
# Fallback: moimagery.missouri.edu (same data, different host)
IMAGESERVER_URLS = [
    "https://lidar.msdis.missouri.edu/arcgis/rest/services/MO_FEMANRCS_2020_D20_DEM_UTM/ImageServer",
    "https://lidar.msdis.missouri.edu/arcgis/rest/services/MO_NorthernSEMO_2021_D21_DEM_UTM/ImageServer",
    "https://lidar.msdis.missouri.edu/arcgis/rest/services/MO_WestCentral_2018_D19_DEM_UTM/ImageServer",
]
SERVICE_SR  = 26915        # UTM Zone 15N — the CRS of the ImageServer
ROADS_PATH  = os.path.join(os.path.dirname(__file__), "static/Roads/roads_county.geojson")
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "static/Roads/roads_quality.geojson")

# ── Analysis parameters ───────────────────────────────────────────────────────
CORRIDOR_M     = 12        # metres each side of road centerline to sample
SAMPLE_SPACING = 10        # metres between sample points along road
PIXEL_SIZE     = 2         # request 2m pixels (1m native but 2m reduces API load)
MAX_PIXELS     = 200       # cap pixels per request to stay within service limits
REQUEST_DELAY  = 0.15      # seconds between API calls (be polite to the server)
MIN_PIXELS     = 3         # minimum pixels needed for a valid score

# IRI thresholds for centerline-detrended roughness — recalibrated after each test run
IRI_THRESHOLDS = [
    (0,      2,     5),   # excellent
    (2,      8,     4),   # good
    (8,      25,    3),   # fair
    (25,     70,    2),   # poor
    (70,     999999, 1),  # failed
]
QUALITY_COLORS = {5:'green', 4:'green', 3:'yellow', 2:'red', 1:'red'}
QUALITY_LABELS = {5:'Good',  4:'Good',  3:'Fair',   2:'Poor', 1:'Poor'}
PASER_DESC     = {5:'Excellent', 4:'Good', 3:'Fair', 2:'Poor', 1:'Failed'}


# ── Coordinate conversion: WGS84 → UTM15N ────────────────────────────────────

def wgs84_to_utm15n(lon, lat):
    """
    Approximate WGS84 decimal degrees → UTM Zone 15N (EPSG:26915) in metres.
    Accurate enough for Missouri road analysis without requiring pyproj.
    """
    # Constants
    a  = 6378137.0          # WGS84 semi-major axis
    f  = 1 / 298.257223563
    b  = a * (1 - f)
    e2 = 1 - (b/a)**2
    e  = math.sqrt(e2)
    k0 = 0.9996
    E0 = 500000.0           # false easting
    N0 = 0.0                # false northing (northern hemisphere)
    lon0 = math.radians(-93.0)  # Zone 15N central meridian

    lat_r = math.radians(lat)
    lon_r = math.radians(lon)

    N  = a / math.sqrt(1 - e2 * math.sin(lat_r)**2)
    T  = math.tan(lat_r)**2
    C  = e2 / (1 - e2) * math.cos(lat_r)**2
    A  = math.cos(lat_r) * (lon_r - lon0)

    e2_p = e2 / (1 - e2)
    M = a * (
        (1 - e2/4 - 3*e2**2/64 - 5*e2**3/256) * lat_r
        - (3*e2/8 + 3*e2**2/32 + 45*e2**3/1024) * math.sin(2*lat_r)
        + (15*e2**2/256 + 45*e2**3/1024) * math.sin(4*lat_r)
        - (35*e2**3/3072) * math.sin(6*lat_r)
    )

    easting = E0 + k0 * N * (
        A + (1-T+C)*A**3/6
        + (5 - 18*T + T**2 + 72*C - 58*e2_p)*A**5/120
    )
    northing = N0 + k0 * (
        M + N * math.tan(lat_r) * (
            A**2/2
            + (5 - T + 9*C + 4*C**2)*A**4/24
            + (61 - 58*T + T**2 + 600*C - 330*e2_p)*A**6/720
        )
    )
    return easting, northing


# ── Road geometry ─────────────────────────────────────────────────────────────

def get_coords(feature):
    g = feature['geometry']
    if g['type'] == 'LineString':
        return g['coordinates']
    elif g['type'] == 'MultiLineString':
        coords = []
        for line in g['coordinates']:
            coords.extend(line)
        return coords
    return []

def road_length_m(coords):
    total = 0
    for i in range(len(coords)-1):
        ex, ey = wgs84_to_utm15n(*coords[i])
        fx, fy = wgs84_to_utm15n(*coords[i+1])
        total += math.sqrt((fx-ex)**2 + (fy-ey)**2)
    return total

def road_corridor_bbox_utm(coords):
    """Return UTM bbox (minx,miny,maxx,maxy) with corridor buffer."""
    utm_pts = [wgs84_to_utm15n(c[0], c[1]) for c in coords]
    xs = [p[0] for p in utm_pts]
    ys = [p[1] for p in utm_pts]
    buf = CORRIDOR_M + 5
    return (min(xs)-buf, min(ys)-buf, max(xs)+buf, max(ys)+buf)


# ── ImageServer query ─────────────────────────────────────────────────────────

_active_urls = None

def get_service_urls(session):
    """Return all working ImageServer URLs."""
    global _active_urls
    if _active_urls is not None:
        return _active_urls
    _active_urls = []
    for url in IMAGESERVER_URLS:
        try:
            r = session.get(url, params={'f':'json'}, timeout=10)
            if r.status_code == 200 and 'currentVersion' in r.text:
                _active_urls.append(url)
                print(f"  Found service: {url.split('/')[-2]}")
        except Exception:
            continue
    if not _active_urls:
        raise RuntimeError("Could not reach any MSDIS ImageServer.")
    return _active_urls

def fetch_elevation_raster(session, service_url, bbox_utm, width, height):
    """
    Call exportImage to get a GeoTIFF elevation raster for a bounding box.
    Returns numpy array of elevation values (metres), or None on failure.
    """
    minx, miny, maxx, maxy = bbox_utm
    params = {
        'bbox':      f"{minx},{miny},{maxx},{maxy}",
        'bboxSR':    SERVICE_SR,
        'size':      f"{width},{height}",
        'imageSR':   SERVICE_SR,
        'format':    'tiff',
        'pixelType': 'F32',
        'noData':    '-9999',
        'f':         'image',
    }
    try:
        r = session.get(f"{service_url}/exportImage", params=params, timeout=30)
        if r.status_code != 200 or len(r.content) < 100:
            return None
        # Parse GeoTIFF using numpy (requires rasterio or gdal)
        # Fallback: use getSamples if rasterio not available
        try:
            import rasterio
            from io import BytesIO
            with rasterio.open(BytesIO(r.content)) as src:
                arr = src.read(1).astype(float)
                arr[arr < -9000] = np.nan  # nodata
                return arr
        except ImportError:
            # rasterio not available — use getSamples instead
            return None
    except Exception as e:
        print(f"    exportImage error: {e}")
        return None

def fetch_elevation_samples(session, service_urls, coords_utm):
    """
    Query multiple services and merge results.
    For each point, use the first service that returns a valid elevation.
    """
    n = len(coords_utm)
    results = [None] * n

    for service_url in service_urls:
        # Find indices still missing data
        missing_idx = [i for i, v in enumerate(results) if v is None]
        if not missing_idx:
            break

        missing_pts = [coords_utm[i] for i in missing_idx]
        batch_size = 200

        fetched = []
        for b in range(0, len(missing_pts), batch_size):
            batch = missing_pts[b:b+batch_size]
            post_data = {
                'geometry':     json.dumps({"points": [[round(p[0],2), round(p[1],2)] for p in batch]}),
                'geometryType': 'esriGeometryMultipoint',
                'inSR':         str(SERVICE_SR),
                'outFields':    '*',
                'returnFirstValueOnly': 'false',
                'f':            'json',
            }
            try:
                r = session.post(f"{service_url}/getSamples", data=post_data, timeout=30)
                if r.status_code != 200:
                    fetched.extend([None] * len(batch))
                    continue
                data = r.json()
                samples = data.get('samples', [])
                for s in samples:
                    val = s.get('value')
                    try:
                        fval = float(val)
                        fetched.append(None if fval < -9000 else fval)
                    except (TypeError, ValueError):
                        fetched.append(None)
                while len(fetched) < b + len(batch):
                    fetched.append(None)
            except Exception as e:
                fetched.extend([None] * len(batch))
            time.sleep(REQUEST_DELAY)

        # Merge fetched values back into results
        for i, idx in enumerate(missing_idx):
            if i < len(fetched) and fetched[i] is not None:
                results[idx] = fetched[i]

    return results


# ── Sample point generation ───────────────────────────────────────────────────

def generate_sample_points(coords, spacing_m, corridor_m):
    """
    Generate a grid of sample points covering the road corridor.
    Points spaced every spacing_m along the road, corridor_m wide each side.
    Returns list of (utm_x, utm_y) tuples.
    """
    points = []
    utm_coords = [wgs84_to_utm15n(c[0], c[1]) for c in coords]

    for i in range(len(utm_coords)-1):
        x1, y1 = utm_coords[i]
        x2, y2 = utm_coords[i+1]
        seg_len = math.sqrt((x2-x1)**2 + (y2-y1)**2)
        if seg_len == 0:
            continue

        # Unit vector along road and perpendicular
        dx, dy   = (x2-x1)/seg_len, (y2-y1)/seg_len
        px, py   = -dy, dx  # perpendicular

        # Sample points along segment
        n_along = max(1, int(seg_len / spacing_m))
        for j in range(n_along):
            t  = (j + 0.5) / n_along
            cx = x1 + t * (x2 - x1)
            cy = y1 + t * (y2 - y1)
            # Sample across corridor (-corridor_m, 0, +corridor_m)
            for offset in [-corridor_m, 0, corridor_m]:
                points.append((cx + px*offset, cy + py*offset))

    return points


# ── Scoring ───────────────────────────────────────────────────────────────────

def compute_cross_slope(elevations_grid, corridor_m):
    """Estimate cross slope % from left/center/right elevation triples."""
    slopes = []
    for i in range(0, len(elevations_grid)-2, 3):
        left   = elevations_grid[i]
        center = elevations_grid[i+1]
        right  = elevations_grid[i+2]
        valid = [v for v in [left, center, right] if v is not None]
        if len(valid) >= 2:
            elev_range = max(valid) - min(valid)
            slope_pct  = (elev_range / (corridor_m * 2)) * 100
            slopes.append(slope_pct)
    return round(float(np.mean(slopes)), 2) if slopes else None

def score_from_cross_slope(cross_slope_pct):
    """
    Score road drainage based on cross slope percentage.
    Ideal gravel road cross slope is 2-4% for proper water shedding.
    Too flat = water pools. Too steep = gravel washes off.
    Returns PASER 1-5.
    """
    if cross_slope_pct is None:
        return 3
    if cross_slope_pct < 0.5:
        return 1   # completely flat — water pools, road degrades fast
    elif cross_slope_pct < 1.5:
        return 2   # inadequate drainage
    elif cross_slope_pct <= 4.0:
        return 5   # ideal range
    elif cross_slope_pct <= 5.5:
        return 4   # slightly steep but acceptable
    elif cross_slope_pct <= 7.0:
        return 3   # too steep, gravel loss likely
    else:
        return 2   # severely steep, erosion likely

def score_from_crown(elevations_grid):
    """
    Score road crown shape from left/center/right elevation triples.
    A well-crowned road has center higher than both edges.
    Crown height of 3-8cm per meter of half-width is ideal for gravel.
    Returns PASER 1-5.
    """
    crown_scores = []
    for i in range(0, len(elevations_grid)-2, 3):
        left   = elevations_grid[i]
        center = elevations_grid[i+1]
        right  = elevations_grid[i+2]
        if None in (left, center, right):
            continue
        # Crown = center elevation relative to average of edges
        edge_avg = (left + right) / 2
        crown_height = center - edge_avg  # positive = crowned, negative = rutted/inverted
        crown_scores.append(crown_height)

    if not crown_scores:
        return 3

    avg_crown = float(np.mean(crown_scores))
    crown_var = float(np.var(crown_scores))  # consistency of crown along road

    # Score based on crown height and consistency
    if avg_crown > 0.15 and crown_var < 0.05:
        return 5   # well-crowned, consistent
    elif avg_crown > 0.08 and crown_var < 0.15:
        return 4   # good crown
    elif avg_crown > 0.02:
        return 3   # some crown present
    elif avg_crown > -0.05:
        return 2   # flat or nearly flat
    else:
        return 1   # inverted crown — water channels to center

def combined_paser(cross_slope_score, crown_score):
    """Combine cross slope and crown scores, weighted toward drainage."""
    weighted = (cross_slope_score * 0.6) + (crown_score * 0.4)
    return max(1, min(5, round(weighted)))


# ── Main pipeline ─────────────────────────────────────────────────────────────

def score_road(feature, session, service_urls):
    """Score one road segment. Returns dict of quality properties."""
    coords = get_coords(feature)
    if len(coords) < 2:
        return None

    length_m   = road_length_m(coords)
    sample_pts = generate_sample_points(coords, SAMPLE_SPACING, CORRIDOR_M)

    # For very short segments generate at least 3 points at start/mid/end
    if len(sample_pts) < MIN_PIXELS:
        utm = [wgs84_to_utm15n(c[0], c[1]) for c in coords]
        mid_idx = len(utm) // 2
        for base_x, base_y in [utm[0], utm[mid_idx], utm[-1]]:
            sample_pts.append((base_x, base_y))
            sample_pts.append((base_x + CORRIDOR_M, base_y))
            sample_pts.append((base_x - CORRIDOR_M, base_y))

    if len(sample_pts) < MIN_PIXELS:
        return None

    # Cap samples for very long roads
    if len(sample_pts) > MAX_PIXELS * 3:
        step = len(sample_pts) // (MAX_PIXELS * 3)
        sample_pts = sample_pts[::step]

    elevations = fetch_elevation_samples(session, service_urls, sample_pts)

    valid_elevs = [e for e in elevations if e is not None]
    if len(valid_elevs) < MIN_PIXELS:
        return None

    # Compute cross slope from left/center/right triples
    cross_sl       = compute_cross_slope(elevations, CORRIDOR_M)
    cross_score    = score_from_cross_slope(cross_sl)
    crown_score    = score_from_crown(elevations)
    paser          = combined_paser(cross_score, crown_score)
    drainage       = cross_score  # cross slope IS the drainage indicator

    # IRI kept as secondary display metric using centerline variance
    center_elevs = [valid_elevs[i] for i in range(1, len(valid_elevs), 3)]
    if len(center_elevs) >= 4:
        zc = np.array(center_elevs)
        xs_idx = np.arange(len(zc), dtype=float)
        s, b = np.polyfit(xs_idx, zc, 1)
        residuals = zc - (s * xs_idx + b)
        iri = round(float(np.var(residuals)), 4)
    else:
        iri = None

    return {
        'paser':           paser,
        'quality_color':   QUALITY_COLORS[paser],
        'quality_label':   QUALITY_LABELS[paser],
        'paser_desc':      PASER_DESC[paser],
        'iri':             iri,
        'cross_slope_pct': cross_sl,
        'drainage_score':  drainage,
        'length_m':        round(length_m),
        'lidar_points':    len(valid_elevs),
        'data_source':     'LiDAR DEM — MSDIS multi-source 2020/2021 (1m QL2)',
        'last_updated':    '2026',
    }


def run(max_roads=None):
    print("Clinton County LiDAR Road Quality Analysis", flush=True)
    print("=" * 50, flush=True)

    print(f"Loading roads: {ROADS_PATH}", flush=True)
    with open(ROADS_PATH) as f:
        roads = json.load(f)
    total = len(roads['features'])
    if max_roads:
        roads['features'] = roads['features'][:max_roads]
        print(f"TEST MODE: processing {max_roads} of {total} segments", flush=True)
    else:
        print(f"Road segments to score: {total}", flush=True)

    session = requests.Session()
    session.headers.update({'User-Agent': 'ClintonCountyGIS/1.0 (road quality analysis)'})

    print("Connecting to MSDIS ImageServers...", flush=True)
    try:
        service_urls = get_service_urls(session)
        print(f"Connected to {len(service_urls)} services", flush=True)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    scored   = 0
    skipped  = 0
    start    = time.time()

    for i, feature in enumerate(roads['features']):
        name = feature['properties'].get('FULLNAME', f'Segment {i+1}')
        if i % 50 == 0:
            elapsed = time.time() - start
            eta     = (elapsed / max(i,1)) * (total - i) / 60
            print(f"  [{i+1}/{total}] {name[:40]}  (ETA: {eta:.0f} min)", flush=True)

        result = score_road(feature, session, service_urls)
        if result:
            feature['properties'].update(result)
            scored += 1
        else:
            length_m = road_length_m(get_coords(feature)) if get_coords(feature) else 0
            feature['properties'].update({
                'quality_color': 'yellow',
                'quality_label': 'Unscored',
                'paser':         None,
                'iri':           None,
                'length_m':      round(length_m),
                'data_source':   'LiDAR — insufficient coverage for this segment',
                'last_updated':  '2026',
            })
            skipped += 1

        time.sleep(REQUEST_DELAY)

    elapsed_min = (time.time() - start) / 60
    print(f"\nCompleted in {elapsed_min:.1f} minutes", flush=True)
    print(f"  Scored:   {scored}")
    print(f"  Skipped:  {skipped}")

    colors = [f['properties'].get('quality_color','yellow') for f in roads['features']]
    green  = colors.count('green')
    yellow = colors.count('yellow')
    red    = colors.count('red')
    total_m = sum(f['properties'].get('length_m',0) for f in roads['features'])
    print(f"\n  Green  (Good): {green} segments")
    print(f"  Yellow (Fair): {yellow} segments")
    print(f"  Red    (Poor): {red} segments")
    print(f"  Total: {total_m/1609:.0f} miles")

    print(f"\nSaving: {OUTPUT_PATH}", flush=True)
    with open(OUTPUT_PATH, 'w') as f:
        json.dump(roads, f)
    print("Done. Restart Flask and open /roads to see real quality data.")


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Clinton County LiDAR Road Quality Analysis')
    parser.add_argument('--max', type=int, default=None, help='Limit to N roads for testing (e.g. --max 20)')
    args = parser.parse_args()
    run(max_roads=args.max)
