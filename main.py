from flask import Flask, request, send_file, jsonify
from playwright.sync_api import sync_playwright
import os
import io

app = Flask(__name__)

def safe_get(arr, i, j, default):
    try:
        return arr[i][j] if arr[i][j] else default
    except:
        return default

# -------------------------------
# HTML GENERATOR WITH CUSTOM STYLING
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
        font-family: 'IBM Plex Mono', 'Cascadia Mono', Consolas, monospace;
    }
    table {
        border-collapse: collapse;
        margin: 0;
        border-spacing: 0;
        font-size: 12px;
    }
    td {
        border: 1px solid #cccccc;
        padding: 6px 12px; 
        white-space: nowrap;
        width: max-content;
    }
    </style>
    </head>
    <body>
    """

    for block_name, block in blocks.items():
        values = block.get("values", [])
        backgrounds = block.get("backgrounds", [])

        if not values:
            continue
            
        # Dynamically find the indexes to ensure styling doesn't break
        batch_idx = 0 # Fallback to index 0
        name_idx = 1 # Fallback to index 1
        divider_idx = 3 # Fallback to index 3
        
        if len(values) > 1:
            headers_lower = [str(h).strip().lower() for h in values[1]]
            
            try:
                batch_idx = headers_lower.index("batch")
            except ValueError:
                pass
                
            try:
                name_idx = headers_lower.index("name")
            except ValueError:
                pass
                
            try:
                divider_idx = headers_lower.index("total calls")
            except ValueError:
                try:
                    divider_idx = headers_lower.index("valid calls")
                except ValueError:
                    pass

        html += "<table>"
        max_cols = max((len(row) for row in values), default=1)

        for i in range(len(values)):
            html += "<tr>"

            for j in range(len(values[i])):
                if i == 0 and j > 0:
                    continue

                val = values[i][j]
                if val == "" or val is None:
                    val = "&nbsp;"

                bg = "#ffffff"
                color = "#000000"
                weight = "normal"
                align = "center"

                # 1. Title Row
                if i == 0:
                    bg = "#ffffff"
                    weight = "bold"
                    align = "center"
                
                # 2. Header Row (Light Purple)
                elif i == 1:
                    bg = "#e9d5ff" 
                    weight = "bold"
                    align = "center"
                
                # 3. Data Rows
                else:
                    # Highlight BOTH Batch and Name columns in a lighter yellow
                    if j == batch_idx or j == name_idx:
                        bg = "#fef9c3" # Lighter shade of yellow
                        weight = "bold"
                        align = "left"
                    else:
                        align = "center"
                        # Pull the red threshold highlight from Apps Script payload
                        sheet_bg = safe_get(backgrounds, i, j, "#ffffff")
                        if sheet_bg.lower() not in ["#ffffff", "#fff", "white"]:
                            bg = sheet_bg

                style = f"background:{bg}; color:{color}; font-weight:{weight}; text-align:{align};"

                # Keep the divider line dynamically after Total Calls
                if j == divider_idx:
                    style += " border-right: 2px solid #94a3b8;"

                if i == 0 and j == 0:
                    html += f"<td colspan='{max_cols}' style='{style}'>{val}</td>"
                else:
                    html += f"<td style='{style}'>{val}</td>"

            html += "</tr>"
        html += "</table>"

    html += "</body></html>"
    return html


# -------------------------------
# SCREENSHOT FUNCTION 
# -------------------------------
def take_screenshot(html):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(device_scale_factor=2) 
        page = context.new_page()
        
        page.set_content(html, wait_until="networkidle")
        page.evaluate("document.fonts.ready")
        
        image_bytes = page.locator("table").first.screenshot(type="png")
        browser.close()
    return image_bytes


# -------------------------------
# ROUTES
# -------------------------------
@app.route("/", methods=["GET"])
def home():
    return "✅ Image API is running (Optimized Reverted Architecture with Lighter Yellow Highlights)"

@app.route("/generate", methods=["POST"])
def generate():
    try:
        payload = request.json
        if not payload:
            return jsonify({"error": "No JSON received"}), 400

        html = generate_html_from_formatted(payload)
        image_bytes = take_screenshot(html)

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