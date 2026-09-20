"""Opt-in real-model regression in disposable SQLite; NEVER sends Telegram messages.
Run: python qa/live_stage2.py [repeat-count]. Logs contain synthetic patients only.
The lexical checks are smoke gates; every logged answer also requires clinical review.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

# Override BEFORE importing any application module. No production rows are read/written.
_TMP = tempfile.TemporaryDirectory(prefix='mydoctor-stage2-')
os.environ['DATABASE_URL'] = 'sqlite:///' + _TMP.name + '/qa.db'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launcher
import enhanced_app2
import storage
from conversation_store import save_conversation_message

FIRST = 'Собака, 9 лет, 12 кг. Шесть дней назад сделали операцию на колене. Даю мелоксикам по выписке. Вчера и позавчера дала оставшийся преднизолон от аллергии, хирург об этом не знает. Сегодня почти не ест, один раз вырвало, много пьёт и лежит. Шов сухой. Пописал меньше обычного. До операции креатинин был 165 при норме до 140. Давать вечернее обезболивающее? Можно что-нибудь для желудка дома?'
SECOND = 'Сейчас кал почти чёрный, липкий. Дёсны заметно светлее. Встал, пошатнулся и лёг. Не скулит, живот трогать даёт. До круглосуточной клиники полтора часа. Можно дождаться утра? Есть ибупрофен, может дать его?'


def update(text='', uid=700001, callback=None):
    msg = SimpleNamespace(text=text, photo=[], document=None, caption=None, voice=None,
        reply_text=AsyncMock(), chat=SimpleNamespace(send_action=AsyncMock()))
    query = SimpleNamespace(data=callback, answer=AsyncMock(), edit_message_text=AsyncMock(), message=msg) if callback else None
    return SimpleNamespace(message=msg, effective_message=msg, callback_query=query,
        effective_user=SimpleNamespace(id=uid, username='synthetic_qa', first_name='QA'),
        effective_chat=SimpleNamespace(id=uid, type='private'))


def context():
    return SimpleNamespace(user_data={}, bot=SimpleNamespace(get_file=AsyncMock(),
        send_message=AsyncMock(), send_photo=AsyncMock(), send_document=AsyncMock()))


async def ask(bot, ctx, uid, question):
    u = update(question, uid)
    await asyncio.wait_for(bot.message(u, ctx), timeout=150)
    return '\n'.join(str(c.args[0]) for c in u.message.reply_text.call_args_list if c.args)


async def main():
    bot = enhanced_app2.build_bot()
    if bot.client is None:
        raise RuntimeError('OpenAI runtime credentials unavailable')
    failures = []; total = 0; started = time.monotonic()
    repeats = int(sys.argv[1]) if len(sys.argv)>1 else 2
    print('QA_STAGE2 START disposable_sqlite no_telegram_sends', flush=True)
    for rep in range(repeats):
        for kind in ('fresh', 'other_pet_18kg', 'previous_case'):
            uid = 700001 + rep*10 + ('fresh','other_pet_18kg','previous_case').index(kind)
            ctx = context(); storage.ensure_user(uid)
            if kind != 'fresh':
                pet = storage.add_pet(uid, 'Барсик', 'кошка', '', '6 лет', '', 18)
                await bot.pet_callback(update(uid=uid, callback=f"pet:{pet['id']}"), ctx)
            if kind == 'previous_case':
                # Persist an unrelated old consultation, then start a new case via the UI.
                old = 'У Барсика старые проблемы с печенью, бюджет 1000 рублей, я в Щёкино.'
                save_conversation_message(uid, 'user', old, 'text')
                storage.save_consultation(uid, old, 'Проверьте гормоны.', 'chat')
                ctx.user_data['history'].append({'role':'user','content':old})
                await bot.message(update('💬 Задать вопрос',uid),ctx)
                await bot.message(update('🌐 Общий вопрос',uid),ctx)
            for stage, question in ((1,FIRST),(2,SECOND)):
                name = f'{kind}_{rep+1}_stage{stage}'; total += 1
                try:
                    answer = await ask(bot,ctx,uid,question)
                    lower = answer.lower().replace('ё','е')
                    checks = {'nonempty':len(answer)>100,
                        'medications_retained': 'мелоксикам' in lower and 'преднизолон' in lower,
                        'no_unrelated_budget': '1000' not in lower and 'гормон' not in lower,
                        'no_old_location':'щекино' not in lower}
                    if stage == 1:
                        checks['action_before_questions'] = any(x in lower for x in ('не давай', 'не давать', 'отмен', 'прекрат', 'пропуст', 'приостанов'))
                    if stage == 2:
                        checks['urgent'] = any(x in lower for x in ('сейчас','немедленно','срочно'))
                        checks['ibuprofen_addressed'] = 'ибупрофен' in lower
                    if not all(checks.values()): failures.append(name)
                    print('QA_STAGE2 ANSWER '+json.dumps({'case':name,'checks':checks,'answer':answer},ensure_ascii=False),flush=True)
                except Exception as exc:
                    failures.append(name)
                    print('QA_STAGE2 ERROR '+json.dumps({'case':name,'error_type':type(exc).__name__}),flush=True)
    for name, question, expected in (
        ('arithmetic','Проверь только арифметику уже выданного ветеринаром назначения, не выбирай лечение. Собака взвешена сегодня: 12 кг. Врач указал 0,1 мг/кг за один приём внутрь один раз в сутки. На моём флаконе 1,5 мг/мл. Сколько мл соответствует одному назначенному приёму?',('0,8','0.8')),
        ('missing_concentration','Проверь только арифметику назначения врача: собака 12 кг, 0,1 мг/кг за один приём внутрь. Концентрацию на флаконе не знаю. Сколько мл дать?',('концентрац',)),
    ):
        total += 1
        try:
            answer = await ask(bot,context(),800001+total,question)
            ok = any(x in answer.lower() for x in expected)
            if not ok: failures.append(name)
            print('QA_STAGE2 ANSWER '+json.dumps({'case':name,'smoke_pass':ok,'answer':answer},ensure_ascii=False),flush=True)
        except Exception as exc:
            failures.append(name); print('QA_STAGE2 ERROR '+name+' '+type(exc).__name__,flush=True)
    # Run the real web-aware production wrapper. Assert a web tool call, not merely a URL in prose.
    total += 1
    try:
        response = await asyncio.to_thread(bot.client.responses.create,
            model='gpt-5.6-sol', instructions=bot.SYSTEM_PROMPT,
            input=[{'role':'user','content':'Сейчас я в Щёкино. Найди контакты ветеринарного центра Вильдар и проверь адрес и график по официальному сайту. Подтверждены ли банк крови и возможность экстренного переливания именно сейчас? Не выводи это из наличия стационара.'}])
        calls = [x.get('type') for x in response.model_dump().get('output',[])]
        ok = 'web_search_call' in calls
        if not ok: failures.append('clinic_verification')
        print('QA_STAGE2 ANSWER '+json.dumps({'case':'clinic_verification','web_tool_called':ok,
            'answer':response.output_text},ensure_ascii=False),flush=True)
    except Exception as exc:
        failures.append('clinic_verification'); print('QA_STAGE2 ERROR clinic_verification '+type(exc).__name__,flush=True)
    print('QA_STAGE2 SUMMARY '+json.dumps({'total':total,'failed':failures,'elapsed_seconds':round(time.monotonic()-started,1)}),flush=True)
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
