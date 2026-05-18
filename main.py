"""
backend/main.py — FastAPI backend cho hệ thống Point TUYTAM MARKET
Deploy lên Render. Kết nối cùng MongoDB Atlas với bot Discord.

LUỒNG LOOTLABS POSTBACK:
1. Bạn tạo link trên LootLabs, đặt Destination URL = website của bạn
2. LootLabs panel → Advanced → Postback URL:
   https://website-ruby.onrender.com/lootlabs/postback
3. User vượt quảng cáo → LootLabs GET /lootlabs/postback?unique_id=XXX
4. Backend tạo mã sẵn → lưu MongoDB kèm unique_id
5. LootLabs redirect user về website?unique_id=XXX
6. Frontend POST /code/generate { unique_id: XXX }
7. Backend trả mã cho user
"""

import os
import secrets
import string
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from pymongo import MongoClient
from pydantic import BaseModel
from collections import defaultdict
import time
import httpx

# ══════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════
MONGO_URI            = os.getenv("MONGO_URI")
API_SECRET           = os.getenv("API_SECRET")
DISCORD_WEBHOOK_URL  = os.getenv("DISCORD_WEBHOOK_URL", "")  # Webhook URL kênh log Discord

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

def _notify_discord(code: str, expires_at: str, unique_id: str):
    """Gửi mã vừa tạo vào kênh Discord qua webhook (fire-and-forget, không raise)."""
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        cfg         = _get_cfg()
        pts         = cfg.get("points_per_redeem", 1)
        expire_mins = cfg.get("code_expire_mins", 10)
        payload = {
            "embeds": [{
                "title": "🔑 Mã Point Mới Từ LootLabs",
                "color": 0xF1C40F,
                "fields": [
                    {"name": "🎟️ Mã",        "value": f"`{code}`",               "inline": True},
                    {"name": "💎 Point",       "value": f"**{pts} pt**",           "inline": True},
                    {"name": "⏰ Hết hạn",    "value": f"**{expire_mins} phút**", "inline": True},
                    {"name": "🔗 unique_id",  "value": f"`{unique_id[:20]}...`" if len(unique_id) > 20 else f"`{unique_id}`", "inline": False},
                ],
                "footer": {"text": "TuyTam Store  •  LootLabs Postback"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }]
        }
        with httpx.Client(timeout=5) as client:
            client.post(DISCORD_WEBHOOK_URL, json=payload)
    except Exception as e:
        print(f"[WEBHOOK] ❌ Discord notify error: {e}")

def _get_cfg() -> dict:
    data = _get_data()
    return data.get("point_cfg", {
        "points_per_redeem": 1,
        "point_value":       100,
        "cooldown_hours":    24,
        "code_expire_mins":  10,
    })

def _check_cooldown(user_id: int) -> int | None:
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
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ══════════════════════════════════════════
# ROUTE 1 — LOOTLABS POSTBACK (LootLabs gọi)
# ══════════════════════════════════════════

@app.get("/lootlabs/postback")
def lootlabs_postback(
    request:   Request,
    unique_id: str = None,
    click_id:  str = None,
    ip:        str = None,
):
    """
    LootLabs gọi endpoint này khi user hoàn thành xem quảng cáo.
    Cấu hình Postback URL trong LootLabs panel → Advanced:
    https://website-ruby.onrender.com/lootlabs/postback
    LootLabs tự thêm: ?unique_id={UNIQUE_ID}&click_id={CLICK_ID}&ip={IP}
    """
    if not unique_id:
        return {"status": "ignored", "reason": "no unique_id"}

    data    = _get_data()
    codes   = data.get("point_codes", {})
    pending = data.get("pending_codes", {})

    # Tránh tạo mã trùng nếu LootLabs gọi postback nhiều lần
    if unique_id in pending:
        return {"status": "already_created"}

    cfg         = _get_cfg()
    expire_mins = cfg.get("code_expire_mins", 10)
    code        = _gen_code()
    expires_at  = (datetime.now(timezone.utc) + timedelta(minutes=expire_mins)).isoformat()

    codes[code] = {
        "user_id":    0,
        "expires_at": expires_at,
        "used":       False,
        "source":     "lootlabs",
        "unique_id":  unique_id,
        "click_id":   click_id or "",
        "ip":         ip or request.client.host,
    }

    # Mapping unique_id → code để frontend tra cứu
    pending[unique_id] = {
        "code":       code,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": expires_at,
    }

    _col.update_one({"_id": "main"}, {"$set": {
        "point_codes":   codes,
        "pending_codes": pending,
    }}, upsert=True)

    # Thông báo Discord
    _notify_discord(code, expires_at, unique_id)

    # LootLabs cần HTTP 200 để xác nhận postback thành công
    return {"status": "ok"}


# ══════════════════════════════════════════
# ROUTE 2 — WEBSITE LẤY MÃ
# ══════════════════════════════════════════

class GenerateRequest(BaseModel):
    unique_id: str

@app.post("/code/generate")
def generate_code(body: GenerateRequest, request: Request):
    """
    Website gọi sau khi LootLabs redirect user về với ?unique_id=XXX.
    Tra cứu mã đã được tạo sẵn bởi postback và trả về.
    """
    unique_id = body.unique_id.strip()

    if not unique_id:
        raise HTTPException(status_code=400, detail={
            "error":   "missing_token",
            "message": "Bạn cần vượt link quảng cáo trước khi nhận mã.",
        })

    data    = _get_data()
    pending = data.get("pending_codes", {})
    record  = pending.get(unique_id)

    if not record:
        raise HTTPException(status_code=403, detail={
            "error":   "invalid_token",
            "message": "Token không hợp lệ hoặc đã hết hạn. Vui lòng vượt link lại.",
        })

    # Kiểm tra mã còn hạn không
    try:
        exp = datetime.fromisoformat(record["expires_at"])
        if datetime.now(timezone.utc) > exp:
            pending.pop(unique_id, None)
            _save_field("pending_codes", pending)
            raise HTTPException(status_code=403, detail={
                "error":   "invalid_token",
                "message": "Token đã hết hạn. Vui lòng vượt link lại.",
            })
    except HTTPException:
        raise
    except Exception:
        pass

    code = record["code"]
    cfg  = _get_cfg()

    # Xoá pending sau khi lấy (single-use)
    pending.pop(unique_id, None)
    _save_field("pending_codes", pending)

    try:
        exp        = datetime.fromisoformat(record["expires_at"])
        expires_in = max(0, int((exp - datetime.now(timezone.utc)).total_seconds()))
    except Exception:
        expires_in = cfg.get("code_expire_mins", 10) * 60

    return {
        "code":       code,
        "expires_in": expires_in,
        "points":     cfg.get("points_per_redeem", 1),
    }


# ══════════════════════════════════════════
# ROUTES — BOT (bot Discord gọi)
# ══════════════════════════════════════════

class RedeemRequest(BaseModel):
    code:    str
    user_id: int

@app.post("/code/redeem")
def redeem_code(body: RedeemRequest, _=Depends(verify_secret)):
    """Bot Discord gọi khi user dùng .redeem <mã>."""
    code    = body.code.strip().upper()
    user_id = body.user_id
    data    = _get_data()
    codes   = data.get("point_codes", {})
    cfg     = _get_cfg()

    remaining = _check_cooldown(user_id)
    if remaining is not None:
        h, m = divmod(remaining // 60, 60)
        raise HTTPException(status_code=429, detail={
            "error":     "cooldown",
            "remaining": remaining,
            "message":   f"Còn {h}h {m}m trước khi redeem tiếp",
        })

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

    if record.get("user_id") and record["user_id"] != 0 and record["user_id"] != user_id:
        raise HTTPException(status_code=403, detail={"error": "wrong_user", "message": "Mã này không dành cho bạn"})

    pts         = cfg.get("points_per_redeem", 1)
    pts_key     = str(user_id)
    user_points = data.get("user_points", {})
    old_pts     = user_points.get(pts_key, 0)
    new_pts     = old_pts + pts
    user_points[pts_key] = new_pts

    codes[code]["used"]    = True
    codes[code]["used_by"] = user_id
    codes[code]["used_at"] = datetime.now(timezone.utc).isoformat()

    point_log = data.get("point_log", [])
    point_log.append({
        "user_id": user_id,
        "delta":   pts,
        "reason":  f"redeem:{code}",
        "balance": new_pts,
        "time":    datetime.now(timezone.utc).isoformat(),
    })

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
    data = _get_data()
    pts  = data.get("user_points", {}).get(str(user_id), 0)
    return {"user_id": user_id, "points": pts}


@app.get("/health")
def health():
    return {"status": "ok"}
