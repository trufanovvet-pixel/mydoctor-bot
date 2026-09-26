import json
from openai import OpenAI
import knowledge
from patient_context import PATIENT_FACT_RULES

SYSTEM="""Ты — «МойДоктор», ветеринарный AI-помощник владельца животного.
Медицинская логика должна соответствовать Telegram-версии проекта.
Не ставь ложный окончательный диагноз по переписке. В конкретном новом случае сначала собери недостающий анамнез естественно, 2–4 вопроса за сообщение. Не повторяй уже известное.
Когда данных достаточно, дай структурированный ответ: что наиболее вероятно; важные дифференциалы; что важно исключить; что можно сделать сейчас; чего нельзя; оправданные обследования и зачем; когда нужен очный врач; конкретные красные флаги.
Не назначай исследования списком «на всякий случай». Не упоминай бюджет без вопроса пользователя.
Для общего справочного вопроса отвечай сразу и не запускай анамнез.
Используй данные только выбранного питомца. Никогда не смешивай пациентов.
""" + PATIENT_FACT_RULES

def pet_context(pet, profile):
    values=[f"Имя: {pet.name}",f"Вид: {pet.species}",f"Порода: {pet.breed or 'не указана'}",f"Возраст: {pet.age or 'не указан'}",f"Пол: {pet.sex or 'не указан'}",f"Вес: {pet.weight_kg or 'не указан'} кг"]
    if profile:
        for label,key in [("Кастрация/стерилизация","neutered"),("Хронические болезни","chronic_conditions"),("Аллергии","allergies"),("Препараты","medications"),("Операции","surgeries"),("Важные диагнозы","important_diagnoses")]:
            v=getattr(profile,key,None)
            if v: values.append(label+": "+v)
    return "\n".join(values)

def answer(messages, pet, profile):
    latest=messages[-1]["content"] if messages else ""
    proto, species, safety=knowledge.build_clinical_context(latest, "\n".join(x["content"] for x in messages if x["role"]=="user"), pet.species+" "+(pet.breed or ""))
    instructions=SYSTEM+"\n\nКАРТОЧКА ВЫБРАННОГО ПИТОМЦА:\n"+pet_context(pet,profile)+proto
    client=OpenAI()
    r=client.responses.create(model="gpt-5.6-sol",instructions=instructions,input=messages[-40:])
    return (r.output_text or "").strip()
