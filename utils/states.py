from aiogram.fsm.state import State, StatesGroup


class FileEditStates(StatesGroup):
    waiting_for_title = State()
    waiting_for_tags = State()
    waiting_for_caption = State()
    waiting_for_share_pin = State()


class ShareUnlockStates(StatesGroup):
    waiting_for_file_pin = State()
    waiting_for_pack_pin = State()


class PackStates(StatesGroup):
    collecting_items = State()
    waiting_for_title = State()
    waiting_for_pin = State()


class GroupStates(StatesGroup):
    waiting_for_personal_name = State()
    waiting_for_shared_name = State()
    waiting_for_invite_code = State()
    waiting_for_rename = State()


class AccountStates(StatesGroup):
    waiting_for_verify_code = State()


class SearchStates(StatesGroup):
    waiting_for_query = State()


class AdminStates(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_ban_uid = State()
