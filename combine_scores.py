#!/usr/bin/env python3
"""
Clinton County Road Quality — Combined Scoring
===============================================
Merges LiDAR structural scores and aerial imagery visual scores
into a single combined quality layer with conflict detection.

Run from clinton-county-gis folder after both pipelines complete:
    python combine_scores.py

Output:
    static/Roads/roads_quality_combined.geojson
"""

import json, os

LIDAR_PATH    = 'static/Roads/roads_quality.geojson'
IMAGERY_PATH  = 'static/Roads/roads_quality_imagery.geojson'
OUTPUT_PATH   = 'static/Roads/roads_quality_combined.geojson'

QUALITY_COLORS = {5:'green', 4:'green', 3:'yellow', 2:'red', 1:'red'}
QUALITY_LABELS = {5:'Good',  4:'Good',  3:'Fair',   2:'Poor', 1:'Poor'}
PASER_DESC     = {5:'Excellent', 4:'Good', 3:'Fair', 2:'Poor', 1:'Failed'}

def combine_paser(lidar_paser, imagery_paser):
    """
    Combine LiDAR and imagery PASER scores.
    LiDAR = structural/drainage (weight 50%)
    Imagery = surface condition (weight 50%)
    Returns combined PASER 1-5.
    """
    if lidar_paser is None and imagery_paser is None:
        return None
    if lidar_paser is None:
        return imagery_paser
    if imagery_paser is None:
        return lidar_paser
    combined = (lidar_paser * 0.5) + (imagery_paser * 0.5)
    return max(1, min(5, round(combined)))

def conflict_type(lidar_paser, imagery_paser):
    """
    Detect meaningful disagreement between LiDAR and imagery scores.
    Returns a string describing the conflict, or None if agreement.
    """
    if lidar_paser is None or imagery_paser is None:
        return None
    diff = lidar_paser - imagery_paser
    if diff >= 2:
        return 'Structure good, surface deteriorating'
    elif diff <= -2:
        return 'Surface looks ok, structural/drainage concern'
    return None

print("Loading LiDAR scores...")
with open(LIDAR_PATH) as f:
    lidar_data = json.load(f)

print("Loading imagery scores...")
with open(IMAGERY_PATH) as f:
    imagery_data = json.load(f)

# Index imagery by LINEARID for fast lookup
imagery_index = {}
for feat in imagery_data['features']:
    lid = feat['properties'].get('LINEARID')
    if lid:
        imagery_index[lid] = feat['properties']

print(f"LiDAR segments:   {len(lidar_data['features'])}")
print(f"Imagery segments: {len(imagery_data['features'])}")

combined_count = 0
lidar_only     = 0
imagery_only   = 0
conflicts      = 0

for feat in lidar_data['features']:
    props     = feat['properties']
    lid       = props.get('LINEARID')
    img_props = imagery_index.get(lid, {})

    lp = props.get('paser')
    ip = img_props.get('paser_imagery')

    combined_paser = combine_paser(lp, ip)
    conflict       = conflict_type(lp, ip)

    if lp and ip:
        combined_count += 1
        if conflict:
            conflicts += 1
    elif lp:
        lidar_only += 1
    elif ip:
        imagery_only += 1

    props['paser_combined']      = combined_paser
    props['paser_lidar']         = lp
    props['paser_imagery']       = ip
    props['quality_color']       = QUALITY_COLORS.get(combined_paser, 'yellow') if combined_paser else 'yellow'
    props['quality_label']       = QUALITY_LABELS.get(combined_paser, 'Unscored') if combined_paser else 'Unscored'
    props['conflict']            = conflict
    props['surface_condition']   = img_props.get('surface_condition')
    props['drainage_imagery']    = img_props.get('drainage_imagery')
    props['visible_distress']    = img_props.get('visible_distress')
    props['ai_notes']            = img_props.get('ai_notes')
    props['data_source']         = 'Combined: LiDAR 2020/2021 + Aerial Imagery 2023'
    props['last_updated']        = '2026'

colors = [f['properties'].get('quality_color','yellow') for f in lidar_data['features']]
miles  = {}
for f in lidar_data['features']:
    c = f['properties'].get('quality_color','yellow')
    m = f['properties'].get('length_m', 0) / 1609.34
    miles[c] = miles.get(c, 0) + m

print(f"\nCombined results:")
print(f"  Both sources scored: {combined_count}")
print(f"  LiDAR only:          {lidar_only}")
print(f"  Imagery only:        {imagery_only}")
print(f"  Conflicts detected:  {conflicts}")
print(f"\n  Green  (Good): {colors.count('green')} segments — {miles.get('green',0):.1f} mi")
print(f"  Yellow (Fair): {colors.count('yellow')} segments — {miles.get('yellow',0):.1f} mi")
print(f"  Red    (Poor): {colors.count('red')} segments — {miles.get('red',0):.1f} mi")

for p in [1,2,3,4,5]:
    n = sum(1 for f in lidar_data['features'] if f['properties'].get('paser_combined')==p)
    print(f"  PASER {p}: {n}")

print(f"\nSaving {OUTPUT_PATH}...")
with open(OUTPUT_PATH, 'w') as f:
    json.dump(lidar_data, f)
print("Done. Add 'county_quality_combined' to app.py ROAD_LAYERS to display it.")
