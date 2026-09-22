import os
from flask import Flask, render_template, request, jsonify, session, redirect
from sqlalchemy import select
import storage
import web_auth

app = Flask(__name__, template_folder="web_templates", static_folder="web_static")
app.secret_key = os.getenv("WEB_SECRET_KEY", os.getenv("TELEGRAM_BOT_TOKEN", "dev-only-change-me"))

@app.get("/")
def home():
    return render_template("index.html")

@app.get("/login/<token>")
def login_token(token):
    telegram_id = web_auth.consume_token(token)
    if telegram_id is None:
        return "Ссылка недействительна или уже использована. Получите новую ссылку в Telegram-боте.", 401
    session.clear()
    session["telegram_id"] = telegram_id
    session.permanent = True
    return redirect("/app")

@app.get("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.get("/app")
def cabinet():
    telegram_id = session.get("telegram_id")
    if not telegram_id:
        return render_template("cabinet.html", linked=False, user=None, pets=[])
    with storage.SessionLocal() as db:
        user = db.scalar(select(storage.User).where(storage.User.telegram_id == telegram_id))
        pets = [] if user is None else db.scalars(select(storage.Pet).where(storage.Pet.user_id == user.id).order_by(storage.Pet.created_at)).all()
        return render_template("cabinet.html", linked=True, user=user, pets=pets)

@app.get("/health")
def health():
    return {"ok": True, "service": "mydoctor-web"}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","8080")))
