from fastapi import FastAPI, HTTPException, Header, Depends
from pydantic import BaseModel
from typing import Optional, Tuple
import os, json
from datetime import datetime, timedelta, timezone

import gspread
from google.oauth2.service_account import Credentials

app = FastAPI(title="License Server for Drive Uploader Pro", version="1.0.0")

# ---------- Config ----------
SHEET_ID = os.getenv("SHEET_ID", "").strip()
SHEET_NAME = os.getenv("SHEET_NAME", "google drive").strip()  # giữ default như app cũ
LICENSE_API_KEY = os.getenv("LICENSE_API_KEY", "").strip()
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "7").strip())  # GMT+7 mặc định

# ---------- Helpers ----------
def tz_now_gmt() -> datetime:
    return datetime.now(timezone.utc)

def tz_now_gmt7() -> datetime:
    return datetime.now(timezone(timedelta(hours=TZ_OFFSET_HOURS)))

def fmt_iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")

def get_gspread_client():
    """
    Ưu tiên nhận SA JSON từ biến môi trường SA_JSON (chuỗi).
    Nếu không có, sẽ dùng GOOGLE_APPLICATION_CREDENTIALS (đường dẫn tới file JSON).
    """
    sa_json = os.getenv("SA_JSON", "").strip()
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        # nếu cần Drive metadata, có thể thêm: "https://www.googleapis.com/auth/drive.metadata.readonly"
    ]
    if sa_json:
        data = json.loads(sa_json)
        creds = Credentials.from_service_account_info(data, scopes=scopes)
        gc = gspread.authorize(creds)
        return gc
    gac_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if not gac_path:
        raise RuntimeError("Service Account credentials not provided. Set SA_JSON or GOOGLE_APPLICATION_CREDENTIALS.")
    creds = Credentials.from_service_account_file(gac_path, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc

def open_sheet():
    if not SHEET_ID:
        raise RuntimeError("SHEET_ID is not set")
    gc = get_gspread_client()
    sh = gc.open_by_key(SHEET_ID)
    ws = sh.worksheet(SHEET_NAME)
    return ws

# ---------- Models ----------
class LicenseRequest(BaseModel):
    machine_key: str

class LicenseResponse(BaseModel):
    machine_key: str
    activated_at: str
    expires_at: str
    run_count: int
    created: bool

# ---------- Security ----------
def verify_api_key(x_api_key: Optional[str] = Header(default=None)):
    if LICENSE_API_KEY:
        if not x_api_key or x_api_key != LICENSE_API_KEY:
            raise HTTPException(status_code=401, detail="Invalid or missing API key")

# ---------- Core ops with Google Sheets ----------
COL_KEY = 1
COL_ACTIVATED = 2
COL_EXPIRES = 3
COL_RUNCOUNT = 4

def _ensure_row(ws, row: int, machine_key: str, activated_at: str, expires_at: str, run_count: int):
    # Cập nhật một lần 4 cột bằng batch
    ws.update(f"A{row}:D{row}", [[machine_key, activated_at, expires_at, str(run_count)]])

def _find_row_by_key(ws, machine_key: str) -> Optional[int]:
    keys = ws.col_values(COL_KEY)
    for idx, val in enumerate(keys):
        if val.strip() == machine_key:
            return idx + 1
    return None

@app.get("/health")
def health():
    return {"ok": True, "now": fmt_iso(tz_now_gmt7())}

@app.post("/license/get-or-create", response_model=LicenseResponse, dependencies=[Depends(verify_api_key)])
def license_get_or_create(req: LicenseRequest):
    ws = open_sheet()
    row = _find_row_by_key(ws, req.machine_key)
    created = False
    if row is None:
        # tạo mới
        keys = ws.col_values(COL_KEY)
        row = len(keys) + 1
        activated_at = fmt_iso(tz_now_gmt7())
        expires_at = fmt_iso(tz_now_gmt7() + timedelta(days=14))
        run_count = 0
        _ensure_row(ws, row, req.machine_key, activated_at, expires_at, run_count)
        created = True
    else:
        # đọc hiện trạng
        activated_at = ws.cell(row, COL_ACTIVATED).value or ""
        expires_at = ws.cell(row, COL_EXPIRES).value or ""
        run_val = ws.cell(row, COL_RUNCOUNT).value or "0"
        try:
            run_count = int(str(run_val).strip() or "0")
        except Exception:
            run_count = 0
        # auto fill nếu trống
        changed = False
        if not activated_at:
            activated_at = fmt_iso(tz_now_gmt7()); changed = True
        if not expires_at:
            expires_at = fmt_iso(tz_now_gmt7() + timedelta(days=7)); changed = True
        if changed:
            _ensure_row(ws, row, req.machine_key, activated_at, expires_at, run_count)
    return LicenseResponse(
        machine_key=req.machine_key,
        activated_at=activated_at,
        expires_at=expires_at,
        run_count=run_count,
        created=created
    )

@app.post("/license/increment-run", response_model=LicenseResponse, dependencies=[Depends(verify_api_key)])
def license_increment_run(req: LicenseRequest):
    ws = open_sheet()
    row = _find_row_by_key(ws, req.machine_key)
    created = False
    if row is None:
        # tạo mới nếu chưa có
        keys = ws.col_values(COL_KEY)
        row = len(keys) + 1
        activated_at = fmt_iso(tz_now_gmt7())
        expires_at = fmt_iso(tz_now_gmt7() + timedelta(days=7))
        run_count = 0
        _ensure_row(ws, row, req.machine_key, activated_at, expires_at, run_count)
        created = True
    else:
        activated_at = ws.cell(row, COL_ACTIVATED).value or fmt_iso(tz_now_gmt7())
        expires_at = ws.cell(row, COL_EXPIRES).value or fmt_iso(tz_now_gmt7() + timedelta(days=7))
        run_val = ws.cell(row, COL_RUNCOUNT).value or "0"
        try:
            run_count = int(str(run_val).strip() or "0")
        except Exception:
            run_count = 0

    run_count += 1
    _ensure_row(ws, row, req.machine_key, activated_at, expires_at, run_count)

    return LicenseResponse(
        machine_key=req.machine_key,
        activated_at=activated_at,
        expires_at=expires_at,
        run_count=run_count,
        created=created
    )
