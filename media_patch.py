import asyncio
import base64


MEDIA_ANALYSIS_MODE = """

РЕЖИМ ЧТЕНИЯ МЕДИЦИНСКИХ ИЗОБРАЖЕНИЙ И ДОКУМЕНТОВ
Фото, скриншот и изображение медицинского документа являются полноценным источником данных так же, как PDF.

Правила:
- сначала внимательно прочитай весь видимый текст, таблицы, цифры, единицы измерения и подписи на изображении;
- не проси PDF только потому, что пользователь прислал фото или скриншот;
- если изображение читаемо, сразу разбирай его по существу;
- если часть изображения реально неразборчива, укажи конкретно, какие строки/цифры не удалось прочитать, но разбери всё остальное;
- никогда не придумывай значения, отметки, единицы или референсные интервалы, которых не видно;
- если это лабораторный анализ, сопоставляй показатели с референсами именно из документа и оценивай связанные изменения вместе;
- если это УЗИ, рентгенологическое заключение, выписка или другой протокол, отделяй фактические находки и заключение от справочного текста и шаблонных вариантов;
- если на бланке перечислены варианты вроде «ровные / неровные», «чёткие / нечёткие», «расширены / не расширены», не считай все варианты находками. Учитывай только явно выбранный, отмеченный, выделенный или заполненный вариант;
- если по изображению невозможно понять, какой вариант выбран, прямо скажи об этом и не делай вывод на основании списка вариантов;
- не считай документ принадлежащим активному питомцу только потому, что он выбран в боте. Используй данные активного питомца только если пользователь явно связал документ с ним или это однозначно видно из текущего диалога;
- ответ начинай с краткого итога, затем основные находки, затем их возможное значение и что логично делать дальше.
"""


def install(bot):
    async def media(update, context):
        await bot.ensure_current_user(update)

        if bot.client is None:
            await update.message.reply_text("ИИ временно не подключён.", reply_markup=bot.MENU)
            return

        if update.message.photo:
            item = update.message.photo[-1]
            file_id = item.file_id
            file_size = item.file_size or 0
            mime_type = "image/jpeg"
            filename = "medical_image.jpg"
            input_type = "input_image"
        elif update.message.document:
            document = update.message.document
            mime_type = document.mime_type or ""
            if mime_type not in {"application/pdf", "image/jpeg", "image/png", "image/webp"}:
                await update.message.reply_text(
                    "Можно прислать фото/скриншот JPG, PNG, WEBP или PDF.",
                    reply_markup=bot.MENU,
                )
                return
            file_id = document.file_id
            file_size = document.file_size or 0
            filename = document.file_name or "medical_document"
            input_type = "input_file" if mime_type == "application/pdf" else "input_image"
        else:
            return

        if file_size > 10 * 1024 * 1024:
            await update.message.reply_text(
                "Файл больше 10 МБ. Пришлите уменьшенную копию или отдельные страницы.",
                reply_markup=bot.MENU,
            )
            return

        user_request = (update.message.caption or "").strip()
        if not user_request:
            user_request = (
                "Прочитай этот ветеринарный медицинский документ на изображении. "
                "Сначала извлеки реальные данные и отмеченные находки, затем кратко объясни их значение."
            )

        await update.message.reply_text("Читаю документ…", reply_markup=bot.MENU)
        await update.message.chat.send_action("typing")

        try:
            tg_file = await context.bot.get_file(file_id)
            file_bytes = bytes(await tg_file.download_as_bytearray())
            encoded = base64.b64encode(file_bytes).decode("ascii")

            if input_type == "input_file":
                media_part = {
                    "type": "input_file",
                    "filename": filename,
                    "file_data": f"data:{mime_type};base64,{encoded}",
                }
            else:
                media_part = {
                    "type": "input_image",
                    "image_url": f"data:{mime_type};base64,{encoded}",
                    "detail": "high",
                }

            instructions = bot.SYSTEM_PROMPT + MEDIA_ANALYSIS_MODE
            history = context.user_data.setdefault("history", [])
            response_input = history[-6:] + [
                {
                    "role": "user",
                    "content": [
                        media_part,
                        {"type": "input_text", "text": user_request},
                    ],
                }
            ]

            response = await asyncio.to_thread(
                bot.client.responses.create,
                model="gpt-5.6-sol",
                instructions=instructions,
                input=response_input,
            )
            answer = (response.output_text or "").strip()
            if not answer:
                answer = (
                    "Не удалось уверенно прочитать изображение. Попробуйте прислать его крупнее "
                    "или отдельными фрагментами — PDF не обязателен."
                )

            history.append({
                "role": "user",
                "content": f"Загружено медицинское изображение/документ {filename}. Запрос: {user_request}",
            })
            history.append({"role": "assistant", "content": answer})
            history[:] = history[-12:]

            await asyncio.to_thread(
                bot.save_consultation,
                update.effective_user.id,
                f"Файл: {filename}. {user_request}",
                answer,
                "file_analysis",
            )
        except Exception as exc:
            print(f"media analysis error: {exc!r}", flush=True)
            answer = (
                "Не получилось обработать изображение. Пришлите его ещё раз как фото или файл. "
                "PDF специально делать не нужно."
            )

        await update.message.reply_text(answer, reply_markup=bot.MENU)

    bot.media = media
