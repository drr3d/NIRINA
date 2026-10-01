"""On/off switch for the task companion, stored as a small JSON file in the data folder."""
import json

from core_agent.config import data_dir


def lokasi(config_path=None):
    # config_path is kept for call compatibility; the file lives in core_agent.config.data_dir
    # (env NIRINA_DATA_DIR), under addons/.
    return data_dir / 'addons' / 'voyager_adaptif.json'


def baca(config_path=None):
    try:
        data = json.loads(lokasi(config_path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {'aktif': False}
    if not isinstance(data, dict) or set(data) != {'aktif'} or type(data['aktif']) is not bool:
        raise ValueError('Setelan Voyager Adaptif tidak valid')
    return data


def aktif(config_path=None):
    return baca(config_path)['aktif']


def set_aktif(nilai, config_path=None):
    """Write the switch (used by the add-on manager). Takes effect the next time a task is started."""
    if type(nilai) is not bool:
        raise ValueError('Setelan Voyager Adaptif tidak valid')
    from core_agent.storage.atomic import tulis_json_atomik
    tulis_json_atomik(lokasi(config_path), {'aktif': nilai})
