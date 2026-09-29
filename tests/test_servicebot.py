import pytest

from helpers import MEMBER, OWNER, STRANGER
from klepa_core import db
from klepa_core.servicebot import bind_owner_chat, new_bind_code


@pytest.fixture
def setup(make_config):
    cfg = make_config()
    conn = db.connect(cfg.core_db_path)
    db.migrate(conn)
    db.seed(conn, cfg)
    return cfg, conn


def test_bind_codes_are_long_and_unique():
    codes = {new_bind_code() for _ in range(100)}
    assert len(codes) == 100
    assert all(len(code) >= 22 for code in codes)


async def test_binding_needs_the_code_from_the_owner_and_a_terminal_yes(setup, service_tg, service_api):
    cfg, conn = setup
    service_tg.add_text(STRANGER, "/start CODE")
    service_tg.add_text(MEMBER, "/start CODE")
    service_tg.add_text(OWNER, "/start CODE", chat_id=-100123, chat_type="group")
    service_tg.add_text(OWNER, "/start WRONG")
    service_tg.add_text(OWNER, "/start CODE")
    questions = []

    def confirm(question):
        questions.append(question)
        return True

    chat = await bind_owner_chat(cfg, service_api, conn, "CODE", confirm, deadline_seconds=5, poll_timeout=1)
    assert chat == OWNER
    assert db.owner_service_chat(conn) == OWNER
    assert len(questions) == 1
    assert str(OWNER) in questions[0]
    assert await service_api.get_updates(None, 0) == []  # the code was acknowledged: Core never sees it


async def test_binding_declined_in_the_terminal_stores_nothing(setup, service_tg, service_api):
    cfg, conn = setup
    service_tg.add_text(OWNER, "/start CODE")
    chat = await bind_owner_chat(cfg, service_api, conn, "CODE", lambda q: False, deadline_seconds=5, poll_timeout=1)
    assert chat is None
    assert db.owner_service_chat(conn) is None


async def test_binding_gives_up_after_the_deadline(setup, service_tg, service_api):
    cfg, conn = setup
    chat = await bind_owner_chat(cfg, service_api, conn, "CODE", lambda q: True, deadline_seconds=0.5, poll_timeout=0)
    assert chat is None
