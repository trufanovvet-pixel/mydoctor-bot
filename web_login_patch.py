import os
import web_auth

LABEL = "🌐 Открыть веб-кабинет"

def install(bot):
    original_message = bot.message
    async def web_login_message(update, context):
        text = (update.message.text or "").strip() if update.message else ""
        if text != LABEL:
            return await original_message(update, context)
        await bot.ensure_current_user(update)
        token = web_auth.create_token(update.effective_user.id)
        base = os.getenv("MYDOCTOR_WEB_URL", "https://mydoctor-web-production.up.railway.app").rstrip("/")
        url = base + "/login/" + token
        await update.message.reply_text(
            "Ваш безопасный вход в веб-кабинет:\n" + url + "\n\nСсылка одноразовая и действует 10 минут. Не пересылайте её другим людям.",
            reply_markup=bot.MENU,
            disable_web_page_preview=True,
        )
    bot.message = web_login_message
