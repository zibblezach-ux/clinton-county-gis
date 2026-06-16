import os
import json
from flask import Flask, render_template, jsonify, send_from_directory, Response

app = Flask(__name__)
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

ROAD_LAYERS = {
    "modot":            "Roads/roads_modot.geojson",
    "psrd":             "Roads/roads_psrd.geojson",
    "municipal":        "Roads/roads_municipal.geojson",
    "county":           "Roads/roads_county.geojson",
    "muni_boundaries":  "Roads/boundaries_municipal.geojson",
    "psrd_boundary":    "Roads/boundary_psrd.geojson",
    "clinton":          "clinton_boundary.geojson",
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
    import json
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
    if layer not in ROAD_LAYERS:
        return jsonify({"error": "unknown layer"}), 404
    filepath = os.path.join(STATIC_DIR, ROAD_LAYERS[layer])
    if not os.path.exists(filepath):
        return jsonify({"type": "FeatureCollection", "features": []}), 200
    def generate():
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                yield chunk
    return Response(generate(), mimetype="application/json")

@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
