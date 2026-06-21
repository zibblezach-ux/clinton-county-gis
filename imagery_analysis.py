#!/usr/bin/env python3
"""
Clinton County Aerial Imagery Road Quality Analysis
====================================================
Uses the 2023/2024 MSDIS 6-inch aerial imagery to visually assess
gravel road surface conditions via the Anthropic Claude vision API.

For each county road segment:
  1. Fetches a 6-inch resolution aerial image of the road corridor
     from the MSDIS ImageServer (RGB + near-infrared)
  2. Sends the image to Claude with a structured road assessment prompt
  3. Claude scores: surface condition, drainage, visible distress, crown
  4. Scores are mapped to PASER ratings and written to roads_quality_imagery.geojson

The imagery layer complements the LiDAR layer:
  - LiDAR: structural/drainage (2018 data, slow to change)
  - Imagery: current surface condition (2023 data, catches recent deterioration)

Usage:
    pip install requests anthropic Pillow
    export ANTHROPIC_API_KEY=your_key_here
    python imagery_analysis.py

    # Test on first 20 roads only:
    python imagery_analysis.py --max 20

Output:
    static/Roads/roads_quality_imagery.geojson
"""

import os, sys, json, math, time, base64, argparse
from io import BytesIO
import requests

try:
    import anthropic
except ImportError:
    print("ERROR: anthropic package not installed. Run: pip install anthropic")
    sys.exit(1)

try:
    from PIL import Image
except ImportError:
    print("ERROR: Pillow not installed. Run: pip install Pillow")
    sys.exit(1)

# ── Configuration ─────────────────────────────────────────────────────────────
IMAGERY_SERVICE = "https://stateimagery.msdis.missouri.edu/arcgis/rest/services/Missouri_6inch_Statewide_2023_2024_Dynamic/ImageServer"
IMAGERY_SR      = 26915      # UTM Zone 15N
ROADS_PATH      = os.path.join(os.path.dirname(__file__), "static/Roads/roads_county.geojson")
OUTPUT_PATH     = os.path.join(os.path.dirname(__file__), "static/Roads/roads_quality_imagery.geojson")

CORRIDOR_M      = 20         # metres each side of centerline to include
IMAGE_WIDTH     = 200        # pixels wide for export (keep small for API speed)
IMAGE_HEIGHT    = 200        # pixels tall
REQUEST_DELAY   = 0.2        # seconds between API calls
CLAUDE_MODEL    = "claude-sonnet-4-6"

QUALITY_COLORS = {5:'green', 4:'green', 3:'yellow', 2:'red', 1:'red'}
QUALITY_LABELS = {5:'Good',  4:'Good',  3:'Fair',   2:'Poor', 1:'Poor'}

# ── Coordinate helpers ────────────────────────────────────────────────────────

def wgs84_to_utm15n(lon, lat):
    a=6378137.0; f=1/298.257223563; b=a*(1-f); e2=1-(b/a)**2
    k0=0.9996; E0=500000.0; lon0=math.radians(-93.0)
    lat_r=math.radians(lat); lon_r=math.radians(lon)
    N=a/math.sqrt(1-e2*math.sin(lat_r)**2)
    T=math.tan(lat_r)**2; C=e2/(1-e2)*math.cos(lat_r)**2
    A=math.cos(lat_r)*(lon_r-lon0); e2p=e2/(1-e2)
    M=a*((1-e2/4-3*e2**2/64-5*e2**3/256)*lat_r
        -(3*e2/8+3*e2**2/32+45*e2**3/1024)*math.sin(2*lat_r)
        +(15*e2**2/256+45*e2**3/1024)*math.sin(4*lat_r)
        -(35*e2**3/3072)*math.sin(6*lat_r))
    E=E0+k0*N*(A+(1-T+C)*A**3/6+(5-18*T+T**2+72*C-58*e2p)*A**5/120)
    Nv=k0*(M+N*math.tan(lat_r)*(A**2/2+(5-T+9*C+4*C**2)*A**4/24
         +(61-58*T+T**2+600*C-330*e2p)*A**6/720))
    return E, Nv

def get_coords(feature):
    g = feature['geometry']
    if g['type'] == 'LineString':
        return g['coordinates']
    elif g['type'] == 'MultiLineString':
        c = []
        for line in g['coordinates']: c.extend(line)
        return c
    return []

def road_bbox_utm(coords, buffer_m=None):
    buf = buffer_m or CORRIDOR_M + 5
    pts = [wgs84_to_utm15n(c[0], c[1]) for c in coords]
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    return min(xs)-buf, min(ys)-buf, max(xs)+buf, max(ys)+buf

def road_length_m(coords):
    total = 0
    utm = [wgs84_to_utm15n(c[0], c[1]) for c in coords]
    for i in range(len(utm)-1):
        dx = utm[i+1][0]-utm[i][0]; dy = utm[i+1][1]-utm[i][1]
        total += math.sqrt(dx*dx+dy*dy)
    return total


# ── Imagery fetch ─────────────────────────────────────────────────────────────

def fetch_road_image(session, coords):
    """
    Fetch a 6-inch aerial RGB image of the road corridor.
    Returns base64-encoded JPEG string, or None on failure.
    """
    xmin, ymin, xmax, ymax = road_bbox_utm(coords)
    bbox_w = xmax - xmin
    bbox_h = ymax - ymin

    # Maintain aspect ratio in exported image
    aspect = bbox_w / max(bbox_h, 1)
    if aspect > 1:
        w = IMAGE_WIDTH
        h = max(50, int(IMAGE_WIDTH / aspect))
    else:
        h = IMAGE_HEIGHT
        w = max(50, int(IMAGE_HEIGHT * aspect))

    # Request RGB only (bands 1,2,3) — NIR is band 4
    params = {
        'bbox':         f"{xmin},{ymin},{xmax},{ymax}",
        'bboxSR':       IMAGERY_SR,
        'size':         f"{w},{h}",
        'imageSR':      IMAGERY_SR,
        'format':       'jpg',
        'pixelType':    'U8',
        'renderingRule': json.dumps({"rasterFunction": "None"}),
        'f':            'image',
    }
    try:
        r = session.get(f"{IMAGERY_SERVICE}/exportImage", params=params, timeout=30)
        if r.status_code != 200 or len(r.content) < 1000:
            return None, None
        # Verify it's actually an image
        img = Image.open(BytesIO(r.content))
        if img.size[0] < 10 or img.size[1] < 10:
            return None, None
        # Convert to JPEG and base64 encode
        buf = BytesIO()
        img.convert('RGB').save(buf, format='JPEG', quality=85)
        b64 = base64.standard_b64encode(buf.getvalue()).decode('utf-8')
        return b64, f"{w}x{h}"
    except Exception as e:
        print(f"    Image fetch error: {e}")
        return None, None


# ── Claude vision assessment ──────────────────────────────────────────────────

ASSESSMENT_PROMPT = """You are a county road engineer inspecting Missouri gravel roads from aerial photography.

Analyze this 6-inch resolution aerial image of a rural gravel road corridor and provide a structured assessment.

Look for these specific indicators:

SURFACE CONDITION:
- Washboarding (parallel ridges across road)
- Rutting (parallel tracks from vehicle wheels)
- Potholes or erosion holes
- Gravel loss (road appears dark/bare soil visible)
- Fresh grading (uniform light-colored gravel surface)
- Loose or excess gravel (very light colored)

DRAINAGE:
- Crown visible (road higher in center than edges)
- Standing water or wet dark areas
- Erosion channels or gullies at road edge
- Ditch condition visible (water draining away from road)

OVERALL ROAD WIDTH:
- Full width passable
- Narrowing from vegetation encroachment
- Shoulders eroded or washed away

Respond ONLY with a JSON object in this exact format (no other text):
{
  "paser": <integer 1-5>,
  "surface_condition": "<one of: excellent, good, fair, poor, failed>",
  "drainage": "<one of: excellent, good, fair, poor>",
  "visible_distress": ["<list of observed issues, e.g. rutting, washboarding, gravel_loss, erosion, potholes, standing_water, crown_loss>"],
  "confidence": "<one of: high, medium, low>",
  "notes": "<one sentence describing what you see>"
}

PASER scale for gravel roads:
5 = Excellent: fresh gravel, good crown, clear drainage, no distress
4 = Good: good crown and drainage, minor loose aggregate or slight washboarding  
3 = Fair: traffic effects visible, needs routine maintenance, moderate washboarding or rutting
2 = Poor: significant deformation, poor drainage, major ruts or erosion
1 = Failed: travel difficult, road barely passable, severe erosion or complete surface failure"""


def assess_image(claude_client, image_b64, road_name):
    """Send image to Claude for road condition assessment."""
    try:
        response = claude_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": image_b64,
                        }
                    },
                    {
                        "type": "text",
                        "text": ASSESSMENT_PROMPT
                    }
                ]
            }]
        )
        raw = response.content[0].text.strip()
        # Strip any markdown fences if present
        if raw.startswith('```'):
            raw = raw.split('```')[1]
            if raw.startswith('json'): raw = raw[4:]
        return json.loads(raw.strip())
    except json.JSONDecodeError as e:
        print(f"    JSON parse error for {road_name}: {e}")
        return None
    except Exception as e:
        print(f"    Claude API error for {road_name}: {e}")
        return None


# ── Main pipeline ─────────────────────────────────────────────────────────────

def score_road(feature, session, claude_client):
    """Score one road segment via imagery. Returns quality dict or None."""
    coords = get_coords(feature)
    if len(coords) < 2:
        return None

    name = feature['properties'].get('FULLNAME', 'Unnamed')
    length_m = road_length_m(coords)

    image_b64, img_size = fetch_road_image(session, coords)
    if not image_b64:
        return None

    assessment = assess_image(claude_client, image_b64, name)
    if not assessment:
        return None

    paser = max(1, min(5, int(assessment.get('paser', 3))))

    return {
        'paser_imagery':        paser,
        'quality_color':        QUALITY_COLORS[paser],
        'quality_label':        QUALITY_LABELS[paser],
        'surface_condition':    assessment.get('surface_condition', 'unknown'),
        'drainage_imagery':     assessment.get('drainage', 'unknown'),
        'visible_distress':     ', '.join(assessment.get('visible_distress', [])),
        'ai_confidence':        assessment.get('confidence', 'unknown'),
        'ai_notes':             assessment.get('notes', ''),
        'image_size':           img_size or '',
        'length_m':             round(length_m),
        'data_source':          'Aerial imagery 2023 — MSDIS 6-inch (AI-assessed)',
        'last_updated':         '2026',
    }


def run(max_roads=None):
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        print("ERROR: Set ANTHROPIC_API_KEY environment variable")
        print("  export ANTHROPIC_API_KEY=your_key_here")
        sys.exit(1)

    print("Clinton County Aerial Imagery Road Quality Analysis")
    print("=" * 52)
    print(f"Imagery: MSDIS 6-inch 2023 statewide")
    print(f"AI model: {CLAUDE_MODEL}")

    print(f"\nLoading roads: {ROADS_PATH}")
    with open(ROADS_PATH) as f:
        roads = json.load(f)

    total = len(roads['features'])
    # Filter to county local roads only — skip private roads (S1740,S1750), ramps (S1630)
    roads['features'] = [f for f in roads['features']
                         if f['properties'].get('MTFCC') in ('S1400', 'S1500')]
    print(f"Filtered to {len(roads['features'])} county road segments (skipped {total - len(roads['features'])} private/other)", flush=True)
    if max_roads:
        roads['features'] = roads['features'][:max_roads]
        print(f"TEST MODE: processing {len(roads['features'])} segments", flush=True)
    else:
        print(f"Processing all {len(roads['features'])} segments", flush=True)

    session = requests.Session()
    session.headers.update({'User-Agent': 'ClintonCountyGIS/1.0'})
    claude_client = anthropic.Anthropic(api_key=api_key)

    scored = 0
    skipped = 0
    start = time.time()

    # Load existing scores to enable resume after interruption
    existing_scores = {}
    if os.path.exists(OUTPUT_PATH):
        try:
            with open(OUTPUT_PATH) as f:
                existing = json.load(f)
            for feat in existing['features']:
                lid = feat['properties'].get('LINEARID')
                if lid and feat['properties'].get('paser_imagery') is not None:
                    existing_scores[lid] = feat['properties']
            if existing_scores:
                print(f"Resume mode: {len(existing_scores)} segments already scored, skipping those", flush=True)
        except Exception:
            pass

    for i, feature in enumerate(roads['features']):
        name = feature['properties'].get('FULLNAME', f'Segment {i+1}')
        if i % 25 == 0:
            elapsed = time.time() - start
            rate = i / max(elapsed, 1)
            remaining = (len(roads['features']) - i) / max(rate, 0.01)
            print(f"  [{i+1}/{len(roads['features'])}] {name[:40]}  "
                  f"(~{remaining/60:.0f} min remaining)")

        # Skip if already scored in a previous run
        lid = feature['properties'].get('LINEARID')
        if lid and lid in existing_scores:
            feature['properties'].update(existing_scores[lid])
            scored += 1
            continue

        result = score_road(feature, session, claude_client)

        if result:
            feature['properties'].update(result)
            scored += 1
        else:
            length_m = road_length_m(get_coords(feature)) if get_coords(feature) else 0
            feature['properties'].update({
                'quality_color':  'yellow',
                'quality_label':  'Unscored',
                'paser_imagery':  None,
                'data_source':    'Imagery — no data for this segment',
                'last_updated':   '2026',
                'length_m':       round(length_m),
            })
            skipped += 1

        time.sleep(REQUEST_DELAY)

    elapsed_min = (time.time() - start) / 60
    print(f"\nCompleted in {elapsed_min:.1f} minutes")
    print(f"  Scored:  {scored}")
    print(f"  Skipped: {skipped}")

    colors = [f['properties'].get('quality_color', 'yellow') for f in roads['features']]
    print(f"  Green:   {colors.count('green')}")
    print(f"  Yellow:  {colors.count('yellow')}")
    print(f"  Red:     {colors.count('red')}")

    print(f"\nSaving: {OUTPUT_PATH}")
    with open(OUTPUT_PATH, 'w') as f:
        json.dump(roads, f)
    print("Done.")
    print("\nNext: add 'roads_quality_imagery' to app.py ROAD_LAYERS and")
    print("      add a toggle layer in roads.html to display it.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Clinton County Aerial Imagery Road Analysis')
    parser.add_argument('--max', type=int, default=None,
                        help='Limit to N roads for testing (e.g. --max 20)')
    args = parser.parse_args()
    run(max_roads=args.max)
