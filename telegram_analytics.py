"""Observe handled private interactions; Telegram cannot report silent chat opens."""
import asyncio
from datetime import datetime
from functools import wraps
import secrets
import time
import analytics as a
import storage
from notification_patch import _admin_user_id

MENU = {'🐾 Мои питомцы':'pets_open', '🧪 Анализы и документы':'analyses_open',
        '🛡 Профилактика':'prevention_open', '⬅️ Профилактика':'prevention_open',
        '💬 Задать вопрос':'assistant_open', '🌐 Общий вопрос':'assistant_open',
        '👨‍⚕️ Записаться на консультацию':'consultation_open', '💳 Тариф и оплаты':'pricing_open', '💳 Оплата и баланс':'pricing_open'}


def install(bot):
    a.migrate()
    a.install_hooks()
    def wrap(handler, name):
        @wraps(handler)
        async def observed(update, ctx, *args, **kwargs):
            if a.context.get().get('source') == 'telegram':
                return await handler(update, ctx, *args, **kwargs)
            user = getattr(update, 'effective_user', None)
            if not user or getattr(update.effective_chat,'type',None) != 'private' or user.id == _admin_user_id():
                return await handler(update, ctx, *args, **kwargs)
            opened_at = datetime.utcnow()
            now = time.time()
            state = ctx.user_data
            new_session = not state.get('_analytics_sid') or now-state.get('_analytics_seen',0)>1800
            if new_session:
                state['_analytics_sid'] = secrets.token_hex(16)
            state['_analytics_seen'] = now
            sid = state['_analytics_sid']
            token = a.context.set({'source':'telegram','session_id':sid})
            try:
                account = await asyncio.to_thread(storage.ensure_user, user.id, user.username, user.first_name)
                uid = account['id']
                text = (getattr(update.effective_message,'text',None) or '').strip()
                event_name = MENU.get(text)
                if name == 'payment_command' or text in ('💳 Тарифы и оплата','💳 Баланс и оплата'): event_name='pricing_open'
                def navigation():
                    rows = []
                    if new_session:
                        rows.extend([a.row('app_open',uid,key=f'session:{sid}:telegram',at=opened_at),
                                     a.row('login',uid,key=f'login:{sid}:telegram')])
                    if event_name: rows.append(a.row(event_name,uid))
                    a.track_batch(rows)
                await asyncio.to_thread(navigation)
                return await handler(update, ctx, *args, **kwargs)
            finally:
                a.context.reset(token)
        return observed
    for name in ('start','menu_command','message','voice_message','media','pet_callback','add_pet_callback','consult_callback','payment_command'):
        if hasattr(bot, name): setattr(bot, name, wrap(getattr(bot,name),name))
