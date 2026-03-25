from flask import Flask, request, send_file, jsonify
from playwright.sync_api import sync_playwright
import os
import io

app = Flask(__name__)

# -------------------------------
# HTML GENERATOR WITH SPREADSHEET CSS
# -------------------------------
def generate_html_from_formatted(payload):
    blocks = payload.get("data", payload)

    html = """
    <html>
    <head>
    <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;700&display=swap" rel="stylesheet">
    
    <style>
    body {
        margin: 0;
        padding: 0;
        background: #ffffff;
        display: inline-block;
        
        /* Prioritize IBM Plex Mono, fallback to system mono fonts */
        font-family: 'IBM Plex Mono', 'Cascadia Mono', Consolas, monospace;
    }
    table {
        border-collapse: collapse;
        margin: 0;
        border-spacing: 0;
        font-size: 13px;
    }
    td {
        border: 1px solid #cccccc;
        padding: 4px 8px; 
        white-space: nowrap; 
        empty-cells: show;
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

                # 2. VISUAL DIVIDER: Add a thicker border after Column D (Index 3)
                if j == 3:
                    style += " border-right: 2px solid #999999;"

                # Handle the Top Merged Title Row
                if i == 0 and len(values[i]) == 1:
                    html += f"<td colspan='{max_cols}' style='{style}'>{val}</td>"
                else:
                    html += f"<td style='{style}'>{val}</td>"

            html += "</tr>"
        html += "</table>"

    html += "</body></html>"
    return html

def safe_get(arr, i, j, default):
    try:
        return arr[i][j]
    except:
        return default

# -------------------------------
# SCREENSHOT FUNCTION (ULTRA-CRISP UPGRADE)
# -------------------------------
def take_screenshot(html):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        
        # 1. FIX BLURRINESS: Emulate a Retina/4K display (3x pixel density)
        context = browser.new_context(device_scale_factor=2) 
        page = context.new_page()
        
        # 2. FIX TYPEWRITER FONT: Tell it to wait until network traffic stops (downloads finish)
        page.set_content(html, wait_until="networkidle")
        
        # 3. EXTRA SAFETY: Explicitly force the browser to wait until all web fonts are fully rendered
        page.evaluate("document.fonts.ready")
        
        # Snap the lossless PNG
        image_bytes = page.locator("table").screenshot(type="png")
        
        browser.close()
    return image_bytes

# -------------------------------
# ROUTES
# -------------------------------
@app.route("/", methods=["GET"])
def home():
    return "✅ Image API is running with high-fidelity PNG output"

@app.route("/generate", methods=["POST"])
def generate():
    try:
        payload = request.json
        if not payload:
            return jsonify({"error": "No JSON received"}), 400

        html = generate_html_from_formatted(payload)
        image_bytes = take_screenshot(html)

        # Serve as PNG
        return send_file(
            io.BytesIO(image_bytes), 
            mimetype="image/png",
            download_name="screenshot.png"
        )

    except Exception as e:
        print("ERROR:", str(e))
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))