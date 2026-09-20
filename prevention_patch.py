import asyncio
import re
from datetime import date, datetime, timedelta

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, select
from sqlalchemy.orm import Mapped, mapped_column
from telegram import ReplyKeyboardMarkup
from telegram.ext import Application

from storage import Base, Pet, SessionLocal, User


class PreventiveEvent(Base):
    __tablename__ = "preventive_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    pet_id: Mapped[int] = mapped_column(ForeignKey("pets.id"), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reminded_7: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reminded_1: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reminded_0: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


PREVENTION_MENU = ReplyKeyboardMarkup(
    [
        ["💉 Вакцинация", "🪱 Глисты"],
        ["🪲 Блохи и клещи", "📅 Календарь"],
        ["⬅️ Главное меню"],
    ],
    resize_keyboard=True,
)

CALENDAR_MENU = ReplyKeyboardMarkup(
    [
        ["➕ Вакцинация", "➕ Обработка от глистов"],
        ["➕ Блохи/клещи", "📅 Мои события"],
        ["⬅️ Профилактика", "⬅️ Главное меню"],
    ],
    resize_keyboard=True,
)

VACCINATION_TEXT = """💉 Вакцинация собак и кошек

Базовая схема зависит от возраста, предыдущих прививок, образа жизни, страны и конкретной вакцины.

Для щенков и котят важна первичная серия вакцинаций с повторными введениями до возраста, когда материнские антитела уже не мешают формированию иммунитета. После первичной серии нужна ревакцинация, а дальше интервал зависит от конкретного компонента вакцины и местных требований.

К базовым (core) вакцинам обычно относят:
Собаки — чума плотоядных, парвовирус, аденовирус; бешенство — по эпидемиологии и законодательству.
Кошки — панлейкопения, герпесвирус и калицивирус; бешенство — по эпидемиологии и законодательству.

Не все компоненты нужно автоматически повторять ежегодно. График лучше строить по данным паспорта, возрасту, рискам и инструкции к конкретной вакцине.

В календарь можно занести дату следующей вакцинации — я напомню за 7 дней, за 1 день и в сам день."""

WORM_TEXT = """🪱 Обработка от гельминтов

Единого интервала «для всех раз в N месяцев» нет: схема зависит от возраста, охоты/поедания добычи, сырого мяса, прогулок, контакта с другими животными, поездок и региональных паразитарных рисков.

Для взрослого питомца профилактика должна быть регулярной и риск-ориентированной. Возможны два подхода: плановая обработка препаратом подходящего спектра или периодические исследования кала с лечением по результату — выбор зависит от конкретного риска.

Щенкам и котятам обработки обычно требуются чаще, чем взрослым животным.

Всегда проверяйте вид животного, массу, возраст и инструкцию препарата. Не используйте средство «на глаз» и не переносите дозировку с другого веса или другого вида животного.

После обработки можно занести следующую дату в календарь — бот напомнит заранее."""

ECTO_TEXT = """🪲 Блохи и клещи

Защита должна соответствовать реальному риску и инструкции выбранного препарата. В регионах, где блохи и клещи активны круглый год, защита также обычно нужна круглый год.

Формы бывают разные: таблетки, капли на холку, ошейники и другие зарегистрированные средства. Интервал действия у них разный, поэтому следующую обработку нужно считать по инструкции именно вашего препарата.

Если в доме появились блохи, обрабатывать обычно нужно всех восприимчивых животных в доме и учитывать окружающую среду — одной обработки одного питомца часто недостаточно.

Очень важно: препараты для собак нельзя автоматически использовать у кошек. Некоторые пиретроиды, особенно перметрин, могут быть опасны для кошек.

Добавьте дату окончания действия/следующей обработки в календарь — я напомню за 7 дней, за 1 день и в день обработки."""


def _parse_date(value: str) -> date | None:
    value = (value or "").strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    match = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", value)
    if match:
        try:
            return date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
        except ValueError:
            return None
    return None


def _active_pet(telegram_id: int):
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.telegram_id == telegram_id))
        if not user or not user.active_pet_id:
            return None
        pet = session.scalar(select(Pet).where(Pet.id == user.active_pet_id, Pet.user_id == user.id))
        if not pet:
            return None
        return {"id": pet.id, "name": pet.name, "species": pet.species}


def _save_event(telegram_id: int, pet_id: int, kind: str, due_date: date, note: str | None = None):
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.telegram_id == telegram_id))
        if not user:
            return False
        pet = session.scalar(select(Pet).where(Pet.id == pet_id, Pet.user_id == user.id))
        if not pet:
            return False
        session.add(PreventiveEvent(user_id=user.id, pet_id=pet.id, kind=kind, due_date=due_date, note=note))
        session.commit()
        return True


def _list_events(telegram_id: int):
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.telegram_id == telegram_id))
        if not user:
            return []
        rows = session.execute(
            select(PreventiveEvent, Pet)
            .join(Pet, Pet.id == PreventiveEvent.pet_id)
            .where(PreventiveEvent.user_id == user.id, PreventiveEvent.due_date >= date.today())
            .order_by(PreventiveEvent.due_date)
            .limit(20)
        ).all()
        return [
            {
                "id": event.id,
                "pet": pet.name,
                "kind": event.kind,
                "due_date": event.due_date,
                "note": event.note,
            }
            for event, pet in rows
        ]


def _due_notifications():
    today = date.today()
    notifications = []
    with SessionLocal() as session:
        rows = session.execute(
            select(PreventiveEvent, Pet, User)
            .join(Pet, Pet.id == PreventiveEvent.pet_id)
            .join(User, User.id == PreventiveEvent.user_id)
            .where(PreventiveEvent.due_date >= today - timedelta(days=1), PreventiveEvent.due_date <= today + timedelta(days=7))
        ).all()
        for event, pet, user in rows:
            days = (event.due_date - today).days
            field = None
            if days == 7 and not event.reminded_7:
                field = "reminded_7"
            elif days == 1 and not event.reminded_1:
                field = "reminded_1"
            elif days == 0 and not event.reminded_0:
                field = "reminded_0"
            if not field:
                continue
            labels = {
                "vaccination": "вакцинация",
                "deworming": "обработка от глистов",
                "ectoparasites": "обработка от блох/клещей",
            }
            if days == 7:
                when = "через 7 дней"
            elif days == 1:
                when = "завтра"
            else:
                when = "сегодня"
            notifications.append((event.id, field, user.telegram_id, f"🔔 Напоминание: у {pet.name} {when} запланирована {labels.get(event.kind, event.kind)} — {event.due_date.strftime('%d.%m.%Y')}."))
    return notifications


def _mark_reminded(event_id, field):
    if field not in {"reminded_7", "reminded_1", "reminded_0"}:
        raise ValueError("invalid reminder field")
    with SessionLocal() as session:
        event = session.get(PreventiveEvent, event_id)
        if event:
            setattr(event, field, True)
            session.commit()


async def _send_due_notifications(application):
    for event_id, field, chat_id, text in await asyncio.to_thread(_due_notifications):
        try:
            await application.bot.send_message(chat_id=chat_id, text=text)
        except Exception as exc:
            print(f"reminder send failed event={event_id} error={type(exc).__name__}", flush=True)
            continue
        await asyncio.to_thread(_mark_reminded, event_id, field)


async def _reminder_loop(application):
    while True:
        try:
            await _send_due_notifications(application)
        except Exception:
            pass
        await asyncio.sleep(3600)


def _install_run_polling_hook():
    if getattr(Application, "_mydoctor_prevention_hook", False):
        return
    original = Application.run_polling

    def run_polling_with_reminders(self, *args, **kwargs):
        previous_post_init = self.post_init

        async def post_init(app):
            if previous_post_init:
                await previous_post_init(app)
            # Do not register an infinite task with Application.stop(), which waits
            # for registered tasks and would hang during a rolling deployment.
            app.bot_data["reminder_task"] = asyncio.create_task(_reminder_loop(app), name="preventive-reminders")

        previous_post_stop = self.post_stop
        async def post_stop(app):
            task = app.bot_data.pop("reminder_task", None)
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            if previous_post_stop:
                await previous_post_stop(app)

        self.post_init = post_init
        self.post_stop = post_stop
        return original(self, *args, **kwargs)

    Application.run_polling = run_polling_with_reminders
    Application._mydoctor_prevention_hook = True


def install(bot):
    bot.MENU = ReplyKeyboardMarkup(
        [
            ["🐾 Мои питомцы", "💬 Задать вопрос"],
            ["🧪 Анализы и документы", "🛡 Профилактика"],
            ["👨‍⚕️ Записаться на консультацию", "ℹ️ Возможности"],
        ],
        resize_keyboard=True,
    )

    original_message = bot.message

    async def start_calendar_event(update, context, kind: str, label: str):
        telegram_id = update.effective_user.id
        pet = await asyncio.to_thread(_active_pet, telegram_id)
        if not pet:
            await update.message.reply_text("Сначала выберите питомца.", reply_markup=bot.MENU)
            return
        context.user_data["prevention_flow"] = {"step": "date", "kind": kind, "label": label}
        await update.message.reply_text(
            f"Введите дату для {pet['name']} в формате ДД.ММ.ГГГГ, например 25.10.2026.",
            reply_markup=CALENDAR_MENU,
        )

    async def prevention_message(update, context):
        text = (update.message.text or "").strip() if update.message else ""
        normalized = text.lower().replace("ё", "е")
        telegram_id = update.effective_user.id

        if text in {"❌ Отмена", "⬅️ Главное меню", "⬅️ Профилактика"}:
            context.user_data.pop("prevention_flow", None)
            context.user_data.pop("prevention_section", None)
            if text == "❌ Отмена":
                await update.message.reply_text("Отменено.", reply_markup=bot.MENU)
                return

        flow = context.user_data.get("prevention_flow")
        if flow and flow.get("step") == "date":
            due = _parse_date(text)
            if due is None:
                await update.message.reply_text("Не понял дату. Напишите, например: 25.10.2026", reply_markup=CALENDAR_MENU)
                return
            if due < date.today():
                await update.message.reply_text("Эта дата уже прошла. Укажите будущую дату.", reply_markup=CALENDAR_MENU)
                return
            pet = await asyncio.to_thread(_active_pet, telegram_id)
            if not pet:
                context.user_data.pop("prevention_flow", None)
                await update.message.reply_text("Сначала выберите питомца в разделе «🐾 Мои питомцы».", reply_markup=bot.MENU)
                return
            ok = await asyncio.to_thread(_save_event, telegram_id, pet["id"], flow["kind"], due, flow.get("note"))
            context.user_data.pop("prevention_flow", None)
            if ok:
                await update.message.reply_text(
                    f"Готово ✅\n{pet['name']}: {flow['label']} — {due.strftime('%d.%m.%Y')}.\nНапомню за 7 дней, за 1 день и в сам день.",
                    reply_markup=CALENDAR_MENU,
                )
            return

        if text == "🛡 Профилактика" or text == "⬅️ Профилактика":
            context.user_data["prevention_section"] = "main"
            await update.message.reply_text(
                "🛡 Профилактика\n\nЗдесь можно посмотреть памятки и вести календарь вакцинаций и обработок конкретного питомца.",
                reply_markup=PREVENTION_MENU,
            )
            return
        if text == "💉 Вакцинация":
            await update.message.reply_text(VACCINATION_TEXT, reply_markup=PREVENTION_MENU)
            return
        if text == "🪱 Глисты":
            await update.message.reply_text(WORM_TEXT, reply_markup=PREVENTION_MENU)
            return
        if text == "🪲 Блохи и клещи":
            await update.message.reply_text(ECTO_TEXT, reply_markup=PREVENTION_MENU)
            return
        if text == "📅 Календарь":
            pet = await asyncio.to_thread(_active_pet, telegram_id)
            if not pet:
                await update.message.reply_text("Сначала выберите питомца в разделе «🐾 Мои питомцы», затем откройте календарь.", reply_markup=bot.MENU)
                return
            context.user_data["prevention_section"] = "calendar"
            await update.message.reply_text(f"📅 Календарь профилактики: {pet['name']}\n\nЧто добавить?", reply_markup=CALENDAR_MENU)
            return

        calendar_aliases = {
            "➕ Вакцинация": ("vaccination", "вакцинация"),
            "➕ Обработка от глистов": ("deworming", "обработка от глистов"),
            "➕ Блохи/клещи": ("ectoparasites", "обработка от блох/клещей"),
        }
        if text in calendar_aliases:
            kind, label = calendar_aliases[text]
            await start_calendar_event(update, context, kind, label)
            return

        if context.user_data.get("prevention_section") == "calendar":
            if ("обработ" in normalized or "защит" in normalized) and any(x in normalized for x in ("блох", "клещ")):
                await start_calendar_event(update, context, "ectoparasites", "обработка от блох/клещей")
                return
            if ("глист" in normalized or "гельминт" in normalized or "дегельмин" in normalized):
                await start_calendar_event(update, context, "deworming", "обработка от глистов")
                return
            if "вакцин" in normalized or "привив" in normalized:
                await start_calendar_event(update, context, "vaccination", "вакцинация")
                return

        if text == "📅 Мои события":
            items = await asyncio.to_thread(_list_events, telegram_id)
            if not items:
                await update.message.reply_text("В календаре пока нет будущих событий.", reply_markup=CALENDAR_MENU)
                return
            labels = {"vaccination": "вакцинация", "deworming": "глисты", "ectoparasites": "блохи/клещи"}
            lines = ["📅 Ближайшие события:"]
            for item in items:
                lines.append(f"{item['due_date'].strftime('%d.%m.%Y')} — {item['pet']}: {labels.get(item['kind'], item['kind'])}")
            await update.message.reply_text("\n".join(lines), reply_markup=CALENDAR_MENU)
            return
        if text == "⬅️ Главное меню":
            context.user_data.pop("prevention_flow", None)
            context.user_data.pop("prevention_section", None)
            await update.message.reply_text("Главное меню", reply_markup=bot.MENU)
            return
        return await original_message(update, context)

    bot.message = prevention_message
    _install_run_polling_hook()
