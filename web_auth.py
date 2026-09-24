import secrets
import hashlib
from datetime import datetime, timedelta
from sqlalchemy import BigInteger, DateTime, Integer, String, select, update
from sqlalchemy.orm import Mapped, mapped_column
import storage

class WebLoginToken(storage.Base):
    __tablename__ = "web_login_tokens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

def init():
    from billing import init_schema
    init_schema()

def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()

def create_token(telegram_id: int, minutes: int = 10) -> str:
    init()
    token = secrets.token_urlsafe(32)
    with storage.SessionLocal() as session:
        session.add(WebLoginToken(token=digest(token), telegram_id=telegram_id, expires_at=datetime.utcnow()+timedelta(minutes=minutes)))
        session.commit()
    return token

def peek_token(token: str) -> int | None:
    if not isinstance(token, str) or not 20 <= len(token) <= 100:
        return None
    with storage.SessionLocal() as session:
        return session.scalar(select(WebLoginToken.telegram_id).where(WebLoginToken.token==digest(token),
            WebLoginToken.used_at.is_(None), WebLoginToken.expires_at > datetime.utcnow()))

def consume_token(token: str) -> int | None:
    init()
    now=datetime.utcnow()
    with storage.SessionLocal() as session:
        telegram_id=session.scalar(update(WebLoginToken).where(WebLoginToken.token==digest(token),
            WebLoginToken.used_at.is_(None),WebLoginToken.expires_at>now).values(used_at=now).returning(WebLoginToken.telegram_id))
        session.commit()
        return telegram_id

def install(app):
    from flask import abort, make_response, redirect, render_template, request, session, url_for
    import billing

    @app.route('/login/<token>', methods=['GET', 'POST'])
    def telegram_login(token):
        telegram_id = peek_token(token)
        if telegram_id is None:
            response = make_response(render_template('telegram_login.html', expired=True), 410)
        else:
            with storage.SessionLocal() as db:
                user = db.scalar(select(storage.User).where(storage.User.telegram_id==telegram_id))
                if not user:
                    abort(410)
                uid, name = user.id, user.first_name or user.username or 'Telegram'
            target = request.values.get('next', '/dashboard')
            if target not in ('/billing', '/dashboard'):
                abort(400)
            transfer = request.values.get('transfer', 'mir')
            if transfer not in billing.CARD_TRANSFERS:
                abort(400)
            destination = url_for('billing_page', transfer=transfer) if target == '/billing' else target
            session.setdefault('telegram_login_csrf', secrets.token_urlsafe(32))
            if request.method == 'POST':
                if not secrets.compare_digest(request.form.get('login_csrf', ''), session['telegram_login_csrf']):
                    abort(400)
                if consume_token(token) != telegram_id:
                    abort(410)
                from web_push import revoke_browser_subscriptions
                revoke_browser_subscriptions('owner')
                session.clear()
                session['uid'] = uid
                session.permanent = request.form.get('remember') == '1'
                response = redirect(destination, code=303)
            elif session.get('uid') == uid:
                response = redirect(destination, code=303)
            else:
                response = make_response(render_template('telegram_login.html', expired=False, name=name,
                    different_account=bool(session.get('uid') and session['uid'] != uid), next_target=target,
                    transfer=transfer, login_csrf=session['telegram_login_csrf']))
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Robots-Tag'] = 'noindex, nofollow'
        return response
