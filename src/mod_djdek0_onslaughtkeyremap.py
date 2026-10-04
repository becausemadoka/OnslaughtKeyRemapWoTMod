# -*- coding: utf-8 -*-
"""
djdek0_onslaughtkeyremap.py

Mod for the game World of Tanks that expose functionality to maintain
multiple control setups based on if you are in Onslaught mode or not
"""

# --- Imports & metadata ----------------------------------------------

import sys
import logging
from functools import partial

import BigWorld
import CommandMapping
import Keys

try:
    from gui.modsSettingsApi import g_modsSettingsApi
except ImportError:
    g_modsSettingsApi = None

try:
    from constants import PREBATTLE_TYPE
    from gui.prb_control.settings import CTRL_ENTITY_TYPE
except ImportError:
    PREBATTLE_TYPE = None
    CTRL_ENTITY_TYPE = None

MOD_TAG = '[Onslaught Key Remap]'

_log = logging.getLogger('mod.onslaught_key_remap')
if not _log.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter('[%(name)s] %(message)s'))
    _log.addHandler(_handler)
    _log.setLevel(logging.INFO)
    _log.propagate = False

ONSLAUGHT_COMMANDS = [
    'CMD_AMMO_CHOICE_7',
    'CMD_AMMO_CHOICE_8',
    'CMD_AMMO_CHOICE_9',
]

DESIRED_ONSLAUGHT_KEYS = {
    'CMD_AMMO_CHOICE_7': 'KEY_E',
    'CMD_AMMO_CHOICE_8': 'KEY_R',
    'CMD_AMMO_CHOICE_9': 'KEY_F',
}

ONSLAUGHT_ENTITY_TYPE_ID = 29

ONSLAUGHT_QUEUE_TYPE_ID = 24

POLL_INTERVAL_SECONDS = 1.0

VERIFY_DELAY_SECONDS = 5.0

SETTINGS_MOD_LINKAGE = 'onslaught_key_remap'

SETTINGS_VAR_NAMES = {
    'CMD_AMMO_CHOICE_7': 'slot7Key',
    'CMD_AMMO_CHOICE_8': 'slot8Key',
    'CMD_AMMO_CHOICE_9': 'slot9Key',
}


# --- Owned runtime state ------------------------------------------------

_command_mapping = CommandMapping.g_instance
_override_applied = False

_saved_conflict_bindings = {}

_conflict_swapped_to = {}

_saved_ammo_original_keys = {}

_started = False
_poll_callback_id = None
_poll_generation = 0
_last_state_key = None

_settings = {
    'slot7Key': [Keys.KEY_E],
    'slot8Key': [Keys.KEY_R],
    'slot9Key': [Keys.KEY_F],
    'applyInTraining': False,
}


# --- Pure helpers (no game state touched) --------------------------------

def _is_onslaught(ctrl_type_id, entity_type_id, apply_in_training=False):
    if entity_type_id in (ONSLAUGHT_ENTITY_TYPE_ID, ONSLAUGHT_QUEUE_TYPE_ID):
        return True
    if apply_in_training and PREBATTLE_TYPE is not None and CTRL_ENTITY_TYPE is not None:
        return ctrl_type_id == CTRL_ENTITY_TYPE.LEGACY and entity_type_id == PREBATTLE_TYPE.TRAINING
    return False


# --- Runtime helpers: read-only live-binding lookups ----------------------

def _commands_bound_to_key(key, exclude=()):
    hits = []
    for name in dir(CommandMapping):
        if not name.startswith('CMD_') or name in exclude:
            continue
        value = getattr(CommandMapping, name)
        if not isinstance(value, int):
            continue
        try:
            bound_key = _command_mapping.get(name)
        except Exception:
            continue
        if bound_key == key:
            hits.append(name)

    return hits


def _key_value_to_name(key_value):
    if key_value is None:
        return None
    try:
        return 'KEY_' + BigWorld.keyToString(key_value)
    except Exception:
        _log.exception('%s could not resolve key value %r to a name', MOD_TAG, key_value)
        return None


# --- Runtime helpers: displacing a command bumped off E/R/F --------------

def _displace_conflict(conflict_name, from_key_name, vacated_key_name):
    try:
        removed = _command_mapping.remove(conflict_name, from_key_name)
        _log.info('%s unbound %s from %s (removed=%r)', MOD_TAG, conflict_name, from_key_name, removed)
    except Exception:
        _log.exception('%s failed to unbind %s from %s', MOD_TAG, conflict_name, from_key_name)
        return

    if not vacated_key_name:
        _log.warning('%s no vacated key available to swap %s onto -- left unbound for the rest of Onslaught', MOD_TAG, conflict_name)
        return

    try:
        added = _command_mapping.add(conflict_name, vacated_key_name, isDefault=False)
        _log.info('%s swapped %s onto %s (vacated by the ammo command that just took %s) so it stays reachable during Onslaught', MOD_TAG, conflict_name, vacated_key_name, from_key_name)
        _conflict_swapped_to[conflict_name] = vacated_key_name
    except Exception:
        _log.exception('%s failed to swap %s onto %s', MOD_TAG, conflict_name, vacated_key_name)


# --- Runtime helpers: making a CommandMapping change take effect ---------

def _save_bindings(what):
    try:
        _command_mapping.save()
        _log.info('%s %s saved and pushed live', MOD_TAG, what)
    except Exception:
        _log.exception('%s failed to save %s', MOD_TAG, what)


def _notify_mapping_changed():
    event = getattr(_command_mapping, 'onMappingChanged', None)
    if event is None:
        return
    for args in ((), (_command_mapping,)):
        try:
            event(*args)
            _log.info('%s fired onMappingChanged%r', MOD_TAG, args)
            return
        except Exception:
            continue
    _log.info('%s could not fire onMappingChanged with any tried signature', MOD_TAG)


def _schedule_verify(label):
    token = _poll_generation
    BigWorld.callback(VERIFY_DELAY_SECONDS, partial(_verify, label, token))


def _verify(label, token):
    if token != _poll_generation:
        return  # mod was unloaded/reloaded since this was scheduled

    _log.info('%s [verify:%s] re-reading bindings %.1fs later', MOD_TAG, label, VERIFY_DELAY_SECONDS)
    for command_name in ONSLAUGHT_COMMANDS:
        try:
            current_key = _command_mapping.get(command_name)
        except Exception:
            _log.exception('%s [verify:%s] failed to read %s', MOD_TAG, label, command_name)
            continue
        _log.info('%s [verify:%s] %s = %r', MOD_TAG, label, command_name, current_key)

    for conflict_name, swapped_key_name in _conflict_swapped_to.items():
        try:
            current_key = _command_mapping.get(conflict_name)
        except Exception:
            _log.exception('%s [verify:%s] failed to read %s', MOD_TAG, label, conflict_name)
            continue
        _log.info('%s [verify:%s] %s = %r (expected on %s)', MOD_TAG, label, conflict_name, current_key, swapped_key_name)


# --- Context lifecycle: apply / restore the key override -----------------

def apply_onslaught_override():
    global _override_applied

    if _override_applied:
        return

    for command_name in ONSLAUGHT_COMMANDS:
        key_name = DESIRED_ONSLAUGHT_KEYS[command_name]
        key_value = getattr(Keys, key_name)

        try:
            orig_key_value = _command_mapping.get(command_name)
        except Exception:
            _log.exception('%s failed to read %s current key before rebind', MOD_TAG, command_name)
            orig_key_value = None
        orig_key_name = _key_value_to_name(orig_key_value)
        _saved_ammo_original_keys[command_name] = orig_key_name

        for conflict_name in _commands_bound_to_key(key_value, exclude=ONSLAUGHT_COMMANDS):
            _saved_conflict_bindings[conflict_name] = key_name
            _displace_conflict(conflict_name, key_name, orig_key_name)

        try:
            stripped = _command_mapping.remove(command_name)
            _log.info('%s stripped %s from its prior key(s) (orig=%s, removed=%r) before rebinding to %s', MOD_TAG, command_name, orig_key_name, stripped, key_name)
        except Exception:
            _log.exception('%s failed to strip %s prior binding', MOD_TAG, command_name)

        try:
            added = _command_mapping.add(command_name, key_name, isDefault=False)
            _log.info('%s %s -> %s (added=%r)', MOD_TAG, command_name, key_name, added)
        except Exception:
            _log.exception('%s failed to bind %s -> %s', MOD_TAG, command_name, key_name)

    _override_applied = True
    _notify_mapping_changed()
    _save_bindings('override')
    _schedule_verify('post-apply')
    _log.info('%s override applied -- E/R/F now select Onslaught ammo/ability slots 7/8/9', MOD_TAG)


def restore_default_bindings():
    global _override_applied

    if not _override_applied:
        return

    for command_name in ONSLAUGHT_COMMANDS:
        key_name = DESIRED_ONSLAUGHT_KEYS[command_name]
        try:
            removed = _command_mapping.remove(command_name, key_name)
            _log.info('%s unbound %s from %s (removed=%r) while restoring', MOD_TAG, command_name, key_name, removed)
        except Exception:
            _log.exception('%s failed to unbind %s from %s while restoring', MOD_TAG, command_name, key_name)

        orig_key_name = _saved_ammo_original_keys.get(command_name)
        if orig_key_name:
            try:
                added = _command_mapping.add(command_name, orig_key_name, isDefault=False)
                _log.info('%s restored %s -> %s (added=%r)', MOD_TAG, command_name, orig_key_name, added)
            except Exception:
                _log.exception('%s failed to restore %s -> %s', MOD_TAG, command_name, orig_key_name)
        else:
            _log.warning('%s no original key was captured for %s -- left unbound after restore', MOD_TAG, command_name)

    for conflict_name, swapped_key_name in _conflict_swapped_to.items():
        try:
            removed = _command_mapping.remove(conflict_name, swapped_key_name)
            _log.info('%s unbound %s from %s (removed=%r) while restoring', MOD_TAG, conflict_name, swapped_key_name, removed)
        except Exception:
            _log.exception('%s failed to unbind %s from %s while restoring', MOD_TAG, conflict_name, swapped_key_name)

    for conflict_name, key_name in _saved_conflict_bindings.items():
        try:
            added = _command_mapping.add(conflict_name, key_name, isDefault=False)
            _log.info('%s restored %s -> %s (added=%r)', MOD_TAG, conflict_name, key_name, added)
        except Exception:
            _log.exception('%s failed to restore %s -> %s', MOD_TAG, conflict_name, key_name)

    _saved_conflict_bindings.clear()
    _conflict_swapped_to.clear()
    _saved_ammo_original_keys.clear()
    _override_applied = False
    _notify_mapping_changed()
    _save_bindings('restored bindings')
    _schedule_verify('post-restore')
    _log.info('%s restored to original bindings', MOD_TAG)


def rebind_slot(command_name, new_key_name):
    """Live entry point for the settings panel: point one Onslaught slot
    at a different physical key, without disturbing the other two."""
    if command_name not in ONSLAUGHT_COMMANDS:
        _log.warning('%s rebind_slot() called for unknown command %r -- ignored', MOD_TAG, command_name)
        return

    old_key_name = DESIRED_ONSLAUGHT_KEYS.get(command_name)
    if new_key_name == old_key_name:
        return

    if not _override_applied:
        DESIRED_ONSLAUGHT_KEYS[command_name] = new_key_name
        _log.info('%s %s rebind queued: %s -> %s (Onslaught not active, applies next time it is)', MOD_TAG, command_name, old_key_name, new_key_name)
        return

    orig_key_name = _saved_ammo_original_keys.get(command_name)
    new_key_value = getattr(Keys, new_key_name)

    try:
        removed = _command_mapping.remove(command_name, old_key_name)
        _log.info('%s live rebind: unbound %s from %s (removed=%r)', MOD_TAG, command_name, old_key_name, removed)
    except Exception:
        _log.exception('%s live rebind: failed to unbind %s from %s', MOD_TAG, command_name, old_key_name)

    for conflict_name in [name for name, key in _saved_conflict_bindings.items() if key == old_key_name]:
        swapped_key_name = _conflict_swapped_to.pop(conflict_name, None)
        if swapped_key_name:
            try:
                _command_mapping.remove(conflict_name, swapped_key_name)
            except Exception:
                _log.exception('%s live rebind: failed to unbind %s from %s', MOD_TAG, conflict_name, swapped_key_name)
        try:
            added = _command_mapping.add(conflict_name, old_key_name, isDefault=False)
            _log.info('%s live rebind: restored %s -> %s (added=%r)', MOD_TAG, conflict_name, old_key_name, added)
        except Exception:
            _log.exception('%s live rebind: failed to restore %s -> %s', MOD_TAG, conflict_name, old_key_name)
        del _saved_conflict_bindings[conflict_name]

    for conflict_name in _commands_bound_to_key(new_key_value, exclude=ONSLAUGHT_COMMANDS):
        _saved_conflict_bindings[conflict_name] = new_key_name
        _displace_conflict(conflict_name, new_key_name, orig_key_name)

    try:
        _command_mapping.remove(command_name)
    except Exception:
        _log.exception('%s live rebind: failed to strip %s prior binding', MOD_TAG, command_name)

    try:
        added = _command_mapping.add(command_name, new_key_name, isDefault=False)
        _log.info('%s live rebind: %s -> %s (added=%r)', MOD_TAG, command_name, new_key_name, added)
    except Exception:
        _log.exception('%s live rebind: failed to bind %s -> %s', MOD_TAG, command_name, new_key_name)

    DESIRED_ONSLAUGHT_KEYS[command_name] = new_key_name
    _notify_mapping_changed()
    _save_bindings('live rebind of %s' % command_name)
    _schedule_verify('post-rebind')
    _log.info('%s %s rebound live: %s -> %s', MOD_TAG, command_name, old_key_name, new_key_name)


# --- Runtime helpers: polling path (the mechanism this mod depends on) ----

def _get_functional_state():
    try:
        from gui.prb_control.dispatcher import g_prbLoader
    except ImportError:
        _log.exception('%s could not import gui.prb_control.dispatcher', MOD_TAG)
        return None

    try:
        dispatcher = g_prbLoader.getDispatcher()
    except Exception:
        _log.exception('%s getDispatcher() raised', MOD_TAG)
        return None

    if dispatcher is None:
        return None

    try:
        return dispatcher.getFunctionalState()
    except Exception:
        _log.exception('%s getFunctionalState() raised', MOD_TAG)
        return None


def _apply_for_state(ctrl_type_id, entity_type_id, reason):
    onslaught = _is_onslaught(ctrl_type_id, entity_type_id, _settings.get('applyInTraining', False))
    _log.info('%s %s: ctrlTypeID=%r entityTypeID=%r onslaught=%r', MOD_TAG, reason, ctrl_type_id, entity_type_id, onslaught)

    if onslaught:
        apply_onslaught_override()
    else:
        restore_default_bindings()


def _enforce_current_mode(ctrl_type_id, entity_type_id):
    global _last_state_key

    state_key = (ctrl_type_id, entity_type_id)
    if state_key == _last_state_key:
        return

    _last_state_key = state_key
    _apply_for_state(ctrl_type_id, entity_type_id, 'mode changed')


def _recheck_current_mode():
    state = _get_functional_state()
    if state is None:
        return
    _apply_for_state(getattr(state, 'ctrlTypeID', None), getattr(state, 'entityTypeID', None), 'settings changed')


def _reguard_conflict_keys():
    if not _override_applied:
        return

    changed = False
    for command_name in ONSLAUGHT_COMMANDS:
        key_name = DESIRED_ONSLAUGHT_KEYS[command_name]
        key_value = getattr(Keys, key_name)
        vacated_key_name = _saved_ammo_original_keys.get(command_name)

        for conflict_name in _commands_bound_to_key(key_value, exclude=ONSLAUGHT_COMMANDS):
            _saved_conflict_bindings.setdefault(conflict_name, key_name)
            _log.info('%s conflict re-guard: %s reappeared on %s -- likely a CommandMapping reload around battle entry', MOD_TAG, conflict_name, key_name)
            _displace_conflict(conflict_name, key_name, vacated_key_name)
            changed = True

    if changed:
        _notify_mapping_changed()
        _save_bindings('conflict re-guard')


def _schedule_poll():
    global _poll_callback_id
    token = _poll_generation
    _poll_callback_id = BigWorld.callback(POLL_INTERVAL_SECONDS, partial(_poll, token))


def _cancel_poll():
    global _poll_callback_id
    if _poll_callback_id is not None:
        try:
            BigWorld.cancelCallback(_poll_callback_id)
        except Exception:
            pass
        _poll_callback_id = None


def _poll(token):
    global _poll_callback_id
    _poll_callback_id = None
    if token != _poll_generation:
        return  # a newer fini()/init() cycle superseded this one

    state = _get_functional_state()
    if state is not None:
        _enforce_current_mode(getattr(state, 'ctrlTypeID', None), getattr(state, 'entityTypeID', None))

    _reguard_conflict_keys()

    _schedule_poll()


# --- Localization --------------------------------------------------------

# 语言代码 -> 文案表。加语言就往下加一项即可。
# key 用 WoT 客户端的语言码：zh / zh_tw / en / ru / ja / ko ...
_TRANSLATIONS = {
    'en': {
        'mod_display_name': 'Onslaught Key Remap',
        'slot7_text': 'Tank Ability',
        'slot7_tooltip': '{HEADER}Tank Ability{/HEADER}{BODY}Physical key used to trigger the Onslaught tank ability. This skill is usually bound to consumable slot 7.{/BODY}',
        'slot8_text': 'Artillery Strike',
        'slot8_tooltip': '{HEADER}Artillery Strike{/HEADER}{BODY}Physical key used to call the Onslaught artillery strike. This skill is usually bound to consumable slot 8.{/BODY}',
        'slot9_text': 'Radio / Signal Flare',
        'slot9_tooltip': '{HEADER}Radio{/HEADER}{BODY}Physical key used to trigger the Onslaught radio / signal flare. This skill is usually bound to consumable slot 9.{/BODY}',
        'apply_in_training_text': 'Apply in Training Rooms',
        'apply_in_training_tooltip': '{HEADER}Apply in Training Rooms{/HEADER}{BODY}When checked, this override is also applied in all training rooms.{/BODY}',
    },
    'zh_cn': {
        'mod_display_name': '天梯按键重映射',
        'slot7_text': '坦克特殊能力',
        'slot7_tooltip': '{HEADER}坦克特殊能力{/HEADER}{BODY}用于选择天梯模式坦克技能的物理按键，该技能通常对应消耗品槽位 7。{/BODY}',
        'slot8_text': '火炮打击',
        'slot8_tooltip': '{HEADER}火炮打击{/HEADER}{BODY}用于选择天梯模式火炮打击技能的物理按键，该技能通常对应消耗品槽位 8。{/BODY}',
        'slot9_text': '无线电台/信号弹',
        'slot9_tooltip': '{HEADER}无线电台{/HEADER}{BODY}用于选择天梯模式无线电台/信号弹技能的物理按键，该技能通常对应消耗品槽位 9。{/BODY}',
        'apply_in_training_text': '在训练房中应用',
        'apply_in_training_tooltip': '{HEADER}在训练房中应用{/HEADER}{BODY}选中后，该覆盖设置也将应用于所有训练房。{/BODY}',
    },
    'zh_tw': {
        'mod_display_name': '天梯按鍵重對應',
        'slot7_text': '戰車特殊能力',
        'slot7_tooltip': '{HEADER}戰車特殊能力{/HEADER}{BODY}用於選擇天梯模式戰車技能的實體按鍵，該技能通常對應消耗品槽位 7。{/BODY}',
        'slot8_text': '火砲打擊',
        'slot8_tooltip': '{HEADER}火砲打擊{/HEADER}{BODY}用於選擇天梯模式火砲打擊技能的實體按鍵，該技能通常對應消耗品槽位 8。{/BODY}',
        'slot9_text': '無線電/信號彈',
        'slot9_tooltip': '{HEADER}無線電{/HEADER}{BODY}用於選擇天梯模式無線電/信號彈技能的實體按鍵，該技能通常對應消耗品槽位 9。{/BODY}',
        'apply_in_training_text': '在訓練房中套用',
        'apply_in_training_tooltip': '{HEADER}在訓練房中套用{/HEADER}{BODY}勾選後，該覆寫設定也將套用於所有訓練房。{/BODY}',
    },
    'ru': {
        'mod_display_name': 'Переназначение клавиш Натиска',
        'slot7_text': 'Спецспособность танка',
        'slot7_tooltip': '{HEADER}Спецспособность танка{/HEADER}{BODY}Физическая клавиша для активации спецспособности танка в Натиске. Обычно привязана к слоту расходников 7.{/BODY}',
        'slot8_text': 'Артудар',
        'slot8_tooltip': '{HEADER}Артудар{/HEADER}{BODY}Физическая клавиша для вызова артудара в Натиске. Обычно привязана к слоту расходников 8.{/BODY}',
        'slot9_text': 'Рация / Сигнальная ракета',
        'slot9_tooltip': '{HEADER}Рация{/HEADER}{BODY}Физическая клавиша для запуска рации / сигнальной ракеты в Натиске. Обычно привязана к слоту расходников 9.{/BODY}',
        'apply_in_training_text': 'Применять в тренировочных боях',
        'apply_in_training_tooltip': '{HEADER}Применять в тренировочных боях{/HEADER}{BODY}Если включено, переопределение клавиш также применяется во всех тренировочных боях.{/BODY}',
    },
    'ja': {
        'mod_display_name': 'オンsロート キー再割り当て',
        'slot7_text': '戦車特殊能力',
        'slot7_tooltip': '{HEADER}戦車特殊能力{/HEADER}{BODY}オンsロートの戦車特殊能力を発動する物理キー。通常は消耗品スロット 7 に対応します。{/BODY}',
        'slot8_text': '砲撃',
        'slot8_tooltip': '{HEADER}砲撃{/HEADER}{BODY}オンsロートの砲撃を要請する物理キー。通常は消耗品スロット 8 に対応します。{/BODY}',
        'slot9_text': '無線 / 信号弾',
        'slot9_tooltip': '{HEADER}無線{/HEADER}{BODY}オンsロートの無線 / 信号弾を発動する物理キー。通常は消耗品スロット 9 に対応します。{/BODY}',
        'apply_in_training_text': '訓練部屋でも適用',
        'apply_in_training_tooltip': '{HEADER}訓練部屋でも適用{/HEADER}{BODY}チェックすると、この上書き設定はすべての訓練部屋にも適用されます。{/BODY}',
    },
    'ko': {
        'mod_display_name': '결전 키 재매핑',
        'slot7_text': '전차 특수 능력',
        'slot7_tooltip': '{HEADER}전차 특수 능력{/HEADER}{BODY}결전 모드의 전차 특수 능력을 발동하는 물리 키입니다. 보통 소모품 슬롯 7에 대응합니다.{/BODY}',
        'slot8_text': '포격 지원',
        'slot8_tooltip': '{HEADER}포격 지원{/HEADER}{BODY}결전 모드의 포격 지원을 요청하는 물리 키입니다. 보통 소모품 슬롯 8에 대응합니다.{/BODY}',
        'slot9_text': '무전기 / 신호탄',
        'slot9_tooltip': '{HEADER}무전기{/HEADER}{BODY}결전 모드의 무전기 / 신호탄을 발동하는 물리 키입니다. 보통 소모품 슬롯 9에 대응합니다.{/BODY}',
        'apply_in_training_text': '훈련장에도 적용',
        'apply_in_training_tooltip': '{HEADER}훈련장에도 적용{/HEADER}{BODY}체크하면 이 재정의 설정이 모든 훈련장에도 적용됩니다.{/BODY}',
    },
}

_DEFAULT_LANGUAGE = 'en'


def _get_client_language():
    """尽力探测客户端语言代码（小写，下划线分隔）。探测不到返回空串。"""
    # 1) 官方 helper（新客户端）
    try:
        from helpers import getClientLanguage
        lang = getClientLanguage()
        if lang:
            return str(lang).lower().replace('-', '_')
    except Exception:
        pass

    # 2) settings 里的常见字段
    try:
        import settings
        for attr in ('g_clientLocale', 'g_clientLanguage', 'g_language'):
            lang = getattr(settings, attr, None)
            if lang:
                return str(lang).lower().replace('-', '_')
    except Exception:
        pass

    # 3) BigWorld
    try:
        import BigWorld
        getter = getattr(BigWorld, 'getLanguage', None)
        if getter is not None:
            lang = getter()
            if lang:
                return str(lang).lower().replace('-', '_')
    except Exception:
        pass

    # 4) 区域常量兜底
    try:
        from constants import IS_CHINA, IS_KOREA, IS_JAPAN
        if IS_CHINA:
            return 'zh'
        if IS_KOREA:
            return 'ko'
        if IS_JAPAN:
            return 'ja'
    except Exception:
        pass

    return ''


def _resolve_language_table(lang):
    """把客户端返回的语言代码归一到 _TRANSLATIONS 里存在的 key。"""
    if not lang:
        return _TRANSLATIONS[_DEFAULT_LANGUAGE]

    # 精确命中（zh_cn / zh_tw / en / ru / ja / ko 等）
    if lang in _TRANSLATIONS:
        return _TRANSLATIONS[lang]

    # 把连字符统一成下划线，再试一次（zh-CN -> zh_cn）
    normalized = lang.replace('-', '_').lower()
    if normalized in _TRANSLATIONS:
        return _TRANSLATIONS[normalized]

    # 中文变体归并：zh_hant* / zh_tw / zh_hk -> zh_tw；其余 zh_* -> zh_cn
    if normalized.startswith('zh'):
        if 'hant' in normalized or 'tw' in normalized or 'hk' in normalized:
            return _TRANSLATIONS.get('zh_tw', _TRANSLATIONS[_DEFAULT_LANGUAGE])
        return _TRANSLATIONS.get('zh_cn', _TRANSLATIONS[_DEFAULT_LANGUAGE])

    # 其他语言取主语言码兜底（es_ar -> es，pt_br -> pt，但表里没 es/pt 就回落英语）
    base = normalized.split('_')[0]
    if base in _TRANSLATIONS:
        return _TRANSLATIONS[base]

    return _TRANSLATIONS[_DEFAULT_LANGUAGE]

def _t(key):
    """取当前语言下 key 对应的文案；缺 key 时回落英语，再缺就返回 key 本身。"""
    table = _resolve_language_table(_get_client_language())
    if key in table:
        return table[key]
    return _TRANSLATIONS[_DEFAULT_LANGUAGE].get(key, key)
# --- Runtime helpers: settings panel (ModsSettingsAPI) --------------------

def _build_settings_template():
    return {
        'modDisplayName': _t('mod_display_name'),
        'enabled': True,
        'column1': [
            {
                'type': 'HotKey',
                'text': _t('slot7_text'),
                'tooltip': _t('slot7_tooltip'),
                'value': _settings['slot7Key'],
                'varName': 'slot7Key',
            },
            {
                'type': 'HotKey',
                'text': _t('slot8_text'),
                'tooltip': _t('slot8_tooltip'),
                'value': _settings['slot8Key'],
                'varName': 'slot8Key',
            },
            {
                'type': 'HotKey',
                'text': _t('slot9_text'),
                'tooltip': _t('slot9_tooltip'),
                'value': _settings['slot9Key'],
                'varName': 'slot9Key',
            },
        ],
        'column2': [
            {
                'type': 'CheckBox',
                'text': _t('apply_in_training_text'),
                'tooltip': _t('apply_in_training_tooltip'),
                'value': _settings.get('applyInTraining', False),
                'varName': 'applyInTraining',
            },
        ],
    }


def onModSettingsChanged(linkage, newSettings):
    if linkage != SETTINGS_MOD_LINKAGE:
        return
    global _settings

    resolved = {}
    for command_name, var_name in SETTINGS_VAR_NAMES.items():
        value = newSettings.get(var_name)
        resolved[command_name] = _key_value_to_name(value[0]) if value else None

    seen = {}
    for command_name, key_name in resolved.items():
        if key_name is None:
            continue
        if key_name in seen:
            _log.warning('%s rejected settings change: %s and %s would both bind to %s', MOD_TAG, seen[key_name], command_name, key_name)
            return
        seen[key_name] = command_name

    old_apply_in_training = _settings.get('applyInTraining', False)
    _settings = newSettings
    _log.info('%s settings changed: %r', MOD_TAG, newSettings)

    for command_name, key_name in resolved.items():
        if key_name:
            rebind_slot(command_name, key_name)

    if newSettings.get('applyInTraining', False) != old_apply_in_training:
        _recheck_current_mode()


def onButtonClicked(linkage, varName, value):
    pass


def _sync_desired_keys_from_settings():
    for command_name, var_name in SETTINGS_VAR_NAMES.items():
        value = _settings.get(var_name)
        if not value:
            continue
        key_name = _key_value_to_name(value[0])
        if key_name:
            DESIRED_ONSLAUGHT_KEYS[command_name] = key_name


def _register_settings_panel():
    global _settings

    if g_modsSettingsApi is None:
        _log.info('%s gui.modsSettingsApi not available -- install ModsSettingsAPI and ModsListAPI to get the settings panel; falling back to hardcoded E/R/F', MOD_TAG)
        return

    template = _build_settings_template()
    try:
        saved = g_modsSettingsApi.getModSettings(SETTINGS_MOD_LINKAGE, template)
    except Exception:
        _log.exception('%s getModSettings() raised', MOD_TAG)
        return

    try:
        if saved:
            _settings = saved
            g_modsSettingsApi.registerCallback(SETTINGS_MOD_LINKAGE, onModSettingsChanged, onButtonClicked)
        else:
            _settings = g_modsSettingsApi.setModTemplate(SETTINGS_MOD_LINKAGE, template, onModSettingsChanged, onButtonClicked)
    except Exception:
        _log.exception('%s failed to register with modsSettingsApi', MOD_TAG)
        return

    _sync_desired_keys_from_settings()
    _log.info('%s settings panel registered -- current settings: %r', MOD_TAG, _settings)


# --- Loader lifecycle: init() / fini() last ------------------------------

def init():
    global _started, _poll_generation

    if _started:
        return

    _register_settings_panel()

    _poll_generation += 1
    _schedule_poll()

    _started = True
    _log.info(
        '%s loaded [build: platoon-fix step23] -- Onslaught (LEGACY entityTypeID=%d, '
        'PREQUEUE entityTypeID=%s) now rebinds %s/%s/%s to ammo/ability slots 7/8/9 via '
        'CommandMapping (training rooms: %s)',
        MOD_TAG, ONSLAUGHT_ENTITY_TYPE_ID,
        ONSLAUGHT_QUEUE_TYPE_ID if CTRL_ENTITY_TYPE is not None else 'disabled (CTRL_ENTITY_TYPE import failed)',
        DESIRED_ONSLAUGHT_KEYS['CMD_AMMO_CHOICE_7'],
        DESIRED_ONSLAUGHT_KEYS['CMD_AMMO_CHOICE_8'],
        DESIRED_ONSLAUGHT_KEYS['CMD_AMMO_CHOICE_9'],
        'applied' if _settings.get('applyInTraining') else 'not applied',
    )


def fini():
    global _started, _poll_generation

    _poll_generation += 1  # invalidate any in-flight _poll(token)
    _cancel_poll()

    _started = False
    restore_default_bindings()
    _log.info('%s unloaded', MOD_TAG)
