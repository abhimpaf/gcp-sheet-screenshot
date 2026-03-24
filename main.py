import os
import time
import datetime
import gspread
from playwright.sync_api import sync_playwright
from google.oauth2.service_account import Credentials

# --- CONFIGURATION ---
SHEET_ID = 'YOUR_SPREADSHEET_ID'
DRIVE_FOLDER_ID = '1J4vznMLK7SWWWWg8kn_w7p4FulKHFaEo'
# Path to your Service Account JSON in the Docker container
SERVICE_ACCOUNT_FILE = 'service_account.json'

def get_screenshot():
    # 1. AUTHENTICATION & DATA SLICING
    scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    ss = client.open_by_key(SHEET_ID)
    source_sheet = ss.worksheet("fr_hourly")

    # Calculate Hourly Columns (7 AM start at Column E)
    now = datetime.datetime.now()
    report_hour = now.hour - 1
    if report_hour < 7 or report_hour > 22: return
    start_col_idx = 4 + ((report_hour - 7) * 3)

    # 2. CREATE STITCHED TEMP SHEET
    # We create a temp sheet so Playwright only sees the 7 relevant columns
    try:
        temp_sheet = ss.add_worksheet(title="Temp_Snapshot", rows="100", cols="7")
    except:
        temp_sheet = ss.worksheet("Temp_Snapshot")
        temp_sheet.clear()

    # Get A-D and Hourly Data from Row 2 (Headers)
    # Note: We use the API to "stitch" columns side-by-side
    all_data = source_sheet.get_all_values()
    headers = all_data[1:3] # Rows 2 and 3
    body = all_data[3:]     # Row 4 onwards

    # 3. PLAYWRIGHT RENDERER
    with sync_playwright() as p:
        # Launch browser with specific viewport for high-res
        browser = p.chromium.launch(headless=True)
        # Use a context to handle auth cookies if necessary, 
        # or just make the temp sheet "anyone with link can view" for the bot
        page = browser.new_page(viewport={'width': 1280, 'height': 800})
        
        # Get unique batches from Column A
        batches = list(set([row[0] for row in body if row[0]]))

        for batch in batches:
            # Build the specific table for this batch
            output_table = []
            for h in headers:
                output_table.append(h[0:4] + h[start_col_idx : start_col_idx+3])
            
            for row in body:
                if row[0] == batch:
                    output_table.append(row[0:4] + row[start_col_idx : start_col_idx+3])

            # Write to temp sheet
            temp_sheet.clear()
            temp_sheet.update('A1', output_table)
            
            # Construct the URL to the specific sheet
            url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={temp_sheet.id}"
            
            # Navigate and take screenshot
            page.goto(url, wait_until="networkidle")
            
            # Wait for Google Sheets UI to settle and hide the grid/toolbars via URL params
            page.goto(url + "&rm=minimal", wait_until="networkidle")
            time.sleep(2) # Allow fonts/values to render

            # Target the specific spreadsheet canvas element
            selector = ".grid-container" 
            page.locator(selector).screenshot(path=f"{batch}_report.jpg", type="jpeg", quality=90)
            print(f"Captured screenshot for {batch}")

        browser.close()
    
    # Cleanup
    ss.del_worksheet(temp_sheet)

if __name__ == "__main__":
    get_screenshot()
