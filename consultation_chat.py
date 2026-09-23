"""Private, durable conversations between an owner and the treating doctor."""
import asyncio
import io
import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta

from flask import abort, flash, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint, func, select, update
from sqlalchemy.orm import Mapped, mapped_column

import storage
from consultation_cases import ConsultationCase, RequestAttachment, STATUSES, TYPES
from web_i18n import t


class ConsultationMessage(storage.Base):
    __tablename__ = 'consultation_messages'
    __table_args__ = (UniqueConstraint('consultation_id', 'role', 'request_key'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    consultation_id: Mapped[int] = mapped_column(ForeignKey('consultations.id'), index=True)
    role: Mapped[str] = mapped_column(String(10))
    actor_id: Mapped[int] = mapped_column(BigInteger)
    request_key: Mapped[str] = mapped_column(String(36))
    body: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class MessageFile(storage.Base):
    __tablename__ = 'consultation_message_files'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey('consultation_messages.id'), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(80))
    data: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)


class ConversationRead(storage.Base):
    __tablename__ = 'consultation_read_positions'
    consultation_id: Mapped[int] = mapped_column(ForeignKey('consultations.id'), primary_key=True)
    owner_seen: Mapped[int] = mapped_column(Integer, default=0)
    doctor_seen: Mapped[int] = mapped_column(Integer, default=0)


class MessageNotification(storage.Base):
    __tablename__ = 'consultation_message_notifications'
    message_id: Mapped[int] = mapped_column(ForeignKey('consultation_messages.id'), primary_key=True)
    state: Mapped[str] = mapped_column(String(20), default='pending')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


def unread_counts(db, role, uid=None, ids=None):
    seen = ConversationRead.doctor_seen if role == 'doctor' else ConversationRead.owner_seen
    query = select(ConsultationMessage.consultation_id, func.count(ConsultationMessage.id)).join(
        storage.Consultation, storage.Consultation.id == ConsultationMessage.consultation_id
    ).outerjoin(ConversationRead, ConversationRead.consultation_id == ConsultationMessage.consultation_id).where(
        storage.Consultation.kind == 'consult_request', ConsultationMessage.role != role,
        ConsultationMessage.id > func.coalesce(seen, 0))
    if role == 'owner':
        query = query.where(storage.Consultation.user_id == uid)
    if ids is not None:
        query = query.where(ConsultationMessage.consultation_id.in_(ids))
    return dict(db.execute(query.group_by(ConsultationMessage.consultation_id)).all())


def serialized_messages(db, rows):
    files = {}
    if rows:
        for file in db.scalars(select(MessageFile).where(MessageFile.message_id.in_([row.id for row in rows]))):
            files.setdefault(file.message_id, []).append(dict(id=file.id, filename=file.filename, url=url_for('message_file', fid=file.id)))
    return [dict(id=row.id, role=row.role, body=row.body, created_at=row.created_at.isoformat()+'Z',
                 files=files.get(row.id, [])) for row in rows]


def thread_context(db, rid, role):
    rows = list(reversed(db.scalars(select(ConsultationMessage).where(
        ConsultationMessage.consultation_id == rid).order_by(ConsultationMessage.id.desc()).limit(51)).all()))
    more = len(rows) > 50
    rows = rows[-50:]
    seen = db.get(ConversationRead, rid)
    session.setdefault('message_csrf', secrets.token_urlsafe(32))
    endpoint = 'doctor_messages' if role == 'doctor' else 'owner_messages'
    return dict(chat_role=role, chat_messages=serialized_messages(db, rows), chat_more=more,
                chat_csrf=session['message_csrf'], chat_key=str(uuid.uuid4()),
                chat_draft='', chat_url=url_for(endpoint, rid=rid),
                chat_read_url=url_for(endpoint+'_read', rid=rid),
                chat_other_seen=(seen.owner_seen if role == 'doctor' else seen.doctor_seen) if seen else 0)


def install(app):
    storage.Base.metadata.create_all(storage.engine)

    def doctor_identity():
        return app.extensions['doctor_identity']()

    def authorized(db, rid, role, lock=False):
        if role == 'doctor':
            if not doctor_identity(): abort(403)
        elif not session.get('uid'):
            abort(401)
        query = select(storage.Consultation).where(storage.Consultation.id == rid, storage.Consultation.kind == 'consult_request')
        if role == 'owner': query = query.where(storage.Consultation.user_id == session['uid'])
        row = db.scalar(query.with_for_update() if lock else query)
        if not row: abort(404)
        from web_app import WebAccount
        if not db.scalar(select(WebAccount.id).where(WebAccount.user_id == row.user_id)): abort(404)
        return row

    def csrf():
        expected = session.get('message_csrf', '')
        actual = request.headers.get('X-Message-CSRF') or request.form.get('message_csrf', '')
        if not expected or not secrets.compare_digest(expected, actual): abort(400)

    def private(response):
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    def result_error(message, code, rid, role, db):
        if request.headers.get('X-Chat-Request') == '1':
            return private(jsonify(error=t(message))), code
        context=thread_context(db,rid,role)
        context['chat_draft']=request.form.get('body','')[:10000]
        back=url_for('doctor_request' if role=='doctor' else 'consultation_request_page',rid=rid)
        return private(app.make_response(render_template('section.html',page='doctor_message_retry' if role=='doctor' else 'message_retry',
            title=t('Сообщение не отправлено'),error=t(message),back_url=back,doctor_csrf=session.get('doctor_csrf',''),statuses=STATUSES,**context))),code

    def messages(rid, role):
        with storage.SessionLocal() as db:
            record = authorized(db, rid, role, lock=request.method == 'POST')
            if request.method == 'GET':
                query = select(ConsultationMessage).where(ConsultationMessage.consultation_id == rid)
                before = request.args.get('before', type=int)
                after = request.args.get('after', 0, type=int)
                if before:
                    rows = db.scalars(query.where(ConsultationMessage.id < before).order_by(ConsultationMessage.id.desc()).limit(51)).all()
                    more = len(rows) > 50
                    rows = list(reversed(rows[:50]))
                else:
                    rows = db.scalars(query.where(ConsultationMessage.id > max(0, after)).order_by(ConsultationMessage.id).limit(51)).all()
                    more = len(rows) > 50
                    rows = rows[:50]
                seen = db.get(ConversationRead, rid)
                other_seen = (seen.owner_seen if role == 'doctor' else seen.doctor_seen) if seen else 0
                case=db.get(ConsultationCase,rid)
                return private(jsonify(messages=serialized_messages(db, rows), more=more, other_seen=other_seen,
                    workflow_status=t(STATUSES[case.status if case else 'new']),paid_confirmed=bool(case and case.paid_confirmed)))
            csrf()
            key = request.form.get('request_key', '')
            try:
                if str(uuid.UUID(key)) != key: raise ValueError()
            except (ValueError, AttributeError):
                return result_error('Обновите страницу перед отправкой сообщения.', 400, rid, role, db)
            existing = db.scalar(select(ConsultationMessage).where(ConsultationMessage.consultation_id == rid,
                ConsultationMessage.role == role, ConsultationMessage.request_key == key))
            if existing:
                saved = existing
            else:
                body = request.form.get('body', '').strip()
                uploads = [file for file in request.files.getlist('files') if file.filename]
                if not body and not uploads:
                    return result_error('Напишите сообщение или прикрепите файл.', 400, rid, role, db)
                if len(body) > 10000 or len(uploads) > 5:
                    return result_error('Сообщение — до 10 000 символов, вложения — до 5 файлов.', 400, rid, role, db)
                recent = db.scalar(select(func.count(ConsultationMessage.id)).where(ConsultationMessage.consultation_id == rid,
                    ConsultationMessage.role == role, ConsultationMessage.created_at > datetime.utcnow()-timedelta(minutes=1)))
                if recent >= 20:
                    return result_error('Слишком много сообщений. Подождите минуту и повторите.', 429, rid, role, db)
                from web_sections import verified_file
                prepared = []; size = 0
                try:
                    for file in uploads:
                        data, mime = verified_file(file)
                        size += max(len(data), file.stream.tell())
                        if size > 15*1024*1024: raise ValueError('Общий размер вложений — до 15 МБ.')
                        name = file.filename.replace('\\', '/').rsplit('/', 1)[-1]
                        name = ''.join(c for c in name if c.isprintable()).strip()[:230] or 'document'
                        # Images are normalized by verified_file; use a matching extension.
                        name = name.rsplit('.', 1)[0]+('.pdf' if mime == 'application/pdf' else '.jpg')
                        prepared.append(dict(filename=name, mime_type=mime, data=data))
                except ValueError as exc:
                    return result_error(str(exc), 400, rid, role, db)
                saved = ConsultationMessage(consultation_id=rid, role=role,
                    actor_id=doctor_identity() if role == 'doctor' else record.user_id, request_key=key, body=body)
                db.add(saved); db.flush()
                for file in prepared: db.add(MessageFile(message_id=saved.id, **file))
                if role == 'owner': db.add(MessageNotification(message_id=saved.id))
                db.commit()
            if request.headers.get('X-Chat-Request') == '1':
                return private(jsonify(message=serialized_messages(db, [saved])[0]))
            return redirect(url_for('doctor_request' if role == 'doctor' else 'consultation_request_page', rid=rid)+'#conversation', code=303)

    def mark_read(rid, role):
        with storage.SessionLocal() as db:
            authorized(db, rid, role, lock=True); csrf()
            last_id = request.form.get('last_id', type=int)
            if not last_id or not db.scalar(select(ConsultationMessage.id).where(
                ConsultationMessage.consultation_id == rid, ConsultationMessage.id == last_id)): abort(400)
            receipt = db.get(ConversationRead, rid)
            if not receipt:
                receipt = ConversationRead(consultation_id=rid, owner_seen=0, doctor_seen=0); db.add(receipt)
            attr = 'doctor_seen' if role == 'doctor' else 'owner_seen'
            setattr(receipt, attr, max(getattr(receipt, attr), last_id)); db.commit()
        return private(jsonify(ok=True))

    @app.route('/consultation/requests/<int:rid>/messages', methods=['GET', 'POST'])
    def owner_messages(rid): return messages(rid, 'owner')

    @app.route('/doctor/requests/<int:rid>/messages', methods=['GET', 'POST'])
    def doctor_messages(rid): return messages(rid, 'doctor')

    @app.post('/consultation/requests/<int:rid>/messages/read')
    def owner_messages_read(rid): return mark_read(rid, 'owner')

    @app.post('/doctor/requests/<int:rid>/messages/read')
    def doctor_messages_read(rid): return mark_read(rid, 'doctor')

    @app.get('/consultation/chat-files/<int:fid>')
    def message_file(fid):
        from flask import send_file
        with storage.SessionLocal() as db:
            file = db.get(MessageFile, fid)
            msg = db.get(ConsultationMessage, file.message_id) if file else None
            if not msg: abort(404)
            authorized(db, msg.consultation_id, 'doctor' if doctor_identity() else 'owner')
            return private(send_file(io.BytesIO(file.data), mimetype=file.mime_type, download_name=file.filename, as_attachment=True))

    @app.get('/messages/unread')
    def messages_unread():
        role = request.args.get('role', 'owner')
        if role not in ('owner', 'doctor'): abort(400)
        if role == 'doctor' and not doctor_identity(): abort(403)
        if role == 'owner' and not session.get('uid'): abort(401)
        with storage.SessionLocal() as db:
            counts = unread_counts(db, role, session.get('uid'))
        return private(jsonify(count=sum(counts.values()), requests=counts))

    @app.get('/messages')
    def owner_inbox():
        if not session.get('uid'): return redirect(url_for('login', next='/messages'))
        page = max(1, request.args.get('page', 1, type=int)); limit = 30
        with storage.SessionLocal() as db:
            last = select(ConsultationMessage.consultation_id, func.max(ConsultationMessage.created_at).label('last_at')).group_by(ConsultationMessage.consultation_id).subquery()
            rows = db.execute(select(storage.Consultation, ConsultationCase).outerjoin(ConsultationCase,
                ConsultationCase.consultation_id == storage.Consultation.id).outerjoin(last, last.c.consultation_id == storage.Consultation.id).where(
                storage.Consultation.user_id == session['uid'], storage.Consultation.kind == 'consult_request').order_by(
                func.coalesce(last.c.last_at, storage.Consultation.created_at).desc()).offset((page-1)*limit).limit(limit+1)).all()
            counts = unread_counts(db, 'owner', session['uid'], [record.id for record, _ in rows])
            return private(app.make_response(render_template('section.html', page='messages', title=t('Переписка с врачом'),
                rows=rows[:limit], unread=counts, statuses=STATUSES, page_number=page, has_more=len(rows)>limit)))


def claim_message_notification():
    now = datetime.utcnow()
    with storage.SessionLocal() as db:
        item = db.scalar(select(MessageNotification).where(MessageNotification.state != 'delivered',
            MessageNotification.next_attempt <= now).order_by(MessageNotification.message_id).limit(1))
        if not item: return None
        changed = db.execute(update(MessageNotification).where(MessageNotification.message_id == item.message_id,
            MessageNotification.state != 'delivered', MessageNotification.next_attempt <= now).values(
            state='sending', attempts=MessageNotification.attempts+1, next_attempt=now+timedelta(minutes=2)), execution_options={'synchronize_session':False})
        if changed.rowcount != 1: db.rollback(); return None
        message = db.get(ConsultationMessage, item.message_id)
        payload = (message.id, message.consultation_id)
        db.commit(); return payload


async def deliver_message_notifications(application):
    from notification_patch import _admin_chat_id
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    for _ in range(20):
        payload = await asyncio.to_thread(claim_message_notification)
        if not payload: break
        mid, rid = payload; delivered = False
        try:
            target = await asyncio.to_thread(_admin_chat_id)
            base = os.getenv('MYDOCTOR_WEB_URL', 'https://mydoctor-web-production.up.railway.app').rstrip('/')
            await application.bot.send_message(chat_id=target,
                text=f'Новое сообщение владельца на сайте МойДоктор. Заявка №{rid}.\nОтветьте в переписке на сайте.',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Открыть переписку', url=f'{base}/doctor/requests/{rid}#conversation')]]),
                read_timeout=20, write_timeout=20, connect_timeout=10)
            delivered = True
        except Exception as exc:
            logging.getLogger(__name__).warning('Message %s notification failed: %s', mid, type(exc).__name__)
        def finish():
            with storage.SessionLocal() as db:
                item = db.get(MessageNotification, mid)
                item.state = 'delivered' if delivered else 'retry'
                item.next_attempt = datetime.utcnow()+timedelta(seconds=min(300, 30*item.attempts))
                db.commit()
        await asyncio.to_thread(finish)
