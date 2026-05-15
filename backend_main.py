"""
backend/main.py — FastAPI backend cho hệ thống Point TUYTAM MARKET
Deploy lên Render. Kết nối cùng MongoDB Atlas với bot Discord.
"""

import os
import secrets
import string
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pymongo import MongoClient
from pydantic import BaseModel

# ══════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════
MONGO_URI  = os.getenv("MONGO_URI")
API_SECRET = os.getenv("API_SECRET")   # Secret key để bot gọi API an toàn

if not MONGO_URI:
    raise RuntimeError("Thiếu biến môi trường MONGO_URI")
if not API_SECRET:
    raise RuntimeError("Thiếu biến môi trường API_SECRET")

# ══════════════════════════════════════════
# MONGODB
# ══════════════════════════════════════════
_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
_db     = _client["tuytam_bot"]
_col    = _db["bot_data"]

def _get_data() -> dict:
    doc = _col.find_one({"_id": "main"})
    return doc or {}

def _save_field(key: str, value):
    _col.update_one({"_id": "main"}, {"$set": {key: value}}, upsert=True)

# ══════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════
def _gen_code(length: int = 8) -> str:
    chars = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))

def _get_cfg() -> dict:
    data = _get_data()
    return data.get("point_cfg", {
        "points_per_redeem": 100,
        "point_value":       100,
        "max_discount_pct":  20,
        "cooldown_hours":    24,
        "code_expire_mins":  10,
    })

def _check_cooldown(user_id: int) -> int | None:
    """Trả về số giây còn lại nếu còn cooldown, None nếu OK."""
    cfg   = _get_cfg()
    hours = cfg.get("cooldown_hours", 24)
    data  = _get_data()
    logs  = data.get("point_log", [])
    for entry in reversed(logs):
        if entry.get("user_id") == user_id and str(entry.get("reason", "")).startswith("redeem"):
            try:
                last_dt  = datetime.fromisoformat(entry["time"])
                cooldown = timedelta(hours=hours)
                diff     = datetime.now(timezone.utc) - last_dt
                if diff < cooldown:
                    return int((cooldown - diff).total_seconds())
            except Exception:
                pass
            break
    return None

# ══════════════════════════════════════════
# AUTH
# ══════════════════════════════════════════
def verify_secret(request: Request):
    token = request.headers.get("X-API-Secret")
    if token != API_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

# ══════════════════════════════════════════
# APP
# ══════════════════════════════════════════
app = FastAPI(title="TuyTam Point API", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Cho phép GitHub Pages gọi API
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ══════════════════════════════════════════
# ROUTES — PUBLIC (website gọi)
# ══════════════════════════════════════════

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/code/generate")
def generate_code(linkvertise_token: str = None):
    """
    Website gọi endpoint này sau khi user vượt link Linkvertise.
    Tạo mã mới và lưu vào MongoDB (không gắn với user cụ thể).
    Mã hết hạn sau X phút theo config.
    """
    cfg         = _get_cfg()
    expire_mins = cfg.get("code_expire_mins", 10)
    code        = _gen_code()
    expires_at  = (datetime.now(timezone.utc) + timedelta(minutes=expire_mins)).isoformat()

    # Lưu mã vào MongoDB
    data  = _get_data()
    codes = data.get("point_codes", {})
    codes[code] = {
        "user_id":    0,         # 0 = ai cũng dùng được, gắn khi redeem
        "expires_at": expires_at,
        "used":       False,
        "source":     "web",
    }
    _save_field("point_codes", codes)

    return {
        "code":       code,
        "expires_in": expire_mins * 60,  # giây
        "points":     cfg.get("points_per_redeem", 100),
    }


# ══════════════════════════════════════════
# ROUTES — BOT (bot Discord gọi)
# ══════════════════════════════════════════

class RedeemRequest(BaseModel):
    code:    str
    user_id: int

@app.post("/code/redeem")
def redeem_code(body: RedeemRequest, _=Depends(verify_secret)):
    """
    Bot Discord gọi khi user dùng .redeem <mã>.
    Xác minh mã, kiểm tra cooldown, cộng point, trả về kết quả.
    """
    code    = body.code.strip().upper()
    user_id = body.user_id
    data    = _get_data()
    codes   = data.get("point_codes", {})
    cfg     = _get_cfg()

    # Kiểm tra cooldown
    remaining = _check_cooldown(user_id)
    if remaining is not None:
        h, m = divmod(remaining // 60, 60)
        raise HTTPException(status_code=429, detail={
            "error":     "cooldown",
            "remaining": remaining,
            "message":   f"Còn {h}h {m}m trước khi redeem tiếp",
        })

    # Kiểm tra mã
    record = codes.get(code)
    if not record:
        raise HTTPException(status_code=404, detail={"error": "invalid_code", "message": "Mã không tồn tại"})
    if record.get("used"):
        raise HTTPException(status_code=400, detail={"error": "used_code", "message": "Mã đã được sử dụng"})
    try:
        exp = datetime.fromisoformat(record["expires_at"])
        if datetime.now(timezone.utc) > exp:
            raise HTTPException(status_code=400, detail={"error": "expired_code", "message": "Mã đã hết hạn"})
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "invalid_code", "message": "Mã không hợp lệ"})

    # Kiểm tra giới hạn user
    if record.get("user_id") and record["user_id"] != 0 and record["user_id"] != user_id:
        raise HTTPException(status_code=403, detail={"error": "wrong_user", "message": "Mã này không dành cho bạn"})

    # Cộng point
    pts     = cfg.get("points_per_redeem", 100)
    pts_key = str(user_id)
    user_points = data.get("user_points", {})
    old_pts     = user_points.get(pts_key, 0)
    new_pts     = old_pts + pts
    user_points[pts_key] = new_pts

    # Đánh dấu mã đã dùng
    codes[code]["used"]    = True
    codes[code]["used_by"] = user_id
    codes[code]["used_at"] = datetime.now(timezone.utc).isoformat()

    # Ghi log
    point_log = data.get("point_log", [])
    point_log.append({
        "user_id": user_id,
        "delta":   pts,
        "reason":  f"redeem:{code}",
        "balance": new_pts,
        "time":    datetime.now(timezone.utc).isoformat(),
    })

    # Lưu tất cả vào MongoDB
    _col.update_one({"_id": "main"}, {"$set": {
        "user_points": user_points,
        "point_codes": codes,
        "point_log":   point_log,
    }}, upsert=True)

    return {
        "success":   True,
        "points":    pts,
        "new_total": new_pts,
        "message":   f"Cộng {pts} point thành công",
    }


@app.get("/user/{user_id}/points")
def get_user_points(user_id: int, _=Depends(verify_secret)):
    """Bot gọi để lấy point của user."""
    data = _get_data()
    pts  = data.get("user_points", {}).get(str(user_id), 0)
    return {"user_id": user_id, "points": pts}
