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

@app.get("/api/demo/pets")
def demo_pets():
    return jsonify([
        {"name":"Ваш питомец","species":"Собака или кошка","note":"Карточка здоровья, документы и история в одном месте"}
    ])

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","8080")))
