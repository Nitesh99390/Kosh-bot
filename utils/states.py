from aiogram.fsm.state import State, StatesGroup


class FileEditStates(StatesGroup):
    waiting_for_title = State()
    waiting_for_tags = State()
    waiting_for_caption = State()


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
