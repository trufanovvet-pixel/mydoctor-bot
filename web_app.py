import os, uuid
from functools import wraps
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_file
from sqlalchemy import select
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import storage
from web_models import WebAccount, PetMedicalProfile, WebMessage, WebDocument
from web_ai import answer

app=Flask(__name__,template_folder="web_templates",static_folder="web_static")
app.secret_key=os.environ.get("WEB_SECRET_KEY","dev-change-me")
UPLOAD=Path(os.environ.get("WEB_UPLOAD_DIR","/tmp/mydoctor_uploads")); UPLOAD.mkdir(parents=True,exist_ok=True)
storage.Base.metadata.create_all(storage.engine)
ALLOWED={"image/jpeg","image/png","image/heic","application/pdf","application/vnd.openxmlformats-officedocument.wordprocessingml.document","application/msword"}

def login_required(fn):
 @wraps(fn)
 def inner(*a,**kw):
  if not session.get("uid"): return redirect(url_for("login"))
  return fn(*a,**kw)
 return inner

def current_account(db):
 return db.get(WebAccount,session["uid"])

def owned_pet(db,pet_id):
 acc=current_account(db)
 return db.scalar(select(storage.Pet).where(storage.Pet.id==pet_id,storage.Pet.user_id==acc.user_id))

@app.get("/")
def home(): return render_template("index.html")

@app.route("/register",methods=["GET","POST"])
def register():
 if request.method=="POST":
  email=request.form["email"].strip().lower(); password=request.form["password"]
  if len(password)<8: flash("Пароль должен быть не короче 8 символов."); return render_template("auth.html",mode="register")
  with storage.SessionLocal() as db:
   if db.scalar(select(WebAccount).where(WebAccount.email==email)): flash("Такой email уже зарегистрирован."); return render_template("auth.html",mode="register")
   user=storage.User(telegram_id=-int(uuid.uuid4().int%9_000_000_000+1_000_000_000)); db.add(user); db.flush()
   acc=WebAccount(user_id=user.id,email=email,password_hash=generate_password_hash(password));db.add(acc);db.commit();session["uid"]=acc.id
  return redirect(url_for("dashboard"))
 return render_template("auth.html",mode="register")

@app.route("/login",methods=["GET","POST"])
def login():
 if request.method=="POST":
  with storage.SessionLocal() as db:
   acc=db.scalar(select(WebAccount).where(WebAccount.email==request.form["email"].strip().lower()))
   if acc and check_password_hash(acc.password_hash,request.form["password"]): session["uid"]=acc.id; return redirect(url_for("dashboard"))
  flash("Неверный email или пароль.")
 return render_template("auth.html",mode="login")

@app.get("/logout")
def logout(): session.clear(); return redirect(url_for("home"))

@app.get("/app")
@login_required
def dashboard():
 with storage.SessionLocal() as db:
  acc=current_account(db); pets=db.scalars(select(storage.Pet).where(storage.Pet.user_id==acc.user_id).order_by(storage.Pet.created_at)).all()
  return render_template("dashboard.html",pets=pets)

@app.route("/pets/new",methods=["GET","POST"])
@login_required
def new_pet():
 if request.method=="POST":
  with storage.SessionLocal() as db:
   acc=current_account(db); pet=storage.Pet(user_id=acc.user_id,name=request.form["name"],species=request.form["species"],breed=request.form.get("breed") or None,age=request.form.get("age") or None,sex=request.form.get("sex") or None,weight_kg=float(request.form["weight"]) if request.form.get("weight") else None);db.add(pet);db.flush()
   db.add(PetMedicalProfile(pet_id=pet.id,neutered=request.form.get("neutered"),chronic_conditions=request.form.get("chronic"),allergies=request.form.get("allergies"),medications=request.form.get("medications"),surgeries=request.form.get("surgeries"),important_diagnoses=request.form.get("diagnoses")));db.commit()
  return redirect(url_for("dashboard"))
 return render_template("pet_form.html")

@app.get("/pets/<int:pet_id>")
@login_required
def pet_page(pet_id):
 with storage.SessionLocal() as db:
  pet=owned_pet(db,pet_id)
  if not pet: return ("Not found",404)
  profile=db.get(PetMedicalProfile,pet.id); docs=db.scalars(select(WebDocument).where(WebDocument.pet_id==pet.id).order_by(WebDocument.created_at.desc())).all()
  return render_template("pet.html",pet=pet,profile=profile,docs=docs)

@app.route("/chat/<int:pet_id>",methods=["GET","POST"])
@login_required
def chat(pet_id):
 with storage.SessionLocal() as db:
  pet=owned_pet(db,pet_id)
  if not pet:return ("Not found",404)
  acc=current_account(db); profile=db.get(PetMedicalProfile,pet.id)
  if request.method=="POST":
   text=request.form.get("message","").strip()
   if text:
    db.add(WebMessage(user_id=acc.user_id,pet_id=pet.id,role="user",content=text));db.commit()
    rows=db.scalars(select(WebMessage).where(WebMessage.user_id==acc.user_id,WebMessage.pet_id==pet.id).order_by(WebMessage.id.desc()).limit(40)).all()
    history=[{"role":x.role,"content":x.content} for x in reversed(rows)]
    reply=answer(history,pet,profile)
    db.add(WebMessage(user_id=acc.user_id,pet_id=pet.id,role="assistant",content=reply));db.add(storage.Consultation(user_id=acc.user_id,pet_id=pet.id,kind="web_chat",user_text=text,assistant_text=reply));db.commit()
   return redirect(url_for("chat",pet_id=pet.id))
  messages=db.scalars(select(WebMessage).where(WebMessage.user_id==acc.user_id,WebMessage.pet_id==pet.id).order_by(WebMessage.id)).all()
  return render_template("chat.html",pet=pet,messages=messages)

@app.post("/upload/<int:pet_id>")
@login_required
def upload(pet_id):
 f=request.files.get("file")
 if not f:return redirect(url_for("pet_page",pet_id=pet_id))
 with storage.SessionLocal() as db:
  pet=owned_pet(db,pet_id)
  if not pet:return ("Not found",404)
  if f.mimetype not in ALLOWED:return ("Unsupported file type",400)
  acc=current_account(db); name=secure_filename(f.filename) or "document"; path=UPLOAD/(str(uuid.uuid4())+"_"+name); f.save(path)
  db.add(WebDocument(user_id=acc.user_id,pet_id=pet.id,filename=name,mime_type=f.mimetype,storage_path=str(path)));db.commit()
 return redirect(url_for("pet_page",pet_id=pet_id))

@app.get("/document/<int:doc_id>")
@login_required
def document(doc_id):
 with storage.SessionLocal() as db:
  acc=current_account(db); doc=db.scalar(select(WebDocument).where(WebDocument.id==doc_id,WebDocument.user_id==acc.user_id))
  if not doc:return ("Not found",404)
  return send_file(doc.storage_path,download_name=doc.filename)

@app.get("/health")
def health(): return {"ok":True}
