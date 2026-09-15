import asyncio
import base64
import io

from PIL import Image, ImageOps


MEDIA_ANALYSIS_MODE = """

РЕЖИМ ЧТЕНИЯ МЕДИЦИНСКИХ ИЗОБРАЖЕНИЙ И ДОКУМЕНТОВ
Фото, скриншот и изображение медицинского документа являются полноценным источником данных так же, как PDF.

Правила:
- сначала внимательно прочитай весь видимый текст, таблицы, цифры, единицы измерения и подписи на изображении;
- перед клинической интерпретацией мысленно восстанови содержание документа и проверь, что не перепутал подписи, значения и соседние строки;
- не проси PDF только потому, что пользователь прислал фото или скриншот;
- если изображение читаемо, сразу разбирай его по существу;
- если часть изображения реально неразборчива, укажи конкретно, какие строки/цифры не удалось прочитать, но разбери всё остальное;
- никогда не придумывай значения, отметки, единицы или референсные интервалы, которых не видно;
- если прислано несколько увеличенных фрагментов одного документа, сопоставь их как части одной и той же страницы и не считай повторяющийся текст разными данными;
- если это лабораторный анализ, сопоставляй показатели с референсами именно из документа и оценивай связанные изменения вместе;
- если это УЗИ, рентгенологическое заключение, выписка или другой протокол, отделяй фактические находки и заключение от справочного текста и шаблонных вариантов;
- если на бланке перечислены варианты вроде «ровные / неровные», «чёткие / нечёткие», «расширены / не расширены», не считай все варианты находками. Учитывай только явно выбранный, отмеченный, выделенный или заполненный вариант;
- если по изображению невозможно понять, какой вариант выбран, прямо скажи об этом и не делай вывод на основании списка вариантов;
- не считай документ принадлежащим активному питомцу только потому, что он выбран в боте. Используй данные активного питомца только если пользователь явно связал документ с ним или это однозначно видно из текущего диалога;
- ответ начинай с краткого итога, затем основные находки, затем их возможное значение и что логично делать дальше.
"""


def _data_url(image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _jpeg_bytes(image: Image.Image, quality: int = 92) -> bytes:
    if image.mode != "RGB":
        background = Image.new("RGB", image.size, "white")
        if image.mode in {"RGBA", "LA"}:
            alpha = image.getchannel("A")
            background.paste(image.convert("RGB"), mask=alpha)
            image = background
        else:
            image = image.convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def _preprocess_image(file_bytes: bytes) -> list[bytes]:
    """Return one enhanced full image plus enlarged overlapping crops."""
    image = Image.open(io.BytesIO(file_bytes))
    image = ImageOps.exif_transpose(image)
    if image.mode not in {"RGB", "RGBA"}:
        image = image.convert("RGB")

    width, height = image.size
    long_side = max(width, height)

    # Telegram often compresses screenshots. Upscale small screenshots so fine table text
    # is presented to the vision model at a useful size, while avoiding absurd dimensions.
    if long_side < 2800:
        scale = min(2.5, 2800 / max(long_side, 1))
        if scale > 1.05:
            image = image.resize(
                (max(1, int(width * scale)), max(1, int(height * scale))),
                Image.Resampling.LANCZOS,
            )

    width, height = image.size
    results = [_jpeg_bytes(image)]

    overlap = 0.12
    if height >= width * 1.25:
        # Tall report/screenshot: three overlapping horizontal bands.
        band = int(height * 0.46)
        starts = [0, max(0, int(height * 0.27)), max(0, height - band)]
        seen = set()
        for top in starts:
            bottom = min(height, top + band)
            key = (top, bottom)
            if key in seen or bottom - top < 200:
                continue
            seen.add(key)
            crop = image.crop((0, top, width, bottom))
            results.append(_jpeg_bytes(crop))
    elif width >= height * 1.25:
        # Wide screenshot/table: three overlapping vertical bands.
        band = int(width * 0.46)
        starts = [0, max(0, int(width * 0.27)), max(0, width - band)]
        seen = set()
        for left in starts:
            right = min(width, left + band)
            key = (left, right)
            if key in seen or right - left < 200:
                continue
            seen.add(key)
            crop = image.crop((left, 0, right, height))
            results.append(_jpeg_bytes(crop))
    else:
        # Near-square page: four overlapping quadrants.
        x_margin = int(width * overlap)
        y_margin = int(height * overlap)
        mid_x = width // 2
        mid_y = height // 2
        boxes = [
            (0, 0, min(width, mid_x + x_margin), min(height, mid_y + y_margin)),
            (max(0, mid_x - x_margin), 0, width, min(height, mid_y + y_margin)),
            (0, max(0, mid_y - y_margin), min(width, mid_x + x_margin), height),
            (max(0, mid_x - x_margin), max(0, mid_y - y_margin), width, height),
        ]
        for box in boxes:
            crop = image.crop(box)
            results.append(_jpeg_bytes(crop))

    return results[:5]


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
                "Прочитай этот ветеринарный медицинский документ. Сначала определи, какие данные "
                "и находки реально указаны или отмечены, затем кратко объясни их значение. "
                "Не проси PDF, если текст можно прочитать с изображения."
            )

        await update.message.reply_text("Читаю документ…", reply_markup=bot.MENU)
        await update.message.chat.send_action("typing")

        try:
            tg_file = await context.bot.get_file(file_id)
            file_bytes = bytes(await tg_file.download_as_bytearray())
            print(
                f"media received type={input_type} mime={mime_type} bytes={len(file_bytes)} filename={filename}",
                flush=True,
            )

            if input_type == "input_file":
                encoded = base64.b64encode(file_bytes).decode("ascii")
                media_parts = [{
                    "type": "input_file",
                    "filename": filename,
                    "file_data": f"data:{mime_type};base64,{encoded}",
                }]
            else:
                processed = await asyncio.to_thread(_preprocess_image, file_bytes)
                print(f"media image variants={len(processed)}", flush=True)
                media_parts = [
                    {
                        "type": "input_image",
                        "image_url": _data_url(part),
                        "detail": "high",
                    }
                    for part in processed
                ]

            instructions = bot.SYSTEM_PROMPT + MEDIA_ANALYSIS_MODE
            history = context.user_data.setdefault("history", [])
            response_input = history[-4:] + [
                {
                    "role": "user",
                    "content": media_parts + [
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
            print(
                f"media model response chars={len(answer)}",
                flush=True,
            )
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
