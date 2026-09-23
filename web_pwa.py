"""Installable owner and doctor entry points; no patient data is cached offline."""
import io
from functools import lru_cache

from flask import abort, jsonify, redirect, render_template, request, send_file, session, url_for
from PIL import Image, ImageDraw

from consultation_cases import STATUSES
from web_i18n import t


@lru_cache(maxsize=8)
def icon_png(size, badge=False):
    # Reuse the site's paw mark, drawn at 4x resolution for crisp launcher icons.
    scale = size * 4
    canvas = Image.new('RGBA', (scale, scale), (0, 0, 0, 0) if badge else '#008486')
    draw = ImageDraw.Draw(canvas)
    def ellipse(box):
        draw.ellipse(tuple(round(n * scale) for n in box), fill='white')
    ellipse((.31, .46, .69, .76))
    for box in ((.24, .35, .37, .52), (.38, .24, .49, .44),
                (.53, .24, .64, .44), (.68, .35, .79, .52)):
        ellipse(box)
    canvas = canvas.resize((size, size), Image.Resampling.LANCZOS)
    output = io.BytesIO(); canvas.save(output, 'PNG')
    return output.getvalue()


def install(app):
    @app.get('/manifest.webmanifest')
    @app.get('/doctor.webmanifest')
    def app_manifest():
        doctor = request.path == '/doctor.webmanifest'
        english = request.args.get('lang') == 'en'
        name = 'MyDoctor' if english else 'МойДоктор'
        if doctor: name += ' · ' + ('Doctor' if english else 'Врач')
        response = jsonify(
            id='/doctor' if doctor else '/', name=name, short_name=name,
            lang='en' if english else 'ru', start_url='/doctor' if doctor else '/dashboard',
            scope='/', display='standalone', background_color='#f2f8fb', theme_color='#008486',
            description=('Your veterinary practice' if doctor else 'Your pet’s health in one place') if english
                        else ('Кабинет ветеринарного врача' if doctor else 'Здоровье питомца в одном месте'),
            icons=[dict(src=f'/app-icon/{size}.png', sizes=f'{size}x{size}', type='image/png', purpose='any maskable')
                   for size in (192, 512)],
            shortcuts=[dict(name=('Owner records' if english else 'Картотека владельцев') if doctor
                            else ('Messages' if english else 'Переписка с врачом'),
                            url='/doctor/clients' if doctor else '/messages')])
        response.mimetype = 'application/manifest+json'
        response.headers['Cache-Control'] = 'public, max-age=3600'
        return response

    @app.get('/app-icon/<int:size>.png')
    def app_icon(size):
        if size not in (96, 180, 192, 512): abort(404)
        return send_file(io.BytesIO(icon_png(size, badge=size == 96)), mimetype='image/png', max_age=86400)

    @app.get('/service-worker.js')
    def service_worker():
        response = app.send_static_file('service-worker.js')
        response.headers['Cache-Control'] = 'no-cache'
        response.headers['Service-Worker-Allowed'] = '/'
        return response

    @app.get('/install')
    @app.get('/doctor/install')
    def install_app_page():
        doctor = request.path.startswith('/doctor/')
        if doctor and not app.extensions['doctor_identity']():
            return redirect(url_for('doctor_login', next='/doctor/install'))
        if doctor:
            import secrets
            session.setdefault('doctor_csrf', secrets.token_urlsafe(32))
        response = app.make_response(render_template('section.html',
            page='doctor_install' if doctor else 'install', title=t('Приложение на телефоне'),
            statuses=STATUSES, doctor_csrf=session.get('doctor_csrf', ''),
            install_role='doctor' if doctor else 'owner'))
        response.headers['Cache-Control'] = 'private, no-store'
        return response

    @app.after_request
    def private_app_pages(response):
        # Keep browsers/proxies from retaining medical pages across account changes.
        # The service worker only caches explicitly public assets.
        if response.mimetype == 'text/html' and (session.get('uid') or request.path.startswith('/doctor')):
            response.headers['Cache-Control'] = 'private, no-store'
        return response
