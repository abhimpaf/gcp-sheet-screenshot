from flask import Flask, request, send_file, jsonify
from playwright.sync_api import sync_playwright
import os
import time

app = Flask(__name__)

# -------------------------------
# HTML GENERATOR WITH FORMATTING
# -------------------------------

def generate_html_from_formatted(payload):

    meta = payload.get("meta", {})
    blocks = payload.get("data", payload)  # fallback if no wrapper

    html = f"""
    <html>
    <head>
    <style>
    body {{
        font-family: Arial;
        padding: 20px;
        background: #ffffff;
    }}
    table {{
        border-collapse: collapse;
        margin-bottom: 30px;
    }}
    td {{
        border: 1px solid #d0d0d0;
        padding: 6px 10px;
        min-width: 80px;
        max-width: 200px;
        word-wrap: break-word;
    }}
    </style>
    </head>
    <body>
    """

    # Optional title
    if meta:
        html += f"""
        <h3>{meta.get("spreadsheetName", "")} - {meta.get("sheetName", "")}</h3>
        """

    # Loop through blocks (r1, r2, etc.)
    for block_name, block in blocks.items():

        values = block.get("values", [])
        backgrounds = block.get("backgrounds", [])
        font_colors = block.get("fontColors", [])
        font_weights = block.get("fontWeights", [])
        font_sizes = block.get("fontSizes", [])
        aligns = block.get("horizontalAlignments", [])

        html += "<table>"

        for i in range(len(values)):
            html += "<tr>"

            for j in range(len(values[i])):

                val = values[i][j] if j < len(values[i]) else ""

                bg = safe_get(backgrounds, i, j, "#ffffff")
                color = safe_get(font_colors, i, j, "#000000")
                weight = safe_get(font_weights, i, j, "normal")
                size = safe_get(font_sizes, i, j, 10)
                align = safe_get(aligns, i, j, "left")

                style = f"""
                background:{bg};
                color:{color};
                font-weight:{weight};
                font-size:{size}px;
                text-align:{align};
                """

                html += f"<td style='{style}'>{val}</td>"

            html += "</tr>"

        html += "</table>"

    html += "</body></html>"

    return html


# -------------------------------
# SAFE ACCESS HELPER
# -------------------------------

def safe_get(arr, i, j, default):
    try:
        return arr[i][j]
    except:
        return default


# -------------------------------
# SCREENSHOT FUNCTION
# -------------------------------

def take_screenshot(html):

    file_path = f"/tmp/output_{int(time.time())}.jpg"

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        page.set_content(html)

        # Allow rendering time
        page.wait_for_timeout(500)

        page.screenshot(
            path=file_path,
            type="jpeg",
            quality=90,
            full_page=True
        )

        browser.close()

    return file_path


# -------------------------------
# ROUTES
# -------------------------------

@app.route("/", methods=["GET"])
def home():
    return "✅ Image API with formatting is running"


@app.route("/generate", methods=["POST"])
def generate():

    try:
        payload = request.json

        if not payload:
            return jsonify({"error": "No JSON received"}), 400

        # Debug logs
        print("Incoming keys:", payload.keys())

        html = generate_html_from_formatted(payload)

        file_path = take_screenshot(html)

        return send_file(file_path, mimetype="image/jpeg")

    except Exception as e:
        print("ERROR:", str(e))
        return jsonify({"error": str(e)}), 500


# -------------------------------
# LOCAL RUN
# -------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))