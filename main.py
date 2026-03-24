from flask import Flask, request, send_file, jsonify
from playwright.sync_api import sync_playwright
import os
from googleapiclient.discovery import build
from google.auth import default

def fetch_sheet_data(spreadsheet_id, sheet_name, cell_range):

    creds, _ = default(scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])

    service = build("sheets", "v4", credentials=creds)

    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!{cell_range}"
    ).execute()

    return result.get("values", [])

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

    spreadsheet_id = data.get("spreadsheetId")
    sheet_name = data.get("sheetName")
    cell_range = data.get("range")

    if not spreadsheet_id:
        return jsonify({"error": "Missing spreadsheetId"}), 400

    table_data = fetch_sheet_data(spreadsheet_id, sheet_name, cell_range)

    if not table_data:
        return jsonify({"error": "No data found"}), 400

    html = generate_html_from_formatted(request.json)
    file_path = take_screenshot(html)

    return send_file(file_path, mimetype="image/jpeg")

def generate_html_from_formatted(data):

    html = """
    <html>
    <head>
    <style>
    body { font-family: Arial; padding: 20px; }
    table { border-collapse: collapse; }
    td {
        border: 1px solid #ccc;
        padding: 8px;
        min-width: 80px;
    }
    </style>
    </head>
    <body>
    """

    for block in data.values():

        values = block["values"]
        bg = block["backgrounds"]
        colors = block["fontColors"]
        weights = block["fontWeights"]
        sizes = block["fontSizes"]
        aligns = block["horizontalAlignments"]

        html += "<table>"

        for i in range(len(values)):
            html += "<tr>"

            for j in range(len(values[i])):

                style = f"""
                background:{bg[i][j]};
                color:{colors[i][j]};
                font-weight:{weights[i][j]};
                font-size:{sizes[i][j]}px;
                text-align:{aligns[i][j]};
                """

                html += f"<td style='{style}'>{values[i][j]}</td>"

            html += "</tr>"

        html += "</table><br><br>"

    html += "</body></html>"

    return html 