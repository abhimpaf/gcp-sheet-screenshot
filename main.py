from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright
import pandas as pd
import base64
import os
import hashlib

app = Flask(__name__)
FALLBACK_EMAIL = os.environ.get("FALLBACK_EMAIL", "abhimanyu.singh@advait.org.in")

# Generates a consistent, distinct hex color for each batch
def get_batch_color(batch_name):
    colors = ["#3b82f6", "#10b981", "#8b5cf6", "#f59e0b", "#ef4444", "#06b6d4", "#d946ef", "#14b8a6", "#eab308"]
    hash_idx = int(hashlib.md5(batch_name.encode()).hexdigest(), 16) % len(colors)
    return colors[hash_idx]

def process_data(raw_data, current_hour):
    target_hour_prefix = f"{current_hour:02d}:00" 
    time_frame = f"{current_hour - 1}:00 - {current_hour}:00"
    
    header_idx = next((i for i, row in enumerate(raw_data) if "Batch" in row and "Name" in row), 0)
    cell_a1 = raw_data[0][0] if raw_data and raw_data[0] else ""
    last_updated_str = cell_a1.split("|")[0].strip() if "|" in cell_a1 else ""

    headers = raw_data[header_idx]
    df = pd.DataFrame(raw_data[header_idx + 1:], columns=headers)
    
    # Ensure Time Slot is treated as a clean string to avoid mismatch
    if 'Time Slot' in df.columns:
        df['Time Slot'] = df['Time Slot'].astype(str).str.strip()

    for col in ['Attempts', 'Valid Calls', 'FEN', 'REN', 'FR']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0).astype(int)

    df_total = df[df['Time Slot'] == 'Total'].rename(columns={'Attempts': 'Total Attempts', 'Valid Calls': 'Total Calls'})
    
    # Use startswith to catch both "19:00" and "19:00:00" safely
    df_hourly = df[df['Time Slot'].str.startswith(target_hour_prefix)].rename(columns={'Valid Calls': 'Calls'}) 

    merged = pd.merge(
        df_total[['Batch', 'Name', 'Total Attempts', 'Total Calls']], 
        df_hourly[['Batch', 'Name', 'Attempts', 'Calls', 'FEN', 'REN', 'FR']], 
        on=['Batch', 'Name'], how='outer'
    ).fillna(0)
    
    merged = merged.sort_values(by='Total Calls', ascending=False)
    
    batches = {}
    for batch_name in merged['Batch'].unique():
        if not batch_name: continue
        batch_df = merged[merged['Batch'] == batch_name].copy()
        
        if batch_name.startswith('OV') or batch_name.startswith('EOV'):
            if current_hour < 9 or current_hour > 22: continue
            display_cols = ['Name', 'Total Attempts', 'Total Calls', 'Attempts', 'Calls', 'FEN']
            batch_df = batch_df[(batch_df['Calls'] < 3) & (batch_df['Attempts'] < 40) & (batch_df['FEN'] < 1)]
            if current_hour >= 11: batch_df = batch_df[batch_df['Total Calls'] > 0]
            
        elif (batch_name.startswith('W') and batch_name != 'W9') or batch_name == 'Leaders':
            display_cols = ['Name', 'Total Attempts', 'Total Calls', 'Attempts', 'Calls', 'FR']
            
        elif batch_name.startswith('F') or batch_name.startswith('R') or batch_name == 'W9':
            display_cols = ['Name', 'Total Attempts', 'Total Calls', 'Attempts', 'Calls', 'FEN', 'REN']
        else: 
            continue
            
        if batch_df.empty: continue
        final_df = batch_df[display_cols].copy()
        
        total_row = {col: final_df[col].sum() for col in display_cols[1:]}
        total_row['Name'] = 'TOTAL'
        final_df = pd.concat([final_df, pd.DataFrame([total_row])], ignore_index=True)
        
        # Inject Batch Name into the Title
        title_text = f"Batch: {batch_name} | {last_updated_str} | {time_frame} Hours"
        
        batches[batch_name] = {
            "title": title_text, 
            "headers": display_cols, 
            "rows": final_df.values.tolist(), 
            "colsCount": len(display_cols),
            "color": get_batch_color(batch_name)
        }
    
    return batches, time_frame

def generate_html(batch_data):
    max_cols = batch_data["colsCount"]
    header_color = batch_data["color"]
    
    html = f"""
    <html>
    <head>
    <link href='https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;700&display=swap' rel='stylesheet'>
    <style>
    body {{ margin: 0; padding: 0; background: #ffffff; display: inline-block; font-family: 'IBM Plex Mono', monospace; }} 
    table {{ border-collapse: collapse; margin: 0; font-size: 11px; }} 
    td {{ border: 1px solid #cccccc; padding: 6px 12px; white-space: nowrap; width: max-content; }} 
    .title-row {{ font-weight: bold; font-size: 13px; text-align: center; background: #ffffff; color: #333333; }} 
    
    /* Dynamic Header Color */
    .header-row td {{ font-weight: bold; text-align: center; background: {header_color}; color: #ffffff; border: 1px solid {header_color}; }} 
    
    .data-row td {{ text-align: center; }} 
    /* Yellow highlight for Agent Names */
    .data-row td:nth-child(1) {{ text-align: left; background-color: #fef08a; color: #000000; font-weight: bold; }} 
    
    .total-row td {{ font-weight: bold; background: #e5e7eb; text-align: center; color: #000000; }} 
    .total-row td:nth-child(1) {{ text-align: left; background-color: #e5e7eb; }} 
    
    .divider {{ border-right: 2px solid #94a3b8 !important; }}
    </style>
    </head>
    <body>
    <table>
        <tr><td colspan='{max_cols}' class='title-row'>{batch_data['title']}</td></tr>
        <tr class='header-row'>
    """
    for i, h in enumerate(batch_data["headers"]): html += f"<td class='{'divider' if i == 2 else ''}'>{h}</td>"
    html += "</tr>"
    
    for r_idx, row in enumerate(batch_data["rows"]):
        row_cls = "total-row" if r_idx == len(batch_data["rows"]) - 1 else "data-row"
        html += f"<tr class='{row_cls}'>"
        for c_idx, val in enumerate(row):
            display_val = int(val) if isinstance(val, float) and val.is_integer() else val
            html += f"<td class='{'divider' if c_idx == 2 else ''}'>{display_val}</td>"
        html += "</tr>"
    html += "</table></body></html>"
    return html

def take_screenshot(html):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(device_scale_factor=2).new_page()
        page.set_content(html, wait_until="networkidle")
        page.evaluate("document.fonts.ready")
        img = page.locator("table").screenshot(type="png")
        browser.close()
    return base64.b64encode(img).decode('utf-8')

@app.route("/run-hourly", methods=["POST"])
def run_hourly():
    payload = request.json
    if not payload:
        return jsonify({"status": "error", "message": "No payload provided"}), 400
        
    current_hour = payload.get("currentHour")
    raw_data = payload.get("rawData", [])
    routing_data = payload.get("routingData", [])
    
    if current_hour < 8 or current_hour > 23:
        return jsonify({"status": "skip", "message": "Outside operating hours"})
        
    try:
        routing_map = {}
        for row in routing_data:
            if len(row) >= 4 and row[0] and row[1] and row[3]:
                for b in [x.strip() for x in str(row[3]).split(",") if x.strip()]:
                    if b not in routing_map: routing_map[b] = []
                    routing_map[b].append({"to": str(row[1]).strip(), "cc": str(row[2]).strip() if len(row) > 2 else ""})

        processed_batches, time_frame = process_data(raw_data, current_hour)
        if not processed_batches: 
            return jsonify({"status": "skip", "message": "No data qualified"})

        outbox = {}
        for batch_name, batch_data in processed_batches.items():
            img_b64 = take_screenshot(generate_html(batch_data))
            filename = f"{batch_name}_{current_hour-1}00-{current_hour}00.png"
            routes = routing_map.get(batch_name, [{"to": FALLBACK_EMAIL, "cc": ""}])
            
            for route in routes:
                key = f"{route['to']}_{route['cc']}"
                if key not in outbox: outbox[key] = {"to": route['to'], "cc": route['cc'], "attachments": []}
                outbox[key]["attachments"].append({"filename": filename, "base64": img_b64})
                
        return jsonify({"status": "success", "time_frame": time_frame, "outbox": outbox}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))