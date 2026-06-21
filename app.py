import os
import json
from flask import Flask, render_template, jsonify, send_from_directory, Response

app = Flask(__name__)
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

ROAD_LAYERS = {
    "modot":                   "Roads/roads_modot.geojson",
    "psrd":                    "Roads/roads_psrd.geojson",
    "municipal":               "Roads/roads_municipal.geojson",
    "county":                  "Roads/roads_county.geojson",
    "county_quality":          "Roads/roads_quality.geojson",
    "county_quality_img":      "Roads/roads_quality_imagery.geojson",
    "county_quality_combined": "Roads/roads_quality_combined.geojson",
    "muni_boundaries":         "Roads/boundaries_municipal.geojson",
    "psrd_boundary":           "Roads/boundary_psrd.geojson",
    "clinton":                 "clinton_boundary.geojson",
}

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/roads")
def roads():
    return render_template("roads.html")

@app.route("/districts")
def districts():
    return render_template("districts.html")

@app.route("/voters")
def voters():
    return render_template("voters.html")

@app.route("/heatmap")
def heatmap():
    return render_template("heatmap.html")

@app.route("/api/tax")
def tax_data():
    filepath = os.path.join(STATIC_DIR, "data", "tax_2025.json")
    def generate():
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk: break
                yield chunk
    return Response(generate(), mimetype="application/json",
                    headers={"Cache-Control": "public, max-age=86400"})

@app.route("/api/roads/<layer>")
def road_layer(layer):
    if layer not in ROAD_LAYERS:
        return jsonify({"error": "unknown layer"}), 404
    filepath = os.path.join(STATIC_DIR, ROAD_LAYERS[layer])
    if not os.path.exists(filepath):
        return jsonify({"type": "FeatureCollection", "features": []}), 200
    def generate():
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk: break
                yield chunk
    return Response(generate(), mimetype="application/json")

def _quality_summary(filepath, source):
    if not os.path.exists(filepath):
        return jsonify({"error": "data not available"}), 404
    with open(filepath) as f:
        data = json.load(f)
    counts = {"green": 0, "yellow": 0, "red": 0}
    miles  = {"green": 0.0, "yellow": 0.0, "red": 0.0}
    paser_counts = {1:0, 2:0, 3:0, 4:0, 5:0}
    for feat in data["features"]:
        p = feat["properties"]
        c = p.get("quality_color", "yellow")
        m = p.get("length_m", 0) / 1609.34
        counts[c] = counts.get(c, 0) + 1
        miles[c]  = miles.get(c, 0.0) + m
        paser = p.get("paser_combined") or p.get("paser") or p.get("paser_imagery")
        if paser and int(paser) in paser_counts:
            paser_counts[int(paser)] += 1
    return jsonify({
        "total_segments": len(data["features"]),
        "total_miles":    round(sum(miles.values()), 1),
        "counts":         counts,
        "miles":          {k: round(v, 1) for k, v in miles.items()},
        "paser_counts":   paser_counts,
        "source":         source,
        "last_updated":   "2026",
    })

@app.route("/api/roads/quality/summary")
def quality_summary():
    return _quality_summary(
        os.path.join(STATIC_DIR, "Roads/roads_quality.geojson"),
        "LiDAR DEM — MSDIS multi-source 2020/2021"
    )

@app.route("/api/roads/quality_imagery/summary")
def quality_imagery_summary():
    return _quality_summary(
        os.path.join(STATIC_DIR, "Roads/roads_quality_imagery.geojson"),
        "Aerial imagery 2023 — MSDIS 6-inch (AI-assessed)"
    )

@app.route("/api/roads/quality_combined/summary")
def quality_combined_summary():
    return _quality_summary(
        os.path.join(STATIC_DIR, "Roads/roads_quality_combined.geojson"),
        "Combined: LiDAR 2020/2021 + Aerial Imagery 2023"
    )

@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)

# ── Local road name editor ────────────────────────────────────────────────────
import json as _json

LOCAL_NAMES_PATH = os.path.join(STATIC_DIR, 'Roads/local_names.json')

@app.route('/names')
def names_editor():
    return render_template('names.html')

@app.route('/api/local_names', methods=['GET'])
def get_local_names():
    if not os.path.exists(LOCAL_NAMES_PATH):
        return jsonify({})
    with open(LOCAL_NAMES_PATH) as f:
        return jsonify(_json.load(f))

@app.route('/api/local_names', methods=['POST'])
def save_local_name():
    data = request.get_json()
    linear_id = data.get('linearid')
    local_name = data.get('local_name', '').strip()
    if not linear_id:
        return jsonify({'error': 'linearid required'}), 400
    if os.path.exists(LOCAL_NAMES_PATH):
        with open(LOCAL_NAMES_PATH) as f:
            names = _json.load(f)
    else:
        names = {}
    if local_name:
        names[linear_id] = local_name
    else:
        names.pop(linear_id, None)  # empty string = delete the entry
    with open(LOCAL_NAMES_PATH, 'w') as f:
        _json.dump(names, f, indent=2)
    return jsonify({'ok': True, 'linearid': linear_id, 'local_name': local_name})
