"""Label retained delivery-test history without creating or injecting any work."""
import json
import storage

KEY = 'care_delivery_probe_20261030_v1'


def mode(db, row, dose, course):
    marker = db.get(storage.BotSetting, KEY)
    if not marker: return ''
    data = json.loads(marker.value)
    if row.user_id != data['user_id'] or course.pet_id != data['pet_id'] or dose.id not in data['dose_ids']:
        return ''
    return 'normal'
