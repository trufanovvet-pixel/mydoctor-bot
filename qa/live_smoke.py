"""Opt-in Railway pre-deploy check. Synthetic cases only; never sends Telegram messages.

Uses existing runtime credentials without printing them. No customer rows are changed.
Run once with: python qa/live_smoke.py
"""
import asyncio
import base64
import io
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw
from openai import OpenAI
from sqlalchemy import func, select
from telegram import Bot

import launcher  # install the exact production patch order
import enhanced_app2
import knowledge
import storage


def minimal_pdf():
    text = b'BT /F1 16 Tf 40 740 Td (QA TEST DOG - WEIGHT 10 KG) Tj 0 -30 Td (Hemoglobin 130 g/L - reference 120 to 180) Tj ET'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        b'<< /Length ' + str(len(text)).encode() + b' >>\nstream\n' + text + b'\nendstream']
    result = b'%PDF-1.4\n'; offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(result)); result += str(number).encode()+b' 0 obj\n'+obj+b'\nendobj\n'
    xref=len(result)
    result += b'xref\n0 6\n0000000000 65535 f \n'
    result += b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
    return result + f'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF'.encode()


def media_case(pdf=False):
    if pdf:
        part={'type':'input_file','filename':'qa_synthetic.pdf',
              'file_data':'data:application/pdf;base64,'+base64.b64encode(minimal_pdf()).decode()}
    else:
        picture=Image.new('RGB',(1400,600),'white'); draw=ImageDraw.Draw(picture)
        draw.text((50,100),'QA TEST DOG - WEIGHT 10 KG\nHemoglobin 130 g/L\nReference 120 to 180 g/L',fill='black',font_size=40)
        buffer=io.BytesIO(); picture.save(buffer,format='PNG')
        part={'type':'input_image','image_url':'data:image/png;base64,'+base64.b64encode(buffer.getvalue()).decode(),'detail':'high'}
    return [{'role':'user','content':[part,{'type':'input_text','text':'Прочитай только гемоглобин и референс с этого учебного бланка. Не добавляй другие показатели.'}]}]


def main():
    start=time.monotonic(); failures=[]
    bot=enhanced_app2.build_bot()
    if bot.client is None or not bot.TOKEN:
        raise RuntimeError('Required runtime credentials are absent')
    print('QA_LIVE START protocols='+str(len(knowledge.PROTOCOLS)),flush=True)

    async def telegram_probe():
        async with Bot(bot.TOKEN) as telegram:
            me=await telegram.get_me()
            webhook=await telegram.get_webhook_info()
            assert me.is_bot and not webhook.url, 'Polling bot must not have a webhook'
        print('QA_LIVE PASS telegram_identity_and_polling_mode',flush=True)
    asyncio.run(telegram_probe())
    with storage.SessionLocal() as session:
        counts={table:session.scalar(select(func.count()).select_from(model)) for table, model in
                [('users',storage.User),('pets',storage.Pet),('consultations',storage.Consultation)]}
    print('QA_LIVE PASS postgres_readonly '+json.dumps(counts),flush=True)

    cases=[
        ('bronchoscopy','Что такое бронхоскопия?',('бронх','дыхатель')),
        ('cryosurgery','Можно удалить неизвестную шишку жидким азотом без исследования?',('исслед','гистолог','биопс','цитолог','диагност')),
        ('emergency','Кот 5 лет несколько часов тужится в лотке, мочи нет совсем, кричит от боли. Что делать?',('сроч','немедлен','неотлож')),
        ('diagnostics','Чем отличается общий анализ крови от биохимии?',('кров','биохим')),
        ('photo',media_case(),('130',)),
        ('pdf',media_case(True),('130',)),
        ('clinic_search','Найди ветеринарную клинику в Туле: дай официальный сайт и адрес, проверь веб-поиском.',('http',)),
    ]
    for name, question, expected in cases:
        try:
            response=bot.client.responses.create(model='gpt-5.6-sol', instructions=bot.SYSTEM_PROMPT,
                input=question if isinstance(question,list) else [{'role':'user','content':question}],
                max_output_tokens=1400,timeout=75)
            answer=(response.output_text or '').strip()
            assert answer and any(word in answer.lower() for word in expected), 'Expected content absent'
            if name=='clinic_search':
                assert any(item.get('type')=='web_search_call' for item in response.model_dump().get('output',[])), 'No actual web search call'
            print('QA_LIVE PASS '+name+' '+json.dumps(answer[:2600],ensure_ascii=False),flush=True)
        except Exception as exc:
            failures.append(name)
            print('QA_LIVE FAIL '+name+' '+type(exc).__name__,flush=True)

    try:
        # Round-trip a synthetic utterance; no user audio or customer data is read.
        direct=OpenAI(timeout=60,max_retries=1)
        speech=direct.audio.speech.create(model='gpt-4o-mini-tts',voice='alloy',
            input='У собаки Грома рвота со вчерашнего дня.',response_format='wav')
        sound=io.BytesIO(speech.read()); sound.name='qa_synthetic.wav'
        transcript=bot.client.audio.transcriptions.create(model='gpt-4o-mini-transcribe',file=sound,language='ru')
        assert 'рвот' in transcript.text.lower()
        print('QA_LIVE PASS voice_transcription',flush=True)
    except Exception as exc:
        failures.append('voice_transcription')
        print('QA_LIVE FAIL voice_transcription '+type(exc).__name__,flush=True)
    print('QA_LIVE SUMMARY '+json.dumps({'failed':failures,'elapsed_seconds':round(time.monotonic()-start,1)},ensure_ascii=False),flush=True)
    return 1 if failures else 0


if __name__=='__main__':
    raise SystemExit(main())
