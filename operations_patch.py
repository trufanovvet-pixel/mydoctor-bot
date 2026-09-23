"""Explicit, owner-scoped operation archive and categorized document browser."""
import asyncio
from datetime import datetime
from sqlalchemy import select
from telegram import ReplyKeyboardMarkup
import storage
from patient_records import OperationRecord, OperationAttachment, DocumentLabel, CATEGORIES, classify_document, plain_text
from pet_records_patch import PetDocument

OPERATIONS='🏥 Операции'
CATS={f'📁 {label}':key for key,label in CATEGORIES.items()}

def keyboard(rows):return ReplyKeyboardMarkup(rows+[['⬅️ Главное меню']],resize_keyboard=True)
def user_id(db,telegram_id):return db.scalar(select(storage.User.id).where(storage.User.telegram_id==telegram_id))
def pets_for(telegram_id):return storage.list_pets(telegram_id)
def list_operations(telegram_id,pid):
    with storage.SessionLocal() as db:
        uid=user_id(db,telegram_id)
        if not db.scalar(select(storage.Pet.id).where(storage.Pet.id==pid,storage.Pet.user_id==uid)):return []
        return db.scalars(select(OperationRecord).where(OperationRecord.user_id==uid,OperationRecord.pet_id==pid).order_by(OperationRecord.created_at.desc())).all()

def install(bot):
    storage.Base.metadata.create_all(storage.engine)
    previous_message=bot.message;previous_media=bot.media
    rows=[list(row) for row in bot.MENU.keyboard]
    rows.insert(1,[OPERATIONS])
    bot.MENU=ReplyKeyboardMarkup(rows,resize_keyboard=True)

    async def choose_pet(update,context):
        await bot.ensure_current_user(update)
        pets=await asyncio.to_thread(pets_for,update.effective_user.id)
        if not pets:
            await update.message.reply_text('Сначала добавьте питомца в разделе «Мои питомцы».',reply_markup=bot.MENU);return
        choices={f"🐾 {p['name']} · №{p['id']}":p['id'] for p in pets}
        context.user_data['operation_flow']={'step':'pet','choices':choices}
        await update.message.reply_text('Чью историю операций открыть?',reply_markup=keyboard([[label] for label in choices]))

    async def op_menu(update,context,pid):
        ops=await asyncio.to_thread(list_operations,update.effective_user.id,pid)
        choices={f'🏥 №{op.id} · {op.title[:70]}':op.id for op in ops}
        context.user_data['operation_flow']={'step':'menu','pet_id':pid,'choices':choices}
        await update.message.reply_text('Операции питомца\n\nВыберите запись, чтобы открыть её и выписки.' if ops else 'Операций пока нет. Добавьте запись, затем прикрепите выписку.',reply_markup=keyboard([['➕ Добавить операцию']]+[[x] for x in choices]))

    async def show_op(update,context,pid,oid):
        with storage.SessionLocal() as db:
            uid=user_id(db,update.effective_user.id)
            op=db.scalar(select(OperationRecord).where(OperationRecord.id==oid,OperationRecord.user_id==uid,OperationRecord.pet_id==pid))
            if not op:return
            files=db.scalars(select(OperationAttachment).where(OperationAttachment.operation_id==oid,OperationAttachment.user_id==uid)).all()
            choices={f'📄 Выписка №{f.id} · {f.filename[:50]}':f.id for f in files}
            context.user_data['operation_flow']={'step':'detail','pet_id':pid,'operation_id':oid,'choices':choices}
            text=op.title+'\n'+(op.operation_date.strftime('%d.%m.%Y') if op.operation_date else 'Дата не указана')+'\n\n'+(op.notes or '')
        await update.message.reply_text(plain_text(text),reply_markup=keyboard([['📎 Добавить выписку'],['⬅️ Операции питомца']]+[[x] for x in choices]))

    async def message(update,context):
        text=(update.message.text or '').strip() if update.message else ''
        if text in ('⬅️ Главное меню','❌ Отмена'):
            context.user_data.pop('operation_flow',None);context.user_data.pop('archive_choices',None)
            return await previous_message(update,context)
        if text==OPERATIONS:return await choose_pet(update,context)
        if text=='🧪 Анализы и документы':
            context.user_data.pop('operation_flow',None)
            await update.message.reply_text('Анализы и исследования\n\nВыберите категорию. Документы откроются только после выбора файла.',reply_markup=keyboard([['📤 Загрузить анализы/документы'],*[[x] for x in CATS],['📋 История обращений']]))
            return
        if text in CATS:
            await bot.ensure_current_user(update)
            with storage.SessionLocal() as db:
                uid=user_id(db,update.effective_user.id)
                rows=db.scalars(select(PetDocument).where(PetDocument.user_id==uid).order_by(PetDocument.created_at.desc())).all()
                labels={r.document_id:r.category for r in db.scalars(select(DocumentLabel).where(DocumentLabel.source=='telegram')).all()}
                pets={p.id:p.name for p in db.scalars(select(storage.Pet).where(storage.Pet.user_id==uid)).all()}
                chosen=[r for r in rows if labels.get(r.id,classify_document((r.filename or '')+' '+(r.caption or '')))==CATS[text]]
                choices={f"📄 №{r.id} · {pets.get(r.pet_id,'Без привязки')} · {(r.filename or 'Документ')[:45]}":r.id for r in chosen}
            context.user_data['archive_choices']=choices
            await update.message.reply_text(CATEGORIES[CATS[text]]+('\nВыберите документ:' if chosen else '\nВ этой категории пока нет файлов.'),reply_markup=keyboard([[x] for x in choices]+[['🧪 Анализы и документы']]))
            return
        if text in context.user_data.get('archive_choices',{}):
            did=context.user_data['archive_choices'][text]
            with storage.SessionLocal() as db:
                uid=user_id(db,update.effective_user.id);doc=db.scalar(select(PetDocument).where(PetDocument.id==did,PetDocument.user_id==uid))
                if not doc:return
                file_id=doc.telegram_file_id;is_photo=doc.media_type=='photo'
            if is_photo:await context.bot.send_photo(chat_id=update.effective_chat.id,photo=file_id)
            else:await context.bot.send_document(chat_id=update.effective_chat.id,document=file_id)
            return
        flow=context.user_data.get('operation_flow')
        if not flow:return await previous_message(update,context)
        step=flow['step'];pid=flow.get('pet_id')
        if step=='pet':
            if text not in flow['choices']:
                await update.message.reply_text('Выберите питомца кнопкой или вернитесь в главное меню.');return
            return await op_menu(update,context,flow['choices'][text])
        if text=='⬅️ Операции питомца':return await op_menu(update,context,pid)
        if step=='menu':
            if text=='➕ Добавить операцию':
                flow['step']='title';await update.message.reply_text('Как называлась операция?',reply_markup=keyboard([['❌ Отмена']]));return
            if text in flow['choices']:return await show_op(update,context,pid,flow['choices'][text])
        elif step=='title':
            if not text or len(text)>255:await update.message.reply_text('Введите название до 255 символов.');return
            flow.update(step='date',title=text)
            await update.message.reply_text('Когда была операция? Укажите ДД.ММ.ГГГГ или нажмите «Дата неизвестна».',reply_markup=keyboard([['Дата неизвестна']]));return
        elif step=='date':
            try:day=None if text=='Дата неизвестна' else datetime.strptime(text,'%d.%m.%Y').date()
            except ValueError:await update.message.reply_text('Напишите дату в формате ДД.ММ.ГГГГ.');return
            flow.update(step='notes',date=day.isoformat() if day else None)
            await update.message.reply_text('Добавьте название клиники и заметку либо нажмите «Без заметки».',reply_markup=keyboard([['Без заметки']]));return
        elif step=='notes':
            with storage.SessionLocal() as db:
                uid=user_id(db,update.effective_user.id)
                if not db.scalar(select(storage.Pet.id).where(storage.Pet.id==pid,storage.Pet.user_id==uid)):context.user_data.pop('operation_flow',None);return
                op=OperationRecord(user_id=uid,pet_id=pid,title=flow['title'],operation_date=datetime.fromisoformat(flow['date']).date() if flow['date'] else None,notes=None if text=='Без заметки' else text[:10000]);db.add(op);db.commit();oid=op.id
            return await show_op(update,context,pid,oid)
        elif step=='detail':
            if text=='📎 Добавить выписку':
                flow['step']='upload';await update.message.reply_text('Пришлите фото или PDF выписки (до 20 МБ). Сохраню её в этой операции.',reply_markup=keyboard([['⬅️ Операции питомца']]));return
            if text in flow['choices']:
                with storage.SessionLocal() as db:
                    uid=user_id(db,update.effective_user.id);file=db.scalar(select(OperationAttachment).where(OperationAttachment.id==flow['choices'][text],OperationAttachment.user_id==uid,OperationAttachment.operation_id==flow['operation_id']))
                    if not file:return
                    payload=file.data or file.telegram_file_id;name=file.filename
                if payload:await context.bot.send_document(chat_id=update.effective_chat.id,document=payload,filename=name)
                return
        await update.message.reply_text('Выберите действие кнопкой или вернитесь в главное меню.')

    async def media(update,context):
        flow=context.user_data.get('operation_flow')
        if not flow or flow.get('step')!='upload':
            result=await previous_media(update,context)
            if isinstance(result,dict) and result.get('document_id'):
                did=result['document_id']
                with storage.SessionLocal() as db:
                    doc=db.get(PetDocument,did)
                    if doc and doc.user_id==user_id(db,update.effective_user.id):
                        category=classify_document((doc.filename or '')+' '+(doc.caption or '')+' '+(result.get('analysis_text') or '')[:1500])
                        tag=db.get(DocumentLabel,('telegram',did))
                        if tag:tag.category=category
                        else:db.add(DocumentLabel(source='telegram',document_id=did,category=category))
                        db.commit()
            return result
        msg=update.message;item=msg.document or (msg.photo[-1] if msg.photo else None)
        if not item:return await msg.reply_text('Пришлите фото или PDF выписки.')
        if getattr(item,'file_size',0) and item.file_size>20*1024*1024:return await msg.reply_text('Файл больше 20 МБ. Пришлите уменьшенную копию.')
        try:
            remote=await context.bot.get_file(item.file_id);data=bytes(await remote.download_as_bytearray())
            if len(data)>20*1024*1024:raise ValueError('Файл больше 20 МБ.')
            mime='image/jpeg' if msg.photo else (item.mime_type or '')
            if mime not in ('image/jpeg','image/png','image/webp','application/pdf'):raise ValueError('Поддерживаются фото и PDF.')
            filename=getattr(item,'file_name',None) or 'Выписка.jpg'
            with storage.SessionLocal() as db:
                uid=user_id(db,update.effective_user.id)
                op=db.scalar(select(OperationRecord).where(OperationRecord.id==flow['operation_id'],OperationRecord.user_id==uid,OperationRecord.pet_id==flow['pet_id']))
                if not op:raise ValueError('Операция не найдена.')
                db.add(OperationAttachment(operation_id=op.id,user_id=uid,filename=filename[:255],mime_type=mime,data=data,telegram_file_id=item.file_id));db.commit()
        except ValueError as exc:await msg.reply_text(str(exc));return
        except Exception:await msg.reply_text('Не удалось сохранить файл. Попробуйте отправить его ещё раз.');return
        await msg.reply_text('Выписка сохранена.')
        return await show_op(update,context,flow['pet_id'],flow['operation_id'])
    bot.message=message;bot.media=media
