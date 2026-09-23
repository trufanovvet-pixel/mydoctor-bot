import base64, io, os, secrets
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, send_file
from openai import OpenAI
from sqlalchemy import DateTime, ForeignKey, Integer, LargeBinary, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import re
import storage, knowledge
from patient_records import plain_text, CATEGORIES, DocumentLabel, classify_document
import math

app=Flask(__name__,template_folder="web_templates",static_folder="web_static",static_url_path="/static")
app.secret_key=os.getenv("FLASK_SECRET_KEY",secrets.token_hex(32))
app.config.update(MAX_CONTENT_LENGTH=20*1024*1024,SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE="Lax",SESSION_COOKIE_SECURE=True)
client=OpenAI()

class WebAccount(storage.Base):
    __tablename__="web_accounts"
    id: Mapped[int]=mapped_column(Integer,primary_key=True,autoincrement=True)
    user_id: Mapped[int]=mapped_column(ForeignKey("users.id"),unique=True,index=True,nullable=False)
    email: Mapped[str]=mapped_column(String(255),unique=True,index=True,nullable=False)
    password_hash: Mapped[str]=mapped_column(String(255),nullable=False)
    created_at: Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow,nullable=False)

class WebDocument(storage.Base):
    __tablename__="web_documents"
    id: Mapped[int]=mapped_column(Integer,primary_key=True,autoincrement=True)
    user_id: Mapped[int]=mapped_column(ForeignKey("users.id"),index=True,nullable=False)
    pet_id: Mapped[int|None]=mapped_column(ForeignKey("pets.id"),index=True,nullable=True)
    filename: Mapped[str]=mapped_column(String(255),nullable=False)
    mime_type: Mapped[str]=mapped_column(String(120),nullable=False)
    data: Mapped[bytes]=mapped_column(LargeBinary,nullable=False)
    analysis: Mapped[str|None]=mapped_column(Text,nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow,index=True,nullable=False)

storage.Base.metadata.create_all(storage.engine)

SYSTEM="""Ты — МойДоктор, ветеринарный AI-помощник для владельцев собак и кошек.\nОтвечай только обычным текстом. Никогда не используй Markdown-разметку: символы **, *, #, ###, обратные кавычки и markdown-таблицы запрещены. Для структуры используй короткие заголовки без спецсимволов и обычную нумерацию.
Работай клинически последовательно: сначала прямой ответ, затем только нужные уточнения.
Не придумывай данные, диагнозы, дозы или результаты исследований. Разделяй факт, предположение и то, что требует подтверждения.
При конкретном пациенте учитывай его карточку и историю. Общий вопрос не привязывай к питомцу без явного указания.
Срочность объясняй конкретными красными флагами. Не запугивай стабильного пациента.
Для изображений и документов сначала описывай, что действительно видно/прочитано, затем интерпретируй в контексте.
Это помощник, а не замена физикальному осмотру и процедурам, которые невозможно выполнить онлайн."""

def current():
    uid=session.get("uid")
    if not uid:return None
    with storage.SessionLocal() as db:
        return db.get(storage.User,uid)

def web_account():
    uid=session.get("uid")
    if not uid:return None
    with storage.SessionLocal() as db:
        return db.scalar(select(WebAccount).where(WebAccount.user_id==uid))

def require_login():
    if not session.get("uid"): return redirect(url_for("login",next=request.path))

def login_destination():
    target=request.args.get("next","")
    if target.startswith("/") and not target.startswith("//") and "\\" not in target and "\n" not in target and "\r" not in target:
        return target
    return url_for("cabinet")

def new_virtual_telegram_id(db):
    while True:
        value=-secrets.randbelow(8_000_000_000_000_000)-1
        if not db.scalar(select(storage.User.id).where(storage.User.telegram_id==value)): return value

def _plain(text):
    return plain_text(text)

app.jinja_env.filters["plain"] = plain_text

def _pet_requested(text, pet):
    value=(text or "").lower().replace("ё","е")
    name=(pet.name or "").lower().replace("ё","е")
    if name and re.search(rf"(?<!\w){re.escape(name)}(?:а|у|ом|е|ы|и)?(?!\w)", value): return True
    return any(x in value for x in ("мой питомец","моя собака","мой пес","мой кот","моя кошка","у моего питомца"))

def pet_context(db,user,text):
    if not user.active_pet_id:return ""
    pet=db.scalar(select(storage.Pet).where(storage.Pet.id==user.active_pet_id,storage.Pet.user_id==user.id))
    if not pet or not _pet_requested(text,pet):return ""
    return f"Пользователь явно спрашивает об этом питомце. Имя: {pet.name}; вид: {pet.species}; порода: {pet.breed or 'не указана'}; возраст: {pet.age or 'не указан'}; пол: {pet.sex or 'не указан'}; вес: {pet.weight_kg if pet.weight_kg is not None else 'не указан'} кг."

@app.get("/")
def home(): return render_template("index.html",logged=bool(session.get("uid")))

@app.route("/register",methods=["GET","POST"])
def register():
    if request.method=="POST":
        email=(request.form.get("email") or "").strip().lower(); password=request.form.get("password") or ""
        if "@" not in email or len(password)<8:
            flash("Введите корректный email и пароль не короче 8 символов.");return render_template("auth.html",mode="register")
        with storage.SessionLocal() as db:
            if db.scalar(select(WebAccount).where(WebAccount.email==email)):
                flash("Аккаунт с таким email уже существует.");return render_template("auth.html",mode="register")
            user=storage.User(telegram_id=new_virtual_telegram_id(db),first_name=(request.form.get("name") or "").strip() or None)
            db.add(user);db.flush()
            db.add(WebAccount(user_id=user.id,email=email,password_hash=generate_password_hash(password)))
            db.commit();session["uid"]=user.id
        return redirect(url_for("cabinet"))
    return render_template("auth.html",mode="register")

@app.route("/login",methods=["GET","POST"])
def login():
    if request.method=="POST":
        email=(request.form.get("email") or "").strip().lower();password=request.form.get("password") or ""
        with storage.SessionLocal() as db:
            acc=db.scalar(select(WebAccount).where(WebAccount.email==email))
            if acc and check_password_hash(acc.password_hash,password):
                session["uid"]=acc.user_id;return redirect(login_destination())
        flash("Неверный email или пароль.")
    return render_template("auth.html",mode="login")

@app.get("/logout")
def logout(): session.clear();return redirect(url_for("home"))

@app.get("/app")
def cabinet():
    gate=require_login()
    if gate:return gate
    return redirect(url_for("dashboard"))

@app.post("/pets")
def add_pet():
    gate=require_login()
    if gate:return gate
    name=(request.form.get("name") or "").strip();species=(request.form.get("species") or "").strip()
    if not name or not species: flash("Укажите имя и вид питомца."); return redirect(url_for("cabinet"))
    weight=None
    try: weight=float((request.form.get("weight") or "").replace(",", ".")) if request.form.get("weight") else None
    except ValueError: flash("Вес должен быть числом."); return redirect(url_for("cabinet"))
    if weight is not None and (not math.isfinite(weight) or weight <= 0 or weight > 300): flash("Проверьте вес питомца."); return redirect(url_for("cabinet"))
    with storage.SessionLocal() as db:
        user=db.get(storage.User,session["uid"])
        pet=storage.Pet(user_id=user.id,name=name,species=species,breed=(request.form.get("breed") or "").strip() or None,age=(request.form.get("age") or "").strip() or None,sex=(request.form.get("sex") or "").strip() or None,weight_kg=weight)
        db.add(pet);db.flush();user.active_pet_id=pet.id;db.commit()
    return redirect(url_for("pet_page",pid=pet.id))

@app.post("/pets/<int:pet_id>/active")
def active_pet(pet_id):
    gate=require_login()
    if gate:return gate
    with storage.SessionLocal() as db:
        user=db.get(storage.User,session["uid"]);pet=db.scalar(select(storage.Pet).where(storage.Pet.id==pet_id,storage.Pet.user_id==user.id))
        if pet:user.active_pet_id=pet.id;db.commit()
    return redirect(url_for("pet_page",pid=pet_id))

@app.post("/api/chat")
def chat():
    if not session.get("uid"):return jsonify({"error":"auth"}),401
    payload=request.get_json(silent=True) or {}
    text=str(payload.get("message") or "").strip()
    if len(text)>12000:return jsonify({"error":"Сообщение слишком длинное."}),413
    if not text:return jsonify({"error":"empty"}),400
    with storage.SessionLocal() as db:
        user=db.get(storage.User,session["uid"]);pctx=pet_context(db,user,text)
        scope_pet=user.active_pet_id if pctx else None
        prev=db.scalars(select(storage.Consultation).where(storage.Consultation.user_id==user.id, storage.Consultation.pet_id==scope_pet, storage.Consultation.kind=="web_chat").order_by(storage.Consultation.id.desc()).limit(6)).all()
        chat_started=session.get("chat_started_at")
        if chat_started:
            try:
                cutoff=datetime.fromisoformat(chat_started)
                prev=[item for item in prev if item.created_at>=cutoff]
            except ValueError:
                pass
        history=[]
        for item in reversed(prev):
            history += [{"role":"user","content":item.user_text},{"role":"assistant","content":item.assistant_text}]
        extra=knowledge.protocol_context(text)
        prompt=(pctx+"\n"+extra+"\nВопрос пользователя: "+text).strip()
        try:
            response=client.responses.create(model="gpt-5.6-sol",instructions=SYSTEM,input=history+[{"role":"user","content":prompt}])
            answer=_plain(response.output_text)
        except Exception:
            return jsonify({"error":"Временная ошибка медицинского помощника. Попробуйте ещё раз."}),503
        pet_for_history=user.active_pet_id if pctx else None
        db.add(storage.Consultation(user_id=user.id,pet_id=pet_for_history,kind="web_chat",user_text=text,assistant_text=answer));db.commit()
    return jsonify({"answer":answer})

@app.post("/api/chat/clear")
def clear_chat():
    if not session.get("uid"):return jsonify({"error":"auth"}),401
    session["chat_started_at"]=datetime.utcnow().isoformat()
    return jsonify({"ok":True})

@app.post("/documents")
def upload_document():
    gate=require_login()
    if gate:return gate
    f=request.files.get("file")
    if not f or not f.filename:return redirect(url_for("analyses_page"))
    data=f.read(); mime=f.mimetype or "application/octet-stream"
    if len(data)>20*1024*1024: flash("Файл слишком большой. Максимум 20 МБ."); return redirect(url_for("analyses_page"))
    allowed=mime in {"image/jpeg","image/png","image/webp","application/pdf"}
    if not allowed or not data:flash("Поддерживаются изображения и PDF.");return redirect(url_for("analyses_page"))
    pet_id=request.form.get("pet_id",type=int)
    category=request.form.get("category","auto")
    if category!="auto" and category not in CATEGORIES:return "Invalid category",400
    try:
        study_date=datetime.strptime(request.form["study_date"],"%Y-%m-%d").date() if request.form.get("study_date") else None
    except ValueError:
        flash("Проверьте дату исследования.");return redirect(url_for("analyses_page"))
    with storage.SessionLocal() as db:
        if pet_id and not db.scalar(select(storage.Pet.id).where(storage.Pet.id==pet_id,storage.Pet.user_id==session["uid"])):return "Not found",404
    analysis=None
    try:
        if mime.startswith("image/"):
            encoded=base64.b64encode(data).decode()
            inp=[{"role":"user","content":[{"type":"input_text","text":"Разбери ветеринарный документ/фото. Сначала перечисли объективно видимое, затем клиническую интерпретацию и ограничения. Не выдумывай невидимые данные."},{"type":"input_image","image_url":f"data:{mime};base64,{encoded}"}]}]
        else:
            uploaded=client.files.create(file=(secure_filename(f.filename) or "document.pdf",io.BytesIO(data),mime),purpose="user_data")
            inp=[{"role":"user","content":[{"type":"input_text","text":"Разбери ветеринарный документ. Извлеки ключевые результаты, интерпретируй их в контексте и укажи ограничения."},{"type":"input_file","file_id":uploaded.id}]}]
        response=client.responses.create(model="gpt-5.6-sol",instructions=SYSTEM,input=inp);analysis=_plain(response.output_text)
    except Exception:
        analysis="Файл сохранён. Автоматический разбор сейчас не удалось выполнить."
    with storage.SessionLocal() as db:
        user=db.get(storage.User,session["uid"])
        if pet_id and not db.scalar(select(storage.Pet.id).where(storage.Pet.id==pet_id,storage.Pet.user_id==user.id)):pet_id=None
        filename=f.filename.replace("\\","/").split("/")[-1][:255] or "Документ"
        doc=WebDocument(user_id=user.id,pet_id=pet_id,filename=filename,mime_type=mime,data=data,analysis=analysis)
        db.add(doc);db.flush()
        if category=="auto":category=classify_document(filename+"\n"+(analysis or "")[:1500])
        db.add(DocumentLabel(source="web",document_id=doc.id,category=category,study_date=study_date));db.commit()
    return redirect(url_for("analyses_page"))

@app.get("/documents/<int:doc_id>")
def document(doc_id):
    gate=require_login()
    if gate:return gate
    with storage.SessionLocal() as db:
        doc=db.scalar(select(WebDocument).where(WebDocument.id==doc_id,WebDocument.user_id==session["uid"]))
        if not doc:return "Not found",404
        return send_file(io.BytesIO(doc.data),mimetype=doc.mime_type,download_name=doc.filename,as_attachment=True)

@app.post("/feedback")
def feedback():
    gate=require_login()
    if gate:return gate
    text=(request.form.get("text") or "").strip()
    if text:
        import feedback_patch
        with storage.SessionLocal() as db:
            user=db.get(storage.User,session["uid"])
            db.add(feedback_patch.UserFeedback(user_id=user.id,telegram_id=user.telegram_id,comment=text,context_text="Web feedback"));db.commit()
    flash("Спасибо, сообщение сохранено.");return redirect(url_for("cabinet"))

@app.get("/health")
def health():return {"ok":True,"service":"mydoctor-web"}

import web_sections
web_sections.install(app, WebDocument)
