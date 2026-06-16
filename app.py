import os
import json
from flask import Flask, render_template, jsonify, send_from_directory, Response

app = Flask(__name__)
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# ---------------------------------------------------------------------------
# Roads data — loaded once at startup from the single roads.geojson file
# and split by RTTYP jurisdiction code:
#   S / U / I  → MoDOT  (state highways, US routes, interstates)
#   C          → County Commission
#   M          → Municipal (city streets)
#   (empty)    → County catch-all (unnamed / private / local)
# ---------------------------------------------------------------------------
_roads_cache = {}

def _load_roads():
    if _roads_cache:
        return
    roads_path = os.path.join(STATIC_DIR, "Roads", "roads.geojson")
    # Fall back to the root-level copy if the subfolder doesn't exist
    if not os.path.exists(roads_path):
        roads_path = os.path.join(STATIC_DIR, "roads.geojson")
    with open(roads_path, "r", encoding="utf-8") as f:
        all_roads = json.load(f)

    modot, county, municipal = [], [], []
    for feature in all_roads.get("features", []):
        rttyp = feature.get("properties", {}).get("RTTYP", "")
        if rttyp in ("S", "U", "I"):
            modot.append(feature)
        elif rttyp == "C":
            county.append(feature)
        elif rttyp == "M":
            municipal.append(feature)
        else:
            # Empty RTTYP — treat as county-maintained local roads
            county.append(feature)

    def _fc(features):
        return json.dumps({"type": "FeatureCollection", "features": features})

    _roads_cache["modot"]    = _fc(modot)
    _roads_cache["county"]   = _fc(county)
    _roads_cache["municipal"] = _fc(municipal)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

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
                if not chunk:
                    break
                yield chunk
    return Response(generate(), mimetype="application/json",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.route("/api/roads/<layer>")
def road_layer(layer):
    # --- boundary layers served directly from geojson files ---
    if layer == "clinton":
        path = os.path.join(STATIC_DIR, "clinton_boundary.geojson")
        if not os.path.exists(path):
            return jsonify({"type": "FeatureCollection", "features": []}), 200
        def gen_boundary():
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk
        return Response(gen_boundary(), mimetype="application/json")

    if layer == "muni_boundaries":
        path = os.path.join(STATIC_DIR, "Cities", "cities.geojson")
        if not os.path.exists(path):
            return jsonify({"type": "FeatureCollection", "features": []}), 200
        def gen_cities():
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk
        return Response(gen_cities(), mimetype="application/json")

    # --- road layers split from roads.geojson ---
    if layer not in ("modot", "county", "municipal"):
        return jsonify({"error": "unknown layer"}), 404

    _load_roads()
    payload = _roads_cache[layer]
    return Response(payload, mimetype="application/json")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)