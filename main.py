from flask import Flask, request, send_file, jsonify
from playwright.sync_api import sync_playwright
import os

app = Flask(__name__)

def generate_html_table(data):
    html = """
    <html>
    <head>
    <style>
    body { font-family: Arial; padding: 20px; }
    table { border-collapse: collapse; }
    td {
        border: 1px solid #ccc;
        padding: 10px;
        min-width: 80px;
        text-align: center;
    }
    tr:nth-child(1) {
        background-color: #f2f2f2;
        font-weight: bold;
    }
    </style>
    </head>
    <body>
    <table>
    """

    for row in data:
        html += "<tr>"
        for cell in row:
            html += f"<td>{cell}</td>"
        html += "</tr>"

    html += "</table></body></html>"
    return html


def take_screenshot(html):
    output_path = "/tmp/output.jpg"

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html)
        page.screenshot(path=output_path, type="jpeg", quality=90, full_page=True)
        browser.close()

    return output_path


@app.route("/", methods=["GET"])
def home():
    return "Image API is running"


@app.route("/generate", methods=["POST"])
def generate():

    data = request.json

    # Expecting:
    # { "table": [["A","B"],["1","2"]] }

    table_data = data.get("table")

    if not table_data:
        return jsonify({"error": "No table data provided"}), 400

    html = generate_html_table(table_data)
    file_path = take_screenshot(html)

    return send_file(file_path, mimetype="image/jpeg")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))