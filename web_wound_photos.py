"""Private, idempotent diary photo analysis with the existing document tariff."""
import secrets
from datetime import datetime, timedelta
from flask import abort, redirect, session, url_for
from sqlalchemy import select, func, or_, and_
from werkzeug.exceptions import HTTPException
import billing
import storage
import wound_photos as wound
from care_models import HealthEntry, WoundPhotoAnalysis
from patient_records import OperationRecord
from web_i18n import language


def install(app, pet_for, pet_page):
    def result_url(db, entry):
        newer = db.scalar(select(func.count()).select_from(HealthEntry).where(
            HealthEntry.pet_id == entry.pet_id, or_(HealthEntry.observed_at > entry.observed_at,
                and_(HealthEntry.observed_at == entry.observed_at, HealthEntry.id > entry.id))))
        return url_for('care_pet', pid=entry.pet_id, page=newer//30+1)+'#entry-'+str(entry.id)

    @app.post('/care/photos/<int:eid>/analyse')
    def care_analyse_wound(eid):
        from web_app import client, STANDARD_MODEL
        uid, lease, now = session['uid'], secrets.token_hex(16), datetime.utcnow()
        stale_usage = None
        with storage.SessionLocal() as db:
            entry = db.get(HealthEntry, eid)
            if not entry: abort(404)
            pet = pet_for(db, entry.pet_id, lock=True)
            if entry.kind != 'postop' or not entry.photo: abort(400)
            operation = db.get(OperationRecord, entry.operation_id)
            if not operation or operation.pet_id != pet.id: abort(404)
            row = db.get(WoundPhotoAnalysis, eid)
            destination = result_url(db, entry)
            if row and row.state == 'success': return redirect(destination, code=303)
            if row and row.state == 'pending' and row.started_at > now-timedelta(minutes=10):
                return pet_page(db, pet, error=wound.PENDING, status=409)
            if row and row.usage_id: stale_usage = (row.requested_by, row.usage_id)
            previous = wound.previous_photos(db, entry)
            kwargs = wound.model_request(pet, operation, entry, previous, language(), STANDARD_MODEL)
            if not row:
                row = WoundPhotoAnalysis(entry_id=eid); db.add(row)
            row.state, row.lease, row.requested_by, row.started_at = 'pending', lease, uid, now
            row.result, row.finished_at, row.usage_id = None, None, None
            row.previous_entry_ids = [e.id for e in previous]
            row.prompt_version, row.language = wound.VERSION, language()
            pid, reported_urgency, previous_count = pet.id, entry.urgency, len(previous)
            db.commit()
        if stale_usage: billing.fail(*stale_usage)
        usage = None
        try:
            # The shared entry lease guards retries across accounts. A failed attempt
            # gets a new key; successful replays never call the model or debit again.
            usage, _ = billing.reserve(uid, 'document', 'wound-'+lease, ('wound:'+str(eid)+':'+lease).encode())
            with storage.SessionLocal() as db:
                pet_for(db, pid, lock=True)
                row = db.get(WoundPhotoAnalysis, eid)
                if row.lease != lease or row.state != 'pending': abort(409)
                row.usage_id = usage.id; db.commit()
            response = client.responses.create(**kwargs, timeout=90)
            result = wound.parse_result(response.output_text, reported_urgency, previous_count)
            with storage.SessionLocal() as db:
                # Membership can be revoked while the remote call is in flight.
                pet_for(db, pid, lock=True)
                row = db.get(WoundPhotoAnalysis, eid)
                if row.lease != lease or row.state != 'pending': abort(409)
                row.result, row.state, row.finished_at = result, 'success', datetime.utcnow()
                row.model = str(getattr(response, 'model', STANDARD_MODEL) or STANDARD_MODEL)[:120]
                # Result lives in the private care table, not a WebDocument belonging
                # to a family member who could keep accessing it after revocation.
                billing.complete(db, uid, usage.id, None, response)
                db.commit()
        except Exception as exc:
            if usage: billing.fail(uid, usage.id)
            with storage.SessionLocal() as db:
                # Serialize cleanup against other attempts and membership writes.
                db.scalar(select(storage.Pet).where(storage.Pet.id == pid).with_for_update())
                row = db.get(WoundPhotoAnalysis, eid)
                if row and row.lease == lease:
                    row.state = 'failed'; db.commit()
            if isinstance(exc, HTTPException): raise
            message = str(exc) if isinstance(exc, billing.BillingError) else wound.ERROR
            with storage.SessionLocal() as db:
                return pet_page(db, pet_for(db, pid), error=message, status=getattr(exc, 'status', 422))
        return redirect(destination, code=303)
