from datetime import datetime, timedelta
from sqlalchemy import select, func
from test_pet_care import account, post, medication_form, clean
from care_models import MedicationCourse, MedicationDose
import storage


def batch_form():
    form = medication_form()
    form['batch_count'] = '2'
    for index, duration in enumerate((14, 7)):
        for field in ('name', 'dose', 'instructions', 'start_date', 'end_date', 'times'):
            form[f'med_{index}_{field}'] = form[field]
        form[f'med_{index}_name'] = f'TEST medicine {index}'
        form[f'med_{index}_duration_days'] = str(duration)
        form[f'med_{index}_end_date'] = ''
    return form


def test_batch_duration_and_repeat_submission():
    client, uid, pid = account()
    form = batch_form()
    for _ in range(2):
        assert post(client, f'/care/pets/{pid}/medications', form).status_code == 303
    with storage.SessionLocal() as db:
        courses = db.scalars(select(MedicationCourse).order_by(MedicationCourse.id)).all()
        assert len(courses) == 2
        assert [(c.end_date-c.start_date).days for c in courses] == [13, 6]
        assert db.scalar(select(func.count()).select_from(MedicationDose)) == 42


def test_batch_invalid_second_row_rolls_back_everything():
    client, uid, pid = account()
    form = batch_form()
    form['med_1_times'] = '25:00'
    response = post(client, f'/care/pets/{pid}/medications', form)
    assert response.status_code == 200
    with storage.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(MedicationCourse)) == 0
        assert db.scalar(select(func.count()).select_from(MedicationDose)) == 0


def test_single_course_calculates_end_date_on_server():
    client, uid, pid = account()
    form = medication_form()
    form.update(duration_days='14', end_date='')
    assert post(client, f'/care/pets/{pid}/medications', form).status_code == 303
    with storage.SessionLocal() as db:
        course = db.scalar(select(MedicationCourse))
        assert (course.end_date-course.start_date).days == 13
