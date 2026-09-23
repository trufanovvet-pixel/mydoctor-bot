"""Explicit UI translations; user records are never translated by replacement."""
import json
from pathlib import Path
from urllib.parse import urlsplit
from flask import g, request, session, redirect, url_for, abort
import storage
from owner_profile import OwnerProfile

EN = json.loads((Path(__file__).parent / 'web_translations_en.json').read_text())


def language():
    return getattr(g, 'language', 'ru')


def t(text):
    return EN.get(text, text) if language() == 'en' else text


def ai_language():
    return ('\nReply in English. Use plain text. The interface language is English. '
            'Keep patient names and reported findings unchanged. Follow an explicit request for another language.'
            if language() == 'en' else '\nОтвечай по-русски, если пользователь явно не попросил другой язык.')


def install(app):
    @app.before_request
    def choose_language():
        chosen = request.cookies.get('language')
        if chosen not in ('ru', 'en') and session.get('uid') and request.endpoint != 'static':
            with storage.SessionLocal() as db:
                profile = db.get(OwnerProfile, session['uid'])
                chosen = profile.language if profile else None
        g.language = chosen if chosen in ('ru', 'en') else 'ru'

    @app.context_processor
    def localization():
        return {'t': t, 'language': language(), 'ui_messages': EN if language() == 'en' else {}}

    @app.get('/language/<lang>')
    def set_language(lang):
        if lang not in ('ru', 'en'):
            abort(404)
        if session.get('uid'):
            with storage.SessionLocal() as db:
                profile = db.get(OwnerProfile, session['uid'])
                if profile:
                    profile.language = lang
                    db.commit()
        target = request.args.get('next', '/')
        parsed = urlsplit(target)
        if not target.startswith('/') or target.startswith('//') or parsed.netloc or parsed.scheme or any(c in target for c in ('\\', '\r', '\n')):
            target = url_for('home')
        response = redirect(target)
        response.set_cookie('language', lang, max_age=365*24*60*60, secure=True, httponly=True, samesite='Lax')
        return response
