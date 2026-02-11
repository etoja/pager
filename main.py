import os
import sqlite3
import requests
from fastapi import FastAPI, Request, Header, HTTPException

from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
PAGER_URL = os.getenv("PAGER_INBOUND_URL", "https://pager.co.ua/api/webhooks/custom")
PAGER_KEY = os.getenv("PAGER_CHANNEL_KEY")

if not TG_BOT_TOKEN or not PAGER_KEY:
    raise RuntimeError("Missing TG_BOT_TOKEN or PAGER_CHANNEL_KEY in environment variables")

app = FastAPI()

db = sqlite3.connect("state.db", check_same_thread=False)
db.execute("""CREATE TABLE IF NOT EXISTS map (
  client_external_id TEXT PRIMARY KEY,
  tg_chat_id INTEGER NOT NULL
)""")
db.commit()

def upsert_map(client_external_id: str, tg_chat_id: int):
    db.execute(
        "INSERT INTO map(client_external_id, tg_chat_id) VALUES(?, ?) "
        "ON CONFLICT(client_external_id) DO UPDATE SET tg_chat_id=excluded.tg_chat_id",
        (client_external_id, tg_chat_id)
    )
    db.commit()

def get_chat_id(client_external_id: str):
    row = db.execute("SELECT tg_chat_id FROM map WHERE client_external_id=?", (client_external_id,)).fetchone()
    return int(row[0]) if row else None

def pager_post(payload: dict):
    headers = {"Content-Type": "application/json", "x-channel-key": PAGER_KEY}
    r = requests.post(PAGER_URL, json=payload, headers=headers, timeout=15)
    if r.status_code >= 400:
        print("Pager inbound error:", r.status_code, r.text[:800])
    return r

def client_external_id_from_user(user_id: int) -> str:
    return f"tg_user:{user_id}"

def message_external_id(user_id: int, chat_id: int, message_id: int) -> str:
    return f"tg_msg:{user_id}:{chat_id}:{message_id}"

async def on_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return

    # Только private 1:1
    if chat.type != "private":
        return

    text = update.message.text or update.message.caption or ""
    if not text:
        # На старте делаем текст-only (надёжно)
        return

    c_ext = client_external_id_from_user(user.id)
    upsert_map(c_ext, chat.id)

    payload = {
        "event": "message.created",
        "client": {
            "externalId": c_ext,
            "name": (user.full_name or "").strip() or None,
        },
        "message": {
            "externalId": message_external_id(user.id, chat.id, update.message.message_id),
            "direction": "incoming",
            "text": text,
            "attachments": []
        }
    }
    if payload["client"]["name"] is None:
        payload["client"].pop("name", None)

    pager_post(payload)

tg_app = Application.builder().token(TG_BOT_TOKEN).build()
tg_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_user_message))

@app.on_event("startup")
async def startup():
    await tg_app.initialize()
    await tg_app.start()

@app.on_event("shutdown")
async def shutdown():
    await tg_app.stop()
    await tg_app.shutdown()

@app.post("/telegram/webhook")
async def telegram_webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}

@app.post("/pager/outbound")
async def pager_outbound(request: Request, x_channel_key: str = Header(None)):
    if x_channel_key != PAGER_KEY:
        raise HTTPException(status_code=401, detail="bad x-channel-key")

    payload = await request.json()
    if payload.get("event") != "message.created":
        return {"externalMessageId": "ignored"}

    client_obj = payload.get("client") or {}
    msg_obj = payload.get("message") or {}

    c_ext = client_obj.get("externalId")
    if not c_ext:
        raise HTTPException(status_code=400, detail="missing client.externalId")

    chat_id = get_chat_id(c_ext)
    if not chat_id:
        raise HTTPException(status_code=400, detail="unknown client.externalId (no mapping yet)")

    text = (msg_obj.get("text") or "").strip()
    atts = msg_obj.get("attachments") or []

    sent = None
    if text:
        sent = await tg_app.bot.send_message(chat_id=chat_id, text=text)

    # Вложения пока шлём ссылками (надёжно)
    urls = []
    for a in atts[:20]:
        url = ((a.get("payload") or {}).get("url"))
        if url:
            urls.append(url)
    if urls:
        sent2 = await tg_app.bot.send_message(chat_id=chat_id, text="\n".join(urls))
        sent = sent2 if not sent else sent

    external_id = f"bot:{chat_id}:{sent.message_id}" if sent else f"pager:{msg_obj.get('pagerMessageId','')}"
    return {"externalMessageId": external_id}
