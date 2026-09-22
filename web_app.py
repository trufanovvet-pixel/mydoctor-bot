import os
from flask import Flask, render_template, request, jsonify
from sqlalchemy import select
import storage

app = Flask(__name__, template_folder="web_templates", static_folder="web_static")

@app.get("/")
def home():
    return render_template("index.html")

@app.get("/app")
def cabinet():
    return render_template("cabinet.html")

@app.get("/health")
def health():
    return {"ok": True, "service": "mydoctor-web"}

@app.get("/api/link-preview")
def link_preview():
    raw = (request.args.get("telegram_id") or "").strip()
    if not raw.isdigit():
        return jsonify({"linked": False, "pets": [], "message": "Укажите Telegram ID для тестовой привязки."})
    telegram_id = int(raw)
    with storage.SessionLocal() as session:
        user = session.scalar(select(storage.User).where(storage.User.telegram_id == telegram_id))
        if user is None:
            return jsonify({"linked": False, "pets": []})
        pets = session.scalars(select(storage.Pet).where(storage.Pet.user_id == user.id).order_by(storage.Pet.created_at)).all()
        return jsonify({
            "linked": True,
            "user": {"first_name": user.first_name, "username": user.username, "plan": user.plan},
            "pets": [{"id": p.id, "name": p.name, "species": p.species, "breed": p.breed, "age": p.age, "sex": p.sex, "weight_kg": p.weight_kg} for p in pets],
        })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","8080")))
