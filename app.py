

import io
import os
import re
import time
import json
from datetime import date, datetime, timedelta
from typing import List, Tuple

import pandas as pd
import pytz
import streamlit as st

# Google APIs
from google.oauth2.service_account import Credentials as SA
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Drive as YOU (OAuth)
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive

# =====================
# ---- CONFIG ---------
# =====================

SHEET_ID = st.secrets.get("SHEET_ID", "")
DRIVE_FOLDER_ID = st.secrets.get("DRIVE_FOLDER_ID", "")

WORKSHEET = "Entries"
HEADER_IMAGE = None

# Optional Calendar
CALENDAR_ID = ""   # e.g. "abc123@group.calendar.google.com"
CALENDAR_TIMEZONE = "America/Los_Angeles"
CALENDAR_EVENT_HOUR_LOCAL = 9

ROOMMATES = ["Abhinav", "Harsha", "Gowith", "Gautam", "Dinesh", "Prudhvi", "Shanmukh"]
CATEGORIES = ["Rent", "Utilities", "PG&E"]
HEADERS = ["timestamp", "roommate", "month", "category", "amount", "status", "date", "notes", "file_links"]

# ==========================
# ---- ID NORMALIZERS  -----
# ==========================
def normalize_sheet_id(value: str) -> str:
    if not value:
        return value
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", value)
    return m.group(1) if m else value

def normalize_drive_folder_id(value: str) -> str:
    if not value:
        return value
    m = re.search(r"/folders/([a-zA-Z0-9-_]+)", value)
    return m.group(1) if m else value

SHEET_ID = normalize_sheet_id(SHEET_ID)
DRIVE_FOLDER_ID = normalize_drive_folder_id(DRIVE_FOLDER_ID)

# ==========================
# ---- SECRETS HELPERS  ----
# ==========================
def _load_service_account_from_secrets() -> dict:
    """Supports either a TOML table or a triple-quoted JSON string."""
    raw = st.secrets["google_service_account"]
    return json.loads(raw) if isinstance(raw, str) else dict(raw)

def _load_drive_oauth_client_from_secrets() -> dict:
    raw = st.secrets["gdrive_oauth_client"]
    return json.loads(raw) if isinstance(raw, str) else dict(raw)

# ==========================
# ---- GOOGLE HELPERS  -----
# ==========================
@st.cache_resource(show_spinner=False)
def get_sheets_service():
    if "google_service_account" not in st.secrets:
        raise RuntimeError("Missing 'google_service_account' in Streamlit secrets.")
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = SA.from_service_account_info(_load_service_account_from_secrets(), scopes=scopes)
    return build("sheets", "v4", credentials=creds)

@st.cache_resource(show_spinner=False)
def get_calendar_service():
    if not CALENDAR_ID or "google_service_account" not in st.secrets:
        return None
    scopes = ["https://www.googleapis.com/auth/calendar"]
    creds = SA.from_service_account_info(_load_service_account_from_secrets(), scopes=scopes)
    return build("calendar", "v3", credentials=creds)

@st.cache_resource(show_spinner=False)
def get_drive_client():
    """Authenticate to Google Drive via OAuth (as YOU)."""
    if "gdrive_oauth_client" not in st.secrets:
        raise RuntimeError("Missing 'gdrive_oauth_client' in Streamlit secrets.")

    settings = {
        "client_config_backend": "settings",
        "client_config": _load_drive_oauth_client_from_secrets(),
        "save_credentials": True,
        "save_credentials_backend": "file",
        "save_credentials_file": "token.json",
        "oauth_scope": [
            "https://www.googleapis.com/auth/drive.file",
            "https://www.googleapis.com/auth/drive",
        ],
    }
    gauth = GoogleAuth(settings=settings)
    try:
        gauth.LoadCredentialsFile("token.json")
    except Exception:
        pass

    if getattr(gauth, "credentials", None) is None:
        # On Streamlit Cloud this shows a URL/code flow in the logs.
        gauth.CommandLineAuth()
    elif gauth.access_token_expired:
        gauth.Refresh()
    else:
        gauth.Authorize()
    return GoogleDrive(gauth)

# ==========================
# ---- SHEETS UTILITIES ----
# ==========================
def _retry_sheets(callable_fn, *args, **kwargs):
    delay = 1.0
    last_err = None
    for _ in range(5):
        try:
            return callable_fn(*args, **kwargs)
        except HttpError as e:
            status = getattr(e, "status_code", None) or (e.resp.status if hasattr(e, "resp") else None)
            if status and 500 <= int(status) < 600:
                last_err = e
                time.sleep(delay)
                delay = min(delay * 2, 8)
                continue
            raise
    raise last_err if last_err else RuntimeError("Unknown Sheets error")

def ensure_worksheet_and_headers(svc):
    if not SHEET_ID:
        raise RuntimeError("SHEET_ID is blank. Add it to Streamlit secrets.")
    meta = _retry_sheets(svc.spreadsheets().get(spreadsheetId=SHEET_ID).execute)
    sheets = {s["properties"]["title"]: s for s in meta.get("sheets", [])}
    if WORKSHEET not in sheets:
        _retry_sheets(
            svc.spreadsheets().batchUpdate(
                spreadsheetId=SHEET_ID,
                body={"requests": [{"addSheet": {"properties": {"title": WORKSHEET}}}]}
            ).execute
        )
        _retry_sheets(
            svc.spreadsheets().values().update(
                spreadsheetId=SHEET_ID,
                range=f"{WORKSHEET}!A1",
                valueInputOption="USER_ENTERED",
                body={"values": [HEADERS]},
            ).execute
        )
    else:
        resp = _retry_sheets(
            svc.spreadsheets().values().get(
                spreadsheetId=SHEET_ID, range=f"{WORKSHEET}!A1:I1"
            ).execute
        )
        values = resp.get("values", [])
        if not values or values[0] != HEADERS:
            _retry_sheets(
                svc.spreadsheets().values().update(
                    spreadsheetId=SHEET_ID,
                    range=f"{WORKSHEET}!A1",
                    valueInputOption="USER_ENTERED",
                    body={"values": [HEADERS]},
                ).execute
            )

def append_row(svc, row: list):
    _retry_sheets(
        svc.spreadsheets().values().append(
            spreadsheetId=SHEET_ID,
            range=f"{WORKSHEET}!A:I",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute
    )

@st.cache_data(show_spinner=False)
def load_entries_df_cached():
    svc = get_sheets_service()
    ensure_worksheet_and_headers(svc)
    resp = _retry_sheets(
        svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=f"{WORKSHEET}!A2:I"
        ).execute
    )
    rows = resp.get("values", [])
    if not rows:
        return pd.DataFrame(columns=HEADERS)
    df = pd.DataFrame(rows, columns=HEADERS)
    if "amount" in df.columns:
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)
    return df

# ==========================
# ---- DRIVE UTILITIES  ----
# ==========================
def ensure_folder(drive: GoogleDrive, name: str, parent_id: str) -> str:
    q = (
        f"title = '{name}' and "
        f"mimeType = 'application/vnd.google-apps.folder' and "
        f"'{parent_id}' in parents and trashed = false"
    )
    lst = drive.ListFile({"q": q}).GetList()
    if lst:
        return lst[0]["id"]
    folder = drive.CreateFile({
        "title": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [{"id": parent_id}],
    })
    folder.Upload()
    return folder["id"]

def upload_files_to_drive(
    drive: GoogleDrive,
    files: List[Tuple[str, bytes]],
    *,
    roommate: str,
    month: str,
    category: str,
) -> List[str]:
    if not DRIVE_FOLDER_ID or not files:
        return []
    month_id = ensure_folder(drive, month, DRIVE_FOLDER_ID)
    rm_id = ensure_folder(drive, roommate, month_id)
    cat_id = ensure_folder(drive, category, rm_id)

    links = []
    for filename, data in files:
        f = drive.CreateFile({"title": filename, "parents": [{"id": cat_id}]})
        f.content = io.BytesIO(data)
        f.Upload()
        # If you prefer fully private links, remove the permission below.
        try:
            f.InsertPermission({"type": "anyone", "role": "reader", "value": "anyone"})
        except Exception:
            pass
        links.append(f.get("alternateLink"))
    return links

# =====================
# -------- UI ---------
# =====================
st.set_page_config(page_title="Roommate Rent & Utilities", page_icon="💸", layout="wide")

if HEADER_IMAGE:
    try:
        st.image(HEADER_IMAGE, use_container_width=True)
    except Exception:
        pass

st.title("💸 Roommate Rent & Utilities Dashboard")

# Sidebar
with st.sidebar:
    st.header("Settings & Filters")

    st.subheader("Roommates")
    rm_names = []
    for i, name in enumerate(ROOMMATES, start=1):
        rm_names.append(st.text_input(f"Roommate {i}", value=name, key=f"rm_{i}"))
    ROOMMATES[:] = rm_names

    cal = get_calendar_service()
    if CALENDAR_ID and cal:
        if st.button("Create/Update monthly calendar reminders (1st, 9am)"):
            tz = pytz.timezone(CALENDAR_TIMEZONE)
            now = datetime.now(tz)
            first_this = now.replace(day=1, hour=CALENDAR_EVENT_HOUR_LOCAL, minute=0, second=0, microsecond=0)
            start_dt = first_this if now < first_this else (
                first_this.replace(
                    year=first_this.year + (1 if first_this.month == 12 else 0),
                    month=(1 if first_this.month == 12 else first_this.month + 1)
                )
            )
            end_dt = start_dt + timedelta(hours=1)
            created = 0
            for rm in ROOMMATES:
                summary = f"Rent & Utilities — {rm}"
                body = {
                    "summary": summary,
                    "description": "Monthly reminder to update dashboard: Rent / Utilities / PG&E",
                    "start": {"dateTime": start_dt.isoformat(), "timeZone": CALENDAR_TIMEZONE},
                    "end": {"dateTime": end_dt.isoformat(), "timeZone": CALENDAR_TIMEZONE},
                    "recurrence": ["RRULE:FREQ=MONTHLY;BYMONTHDAY=1"],
                }
                existing = cal.events().list(calendarId=CALENDAR_ID, q=summary, singleEvents=True, maxResults=1).execute()
                if existing.get("items"):
                    ev_id = existing["items"][0]["id"]
                    cal.events().update(calendarId=CALENDAR_ID, eventId=ev_id, body=body).execute()
                else:
                    cal.events().insert(calendarId=CALENDAR_ID, body=body).execute()
                created += 1
            st.success(f"Calendar reminders ensured for {created} roommate(s).")
    else:
        st.info("Optional: set CALENDAR_ID to enable 1st-of-month reminders.")

    st.subheader("Filters")
    try:
        df_now = load_entries_df_cached()
    except Exception as e:
        st.error(f"Could not load data from Sheets. Check SHEET_ID + sharing. Error: {e}")
        df_now = pd.DataFrame(columns=HEADERS)

    roommates_in_sheet = sorted([r for r in (df_now["roommate"].unique().tolist() if not df_now.empty else []) if r])
    selected_roommate = st.selectbox("Roommate", options=["All"] + roommates_in_sheet, index=0)
    selected_month = st.selectbox("Month", options=["All"] + (sorted(df_now["month"].unique().tolist()) if not df_now.empty else []), index=0)
    selected_status = st.selectbox("Status", ["All", "Paid", "Unpaid"], index=0)

# Add Entries
st.subheader("Add entries — one column per roommate")
columns = st.columns(len(ROOMMATES))
sheets = None
drive = None

for col, rm in zip(columns, ROOMMATES):
    with col:
        st.markdown(f"### {rm}")
        with st.form(f"form_{rm}"):
            month = st.text_input("Month (YYYY-MM)", key=f"month_{rm}")
            notes = st.text_area("Notes (optional)", key=f"notes_{rm}")

            pending = []
            for cat in CATEGORIES:
                st.markdown(f"**{cat}**")
                paid = st.checkbox("Paid", key=f"paid_{rm}_{cat}")
                status = "Paid" if paid else "Unpaid"
                amount = st.number_input("Amount", min_value=0.0, step=1.0, key=f"amt_{rm}_{cat}")
                bill_date = st.date_input("Date", value=date.today(), key=f"date_{rm}_{cat}")
                uploads = st.file_uploader(
                    "Upload PDF/Screenshot (optional)",
                    type=["pdf", "png", "jpg", "jpeg"],
                    accept_multiple_files=True,
                    key=f"up_{rm}_{cat}",
                )
                st.divider()

                if month:
                    pending.append({
                        "roommate": rm,
                        "month": month,
                        "category": cat,
                        "amount": amount,
                        "status": status,
                        "date": bill_date,
                        "notes": notes,
                        "uploads": uploads,
                    })

            submitted = st.form_submit_button("Save for " + rm)

        if submitted:
            if not SHEET_ID:
                st.error("Please set SHEET_ID in Streamlit secrets.")
            elif not pending:
                st.warning("Nothing to save — enter Month and at least one category.")
            else:
                if sheets is None:
                    sheets = get_sheets_service()
                    ensure_worksheet_and_headers(sheets)

                drive_error = None
                for item in pending:
                    links = []
                    try:
                        if DRIVE_FOLDER_ID and item.get("uploads"):
                            if drive is None:
                                drive = get_drive_client()
                            files = [(uf.name, uf.read()) for uf in item["uploads"]]
                            links = upload_files_to_drive(
                                drive, files,
                                roommate=item["roommate"],
                                month=item["month"],
                                category=item["category"],
                            )
                    except Exception as e:
                        drive_error = e  # keep writing to sheet

                    row = [
                        time.strftime("%Y-%m-%d %H:%M:%S"),
                        item["roommate"],
                        item["month"],
                        item["category"],
                        f"{item['amount']}",
                        item["status"],
                        item["date"].strftime("%Y-%m-%d"),
                        item["notes"],
                        "; ".join(links) if links else "",
                    ]
                    try:
                        append_row(sheets, row)
                    except HttpError as e:
                        st.error(f"Sheets append error: {e}")
                        break

                st.success(f"Saved {len(pending)} item(s) for {rm}.")
                if drive_error:
                    st.warning(f"Drive upload issue: {drive_error}")
                st.cache_data.clear()

# Summary
st.subheader("Summary")
try:
    df = load_entries_df_cached()
except Exception as e:
    st.error(f"Could not load data from Sheets. Check SHEET_ID and sharing. Error: {e}")
    df = pd.DataFrame(columns=HEADERS)

if df.empty:
    st.info("No entries yet. Add your first one above.")
else:
    filtered = df.copy()
    if selected_roommate != "All":
        filtered = filtered[filtered["roommate"] == selected_roommate]
    if selected_month != "All":
        filtered = filtered[filtered["month"] == selected_month]
    if selected_status != "All":
        filtered = filtered[filtered["status"] == selected_status]

    total_due = filtered.loc[filtered["status"] == "Unpaid", "amount"].sum()
    total_paid = filtered.loc[filtered["status"] == "Paid", "amount"].sum()
    total_all = filtered["amount"].sum()
    c1, c2, c3 = st.columns(3)
    c1.metric("Total Paid", f"${total_paid:,.2f}")
    c2.metric("Total Unpaid", f"${total_due:,.2f}")
    c3.metric("All Entries", f"${total_all:,.2f}")

    st.divider()
    st.caption("Click column headers to sort. File links open the uploaded receipts in Drive.")
    st.dataframe(filtered, use_container_width=True)
