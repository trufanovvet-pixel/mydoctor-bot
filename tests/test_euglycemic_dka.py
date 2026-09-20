import json

import knowledge


CASES = (
    'Кошка на Bexacat, сахар 5,8, со вчера не ест и вялая. Ждать утра?',
    'Даю кошке бексакат от диабета. Сахар нормальный, но она не ест и лежит.',
    'Кот принимает бексаглифлозин, стал вялым и отказывается от еды, глюкоза в норме.',
)


def test_sick_bexacat_cats_retrieve_dka_without_hyperglycemia():
    for question in CASES:
        assert any(p.get('id') == 'dka_v1' for p in knowledge.match_protocols(question))


async def test_euglycemic_warning_and_source_reach_actual_model_request(bot, ctx, update, raw):
    await bot.message(update(CASES[0]), ctx)
    request = json.dumps(raw.responses.calls[-1], ensure_ascii=False)
    assert 'Нормальная глюкоза не исключает' in request
    assert 'https://my.elanco.com/us/bexacat' in request
    assert CASES[0] in request
