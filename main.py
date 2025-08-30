from fastapi import FastAPI, HTTPException, Header, Depends
from pydantic import BaseModel
from typing import Optional
import os, json
from datetime import datetime, timedelta, timezone

import gspread
from google.oauth2.service_account import Credentials

# NEW: JWT
import jwt  # PyJWT

app = FastAPI(title="License Server for Drive Uploader Pro", version="1.1.0")

# ---------- Config ----------
SHEET_ID = os.getenv("SHEET_ID", "").strip()
SHEET_NAME = os.getenv("SHEET_NAME", "google drive").strip()
LICENSE_API_KEY = os.getenv("LICENSE_API_KEY", "").strip()
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "7").strip())

# NEW: JWT signing config
PRIVATE_KEY_PEM  = os.getenv("PRIVATE_KEY_PEM", "").strip()
PRIVATE_KEY_FILE = os.getenv("PRIVATE_KEY_FILE", "").strip()
LICENSE_AUD      = os.getenv("LICENSE_AUD", "").strip()
TOKEN_TTL_DAYS   = int(os.getenv("TOKEN_TTL_DAYS", "14").strip())  # offline token TTL

# ---------- Helpers ----------
def tz_now_gmt() -> datetime: return datetime.now(timezone.utc)
def tz_now_gmt7() -> datetime: return datetime.now(timezone(timedelta(hours=TZ_OFFSET_HOURS)))
def fmt_iso(dt: datetime) -> str: return dt.isoformat(timespec="seconds")

def get_gspread_client():
    sa_json = os.getenv("SA_JSON", "").strip()
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    if sa_json:
        data = json.loads(sa_json)
        creds = Credentials.from_service_account_info(data, scopes=scopes)
        return gspread.authorize(creds)
    gac_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if not gac_path:
        raise RuntimeError("Service Account credentials not provided. Set SA_JSON or GOOGLE_APPLICATION_CREDENTIALS.")
    creds = Credentials.from_service_account_file(gac_path, scopes=scopes)
    return gspread.authorize(creds)

def open_sheet():
    if not SHEET_ID:
        raise RuntimeError("SHEET_ID is not set")
    sh = get_gspread_client().open_by_key(SHEET_ID)
    return sh.worksheet(SHEET_NAME)

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

# ---------- Google Sheets ops ----------
COL_KEY = 1
COL_ACTIVATED = 2
COL_EXPIRES = 3
COL_RUNCOUNT = 4

def _ensure_row(ws, row: int, machine_key: str, activated_at: str, expires_at: str, run_count: int):
    ws.update(f"A{row}:D{row}", [[machine_key, activated_at, expires_at, str(run_count)]])

def _find_row_by_key(ws, machine_key: str) -> Optional[int]:
    keys = ws.col_values(COL_KEY)
    for idx, val in enumerate(keys):
        if (val or "").strip() == machine_key:
            return idx + 1
    return None

# ---------- Utilities ----------
def parse_iso_maybe(iso: str) -> Optional[datetime]:
    iso = (iso or "").strip()
    if not iso: return None
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        try:
            dt = datetime.strptime(iso.replace("Z",""), "%Y-%m-%dT%H:%M:%S")
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None

def load_private_key() -> str:
    if PRIVATE_KEY_PEM:
        return PRIVATE_KEY_PEM
    if PRIVATE_KEY_FILE and os.path.exists(PRIVATE_KEY_FILE):
        return open(PRIVATE_KEY_FILE, "r", encoding="utf-8").read()
    raise HTTPException(status_code=500, detail="PRIVATE_KEY_PEM/PRIVATE_KEY_FILE not set")

def build_offline_token(machine_key: str, run_count: int, db_expires_at_iso: str) -> str:
    now = tz_now_gmt()
    # hạn offline token: min(DB expires, now + TOKEN_TTL_DAYS)
    db_exp_dt = parse_iso_maybe(db_expires_at_iso) or (now + timedelta(days=TOKEN_TTL_DAYS))
    ttl_exp_dt = now + timedelta(days=TOKEN_TTL_DAYS)
    exp_dt = min(db_exp_dt, ttl_exp_dt)

    payload = {
        "machine_key": machine_key,
        "rc": run_count,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(exp_dt.timestamp())
    }
    if LICENSE_AUD:
        payload["aud"] = LICENSE_AUD

    token = jwt.encode(payload, load_private_key(), algorithm="RS256")
    # PyJWT>=2.x: trả về str
    return token

# ---------- Routes ----------
@app.get("/health")
def health():
    return {"ok": True, "now": fmt_iso(tz_now_gmt7())}

@app.post("/license/get-or-create", response_model=LicenseResponse, dependencies=[Depends(verify_api_key)])
def license_get_or_create(req: LicenseRequest):
    ws = open_sheet()
    row = _find_row_by_key(ws, req.machine_key)
    created = False
    if row is None:
        keys = ws.col_values(COL_KEY)
        row = len(keys) + 1
        activated_at = fmt_iso(tz_now_gmt7())
        expires_at = fmt_iso(tz_now_gmt7() + timedelta(days=7))
        run_count = 0
        _ensure_row(ws, row, req.machine_key, activated_at, expires_at, run_count)
        created = True
    else:
        activated_at = ws.cell(row, COL_ACTIVATED).value or ""
        expires_at = ws.cell(row, COL_EXPIRES).value or ""
        run_val = ws.cell(row, COL_RUNCOUNT).value or "0"
        try: run_count = int(str(run_val).strip() or "0")
        except Exception: run_count = 0
        changed = False
        if not activated_at: activated_at = fmt_iso(tz_now_gmt7()); changed = True
        if not expires_at:  expires_at  = fmt_iso(tz_now_gmt7() + timedelta(days=7)); changed = True
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
        keys = ws.col_values(COL_KEY)
        row = len(keys) + 1
        activated_at = fmt_iso(tz_now_gmt7())
        expires_at = fmt_iso(tz_now_gmt7() + timedelta(days=7))
        run_count = 0
        _ensure_row(ws, row, req.machine_key, activated_at, expires_at, run_count)
        created = True
    else:
        activated_at = ws.cell(row, COL_ACTIVATED).value or fmt_iso(tz_now_gmt7())
        expires_at  = ws.cell(row, COL_EXPIRES).value   or fmt_iso(tz_now_gmt7() + timedelta(days=7))
        run_val = ws.cell(row, COL_RUNCOUNT).value or "0"
        try: run_count = int(str(run_val).strip() or "0")
        except Exception: run_count = 0

    run_count += 1
    _ensure_row(ws, row, req.machine_key, activated_at, expires_at, run_count)
    return LicenseResponse(
        machine_key=req.machine_key,
        activated_at=activated_at,
        expires_at=expires_at,
        run_count=run_count,
        created=created
    )

# NEW: cấp offline token RS256
class TokenResponse(BaseModel):
    token: str

@app.post("/license/issue-token", response_model=TokenResponse, dependencies=[Depends(verify_api_key)])
def license_issue_token(req: LicenseRequest):
    ws = open_sheet()
    row = _find_row_by_key(ws, req.machine_key)
    if row is None:
        # đảm bảo có dòng trong sheet
        keys = ws.col_values(COL_KEY)
        row = len(keys) + 1
        activated_at = fmt_iso(tz_now_gmt7())
        expires_at  = fmt_iso(tz_now_gmt7() + timedelta(days=7))
        run_count = 0
        _ensure_row(ws, row, req.machine_key, activated_at, expires_at, run_count)
    else:
        activated_at = ws.cell(row, COL_ACTIVATED).value or fmt_iso(tz_now_gmt7())
        expires_at  = ws.cell(row, COL_EXPIRES).value   or fmt_iso(tz_now_gmt7() + timedelta(days=7))
        run_val = ws.cell(row, COL_RUNCOUNT).value or "0"
        try: run_count = int(str(run_val).strip() or "0")
        except Exception: run_count = 0

    tok = build_offline_token(req.machine_key, run_count, expires_at)
    return TokenResponse(token=tok)
