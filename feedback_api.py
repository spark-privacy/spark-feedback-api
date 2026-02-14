# feedback_api.py
import os
import re
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# =========================
# CONFIG (env vars)
# =========================
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()

# Куда присылать письма (можно оставить founder)
TO_EMAIL = os.getenv("TO_EMAIL", "founder@sparkheritage.online").strip()

# От кого (ВАЖНО: Resend требует верифицированный домен отправителя)
# Примеры: "hello@sparkheritage.online" или "info@sparkheritage.online"
FROM_EMAIL = os.getenv("FROM_EMAIL", "hello@sparkheritage.online").strip()

# Тема письма (можно менять)
SUBJECT_PREFIX = os.getenv("SUBJECT_PREFIX", "[Смысл слова] Feedback").strip()

# Доп. защита
SHARED_SECRET = os.getenv("SHARED_SECRET", "").strip()  # если задашь — будет проверять заголовок X-Api-Key

# CORS (для Flutter обычно ок поставить '*', но лучше потом ограничить)
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").strip()

# Простейший антиспам-рейтлимит (в памяти, достаточно для MVP)
RATE_LIMIT_PER_IP_PER_MIN = int(os.getenv("RATE_LIMIT_PER_IP_PER_MIN", "20"))

# =========================
# APP
# =========================
app = FastAPI(title="Spark Heritage Feedback API", version="1.0.0")

if ALLOWED_ORIGINS == "*":
    origins = ["*"]
else:
    origins = [o.strip() for o in ALLOWED_ORIGINS.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

_ip_hits: Dict[str, list] = {}  # { ip: [timestamps...] }


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _rate_limit(ip: str) -> None:
    if RATE_LIMIT_PER_IP_PER_MIN <= 0:
        return

    t = _now_utc().timestamp()
    bucket = _ip_hits.get(ip, [])
    # оставляем только последнюю минуту
    bucket = [x for x in bucket if (t - x) <= 60.0]
    if len(bucket) >= RATE_LIMIT_PER_IP_PER_MIN:
        raise HTTPException(status_code=429, detail="Too many requests")
    bucket.append(t)
    _ip_hits[ip] = bucket


def _clean(s: str, max_len: int) -> str:
    s = (s or "").strip()
    s = re.sub(r"\r\n", "\n", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    if len(s) > max_len:
        s = s[:max_len].rstrip() + "…"
    return s


def _valid_email(email: str) -> bool:
    email = (email or "").strip()
    if not email:
        return False
    # мягкая проверка
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email))


class FeedbackPayload(BaseModel):
    # пользовательские поля
    name: Optional[str] = Field(default="", max_length=60)
    email: Optional[str] = Field(default="", max_length=120)
    message: str = Field(min_length=4, max_length=4000)

    # системные поля (подставим из приложения)
    app_name: Optional[str] = Field(default="Смысл слова и контекста", max_length=120)
    app_version: Optional[str] = Field(default="", max_length=60)
    build_number: Optional[str] = Field(default="", max_length=60)
    platform: Optional[str] = Field(default="", max_length=40)  # android/ios
    device: Optional[str] = Field(default="", max_length=140)
    os_version: Optional[str] = Field(default="", max_length=80)
    locale: Optional[str] = Field(default="ru", max_length=16)

    # антиспам (скрытое поле, должно быть пустым)
    honey: Optional[str] = Field(default="", max_length=40)


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True}


@app.post("/feedback")
async def feedback(payload: FeedbackPayload, request: Request) -> Dict[str, Any]:
    # 1) Проверка секрета (опционально)
    if SHARED_SECRET:
        api_key = (request.headers.get("X-Api-Key") or "").strip()
        if api_key != SHARED_SECRET:
            raise HTTPException(status_code=401, detail="Unauthorized")

    # 2) Антиспам: honey должен быть пустым
    if (payload.honey or "").strip():
        # не палим спамеру — отвечаем как будто ок
        return {"ok": True}

    # 3) Рейтлимит
    ip = (request.client.host if request.client else "unknown") or "unknown"
    _rate_limit(ip)

    # 4) Валидация
    msg = _clean(payload.message, 4000)
    if len(msg) < 4:
        raise HTTPException(status_code=400, detail="Message too short")

    user_email = _clean(payload.email or "", 120)
    if user_email and not _valid_email(user_email):
        raise HTTPException(status_code=400, detail="Invalid email")

    name = _clean(payload.name or "", 60)

    # 5) Готовим письмо
    ts = _now_utc().strftime("%Y-%m-%d %H:%M UTC")

    subject = f"{SUBJECT_PREFIX} · {payload.platform or 'unknown'} · v{payload.app_version or '-'}"

    meta_lines = [
        f"Time: {ts}",
        f"App: {payload.app_name or ''}",
        f"Version: {payload.app_version or ''} ({payload.build_number or ''})",
        f"Platform: {payload.platform or ''}",
        f"Device: {payload.device or ''}",
        f"OS: {payload.os_version or ''}",
        f"Locale: {payload.locale or ''}",
        f"IP: {ip}",
    ]

    from_block = []
    if name:
        from_block.append(f"Name: {name}")
    if user_email:
        from_block.append(f"Email: {user_email}")

    text_body = (
        "Новая обратная связь из приложения\n"
        "================================\n\n"
        + ("\n".join(from_block) + "\n\n" if from_block else "")
        + "Сообщение:\n"
        + msg
        + "\n\n---\n"
        + "\n".join(meta_lines)
        + "\n"
    )

    html_body = f"""
    <div style="font-family: -apple-system, Segoe UI, Roboto, Arial; line-height:1.4;">
      <h2 style="margin:0 0 8px 0;">Новая обратная связь из приложения</h2>
      {"".join([f"<div><b>{line.split(':')[0]}:</b> {line.split(':',1)[1].strip()}</div>" for line in from_block]) if from_block else ""}
      <div style="margin-top:14px; padding:14px; border-radius:12px; background:#f6f7f9; white-space:pre-wrap;">{msg}</div>
      <div style="margin-top:14px; color:#555; font-size:13px;">
        <div style="margin-bottom:6px;"><b>Meta</b></div>
        {"".join([f"<div>{m}</div>" for m in meta_lines])}
      </div>
    </div>
    """.strip()

    # 6) Отправляем через Resend
    if not RESEND_API_KEY:
        raise HTTPException(status_code=500, detail="RESEND_API_KEY is not set")

    payload_resend = {
        "from": FROM_EMAIL,
        "to": [TO_EMAIL],
        "subject": subject,
        "text": text_body,
        "html": html_body,
        # если хочешь — можно включить reply_to, чтобы отвечать прямо пользователю
        **({"reply_to": user_email} if user_email else {}),
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                json=payload_resend,
            )
            if r.status_code >= 400:
                raise HTTPException(status_code=502, detail=f"Resend error: {r.status_code} {r.text}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Send failed: {e}")

    return {"ok": True}
