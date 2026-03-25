from flask import Flask, request, send_file, jsonify
from playwright.sync_api import sync_playwright
import os
import io

app = Flask(__name__)

# -------------------------------
# HTML GENERATOR WITH FORMATTING
# -------------------------------
def generate_html_from_formatted(payload):
    blocks = payload.get("data", payload)  # fallback if no wrapper

    # Removed body padding/margins so the table sits perfectly flush
    html = """
    <html>
    <head>
    <style>
    body {
        margin: 0;
        padding: 0;
        background: #ffffff;
        display: inline-block;
        font-family: Arial, sans-serif;
    }
    table {
        border-collapse: collapse;
        margin: 0;
        border-spacing: 0;
    }
    td {
        border: 1px solid #d0d0d0;
        padding: 6px 10px;
        min-width: 80px;
        max-width: 200px;
        word-wrap: break-word;
    }
    </style>
    </head>
    <body>
    """

    for block_name, block in blocks.items():
        values = block.get("values", [])
        backgrounds = block.get("backgrounds", [])
        font_colors = block.get("fontColors", [])
        font_weights = block.get("fontWeights", [])
        font_sizes = block.get("fontSizes", [])
        aligns = block.get("horizontalAlignments", [])

        if not values:
            continue

        html += "<table>"
        
        # Calculate maximum columns in the data to know how wide to stretch the merged header
        max_cols = max((len(row) for row in values), default=1)

        for i in range(len(values)):
            html += "<tr>"

            for j in range(len(values[i])):
                val = values[i][j]
                bg = safe_get(backgrounds, i, j, "#ffffff")
                color = safe_get(font_colors, i, j, "#000000")
                weight = safe_get(font_weights, i, j, "normal")
                size = safe_get(font_sizes, i, j, 10)
                align = safe_get(aligns, i, j, "left")

                style = f"background:{bg}; color:{color}; font-weight:{weight}; font-size:{size}px; text-align:{align};"

                # OPTIMIZATION: If this is the very first row and it only has 1 value, merge it!
                if i == 0 and len(values[i]) == 1:
                    html += f"<td colspan='{max_cols}' style='{style}'>{val}</td>"
                else:
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
# SCREENSHOT FUNCTION (OPTIMIZED)
# -------------------------------
def take_screenshot(html):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html)
        
        # OPTIMIZATION: Target ONLY the table edge-to-edge. No timeouts needed.
        # Returns bytes directly in RAM (no disk writing)
        image_bytes = page.locator("table").screenshot(type="jpeg", quality=90)
        
        browser.close()
    return image_bytes

# -------------------------------
# ROUTES
# -------------------------------
@app.route("/", methods=["GET"])
def home():
    return "✅ Image API is running edge-to-edge"

@app.route("/generate", methods=["POST"])
def generate():
    try:
        payload = request.json
        if not payload:
            return jsonify({"error": "No JSON received"}), 400

        html = generate_html_from_formatted(payload)
        
        # Get image bytes directly from memory
        image_bytes = take_screenshot(html)

        # Serve the bytes without ever touching the hard drive
        return send_file(
            io.BytesIO(image_bytes), 
            mimetype="image/jpeg",
            download_name="screenshot.jpg"
        )

    except Exception as e:
        print("ERROR:", str(e))
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))