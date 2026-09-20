"""Opt-in synthetic handler checks; isolated database and mocked Telegram sends.

Lexical gates are preliminary. Read every logged answer for clinical accuracy.
"""
import asyncio
import json

from live_stage2 import ask, context, enhanced_app2

QUESTION = ('Кошка 8 лет, 4 кг, диабет. Пять дней даю Bexacat по назначению врача. '
            'Сегодня почти не ест, лежит, один раз вырвало. Сахар сейчас 5,8 ммоль/л. '
            'Можно успокоиться раз сахар нормальный, дать вечернюю таблетку и ждать утра?')


async def main():
    bot = enhanced_app2.build_bot()
    if bot.client is None:
        raise RuntimeError('OpenAI runtime credentials unavailable')
    failures = []
    print('QA_EUGLYCEMIC START isolated_sqlite no_telegram_sends', flush=True)
    for repeat in range(3):
        try:
            answer = await ask(bot, context(), 900001 + repeat, QUESTION)
            lower = answer.lower().replace('ё', 'е')
            checks = {
                'urgent': any(w in lower for w in ('сейчас', 'срочно', 'немедленно')),
                'ketoacidosis': 'кетоацидоз' in lower,
                'drug_addressed': any(w in lower for w in ('bexacat', 'бексакат', 'бексаглифлозин')),
                'dose_action': any(w in lower for w in ('не давай', 'не давать', 'отмен', 'прекрат', 'приостанов', 'пропуст')),
            }
            if not all(checks.values()):
                failures.append(repeat + 1)
            print('QA_EUGLYCEMIC ANSWER ' + json.dumps({'repeat': repeat + 1,
                  'checks': checks, 'answer': answer}, ensure_ascii=False), flush=True)
        except Exception as exc:
            failures.append(repeat + 1)
            print('QA_EUGLYCEMIC ERROR ' + type(exc).__name__, flush=True)
    print('QA_EUGLYCEMIC SUMMARY ' + json.dumps({'total': 3, 'failed': failures}), flush=True)
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
