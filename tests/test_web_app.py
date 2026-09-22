import io
import os
import tempfile
from unittest.mock import patch, MagicMock

os.environ.setdefault("DATABASE_URL", "sqlite:///" + tempfile.gettempdir() + "/mydoctor-web-test.db")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import storage
import web_app


def reset_db():
    storage.Base.metadata.create_all(storage.engine)
    with storage.engine.begin() as c:
        for table in reversed(storage.Base.metadata.sorted_tables):
            c.execute(table.delete())


def register(client,email="qa@example.com"):
    return client.post("/register",data={"email":email,"password":"password123","name":"QA"},follow_redirects=True)


def test_register_login_and_relogin():
    reset_db(); c=web_app.app.test_client()
    assert register(c).status_code==200
    assert c.get("/logout").status_code==302
    assert c.post("/login",data={"email":"qa@example.com","password":"password123"},follow_redirects=True).status_code==200


def test_pet_validation_and_isolation():
    reset_db(); a=web_app.app.test_client(); b=web_app.app.test_client(); register(a,"a@x.ru"); register(b,"b@x.ru")
    a.post("/pets",data={"name":"Гром","species":"Собака","weight":"-1"})
    with a.session_transaction() as s: uid=s["uid"]
    with storage.SessionLocal() as db: assert db.query(storage.Pet).filter_by(user_id=uid).count()==0
    a.post("/pets",data={"name":"Гром","species":"Собака","weight":"50"})
    with storage.SessionLocal() as db: pet=db.query(storage.Pet).filter_by(user_id=uid).first()
    b.post(f"/pets/{pet.id}/active")
    with b.session_transaction() as s: buid=s["uid"]
    with storage.SessionLocal() as db: assert db.get(storage.User,buid).active_pet_id is None


def test_general_question_does_not_pull_active_pet():
    reset_db(); c=web_app.app.test_client(); register(c); c.post("/pets",data={"name":"Гром","species":"Собака","weight":"50"})
    assert web_app._pet_requested("Как проводится TPLO у собак?", MagicMock(name="Гром")) is False


def test_named_pet_does_use_context():
    assert web_app._pet_requested("Грому назначили TPLO", MagicMock(name="Гром")) is True


def test_markdown_removed():
    assert web_app._plain("### Заголовок\n**текст**")=="Заголовок\nтекст"


def test_bad_upload_does_not_crash():
    reset_db(); c=web_app.app.test_client(); register(c)
    r=c.post("/documents",data={"file":(io.BytesIO(b"bad"),"x.exe")},content_type="multipart/form-data",follow_redirects=True)
    assert r.status_code==200


def test_document_idor_blocked():
    reset_db(); a=web_app.app.test_client(); b=web_app.app.test_client(); register(a,"a@x.ru"); register(b,"b@x.ru")
    with a.session_transaction() as s: auid=s["uid"]
    with storage.SessionLocal() as db:
        d=web_app.WebDocument(user_id=auid,pet_id=None,filename="a.pdf",mime_type="application/pdf",data=b"x",analysis="x");db.add(d);db.commit();did=d.id
    assert b.get(f"/documents/{did}").status_code==404


def test_ai_failure_is_controlled():
    reset_db(); c=web_app.app.test_client(); register(c)
    with patch.object(web_app.client.responses,"create",side_effect=RuntimeError("offline")):
        r=c.post("/api/chat",json={"message":"У собаки рвота"})
    assert r.status_code==503


def test_empty_and_too_long_chat():
    reset_db(); c=web_app.app.test_client(); register(c)
    assert c.post("/api/chat",json={"message":""}).status_code==400
    assert c.post("/api/chat",json={"message":"x"*12001}).status_code==413
