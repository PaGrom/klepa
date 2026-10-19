import pytest

from klepa_core.locale import LocaleError, available_locales, load_locale, plural_category

COMPLETE = """
plural = "one-other"

[texts]
stage1 = "a"
start = "b"
no_commands = "c"
unsupported = "d"
too_large = "e"
failed = "f"
hold = "g"
held_dropped = "h"
turn_failed = "t"
original_failed = "not sent: {name}"
interim_listening = "interim_listening text"
interim_reading = "interim_reading text"
interim_listened = "interim_listened text"
interim_read = "interim_read text"
interim_unread = "interim_unread text"
interim_later = "interim_later text"
interim_cancelled = "interim_cancelled text"
cancel_button = "cancel_button text"
cancel_which = "cancel_which text"
cancel_all = "cancel_all text"
button_expired = "button_expired text"
label_voice = "label_voice text"
label_photo = "label_photo text"
label_video = "label_video text"
label_file = "label_file text"
draft_sheet = "pages: {n}"
draft_done_button = "draft_done_button text"
draft_order_button = "draft_order_button text"
draft_no_button = "draft_no_button text"
draft_order_hint = "draft_order_hint text"
draft_declined = "draft_declined text"
draft_building = "draft_building text"
draft_built = "built: {n}"
draft_failed = "draft_failed text"
draft_too_large = "draft_too_large text"
draft_name = "Document {date}"
draft_name_version = "Document {date}, version {version}"

[receipts]
one_file = "got it"
one_voice = "got the voice message"
voices = "got {n} voice messages"
files = { one = "got {n} file", other = "got {n} files" }

[cancel]
words = ["Cancel", "stop"]

[spaces]
shared_folder = "Shared"
private_keywords = ["just for me"]

[service]
status_button = "Status"
pause_button = "Pause"
resume_button = "Resume"
not_yet = "later"
button_expired = "expired"
all_good = "good"
attention = "attention"
line = "{headline} {host} {last_intake} {snapshot} {documents} {pending_copies} {failed_copies} {unknown_sends}"
snapshot = "{generation} {hash} {integrity}"
never = "never"
no_snapshot = "none"
ok = "ok"
unavailable = "unavailable {error}"
host_off = "off"
host_starting = "starting"
host_running = "running"
host_hold = "hold"
host_paused = "paused"
host_stopped = "stopped"
alert_documents_unavailable = "docs {error}"
alert_documents_timeout = "timeout {error} {interpreter}"
alert_permission_denied = "denied {error} {interpreter}"
alert_channel_dead = "dead {error}"
alert_snapshot_failed = "snapshot {error}"
alert_restarted = "restarted"
alert_album_private_after_copy = "album {count}"
alert_host_failed = "host {reason}"
alert_host_silent = "silent"
alert_host_exited = "exited"
alert_host_conflict = "conflict"
alert_host_not_polling = "not polling"
alert_host_send_failed = "refused {error}"
alert_host_send_refused = "not taken {reason}"
alert_host_turn_failed = "turn {reason}"
alert_host_model_auth = "token"
alert_host_model_unavailable = "model {reason}"
alert_egress_failed = "egress {error}"
alert_processing_unavailable = "unread {missing}"
"""


def test_shipped_locales():
    assert {"en", "ru", "sr", "uk"} <= set(available_locales())


@pytest.mark.parametrize(
    ("rule", "n", "category"),
    [
        ("one-other", 1, "one"),
        ("one-other", 2, "other"),
        ("one-other", 21, "other"),
        ("one-few-many", 1, "one"),
        ("one-few-many", 3, "few"),
        ("one-few-many", 5, "many"),
        ("one-few-many", 11, "many"),
        ("one-few-many", 12, "many"),
        ("one-few-many", 21, "one"),
        ("one-few-many", 22, "few"),
        ("one-few-many", 111, "many"),
        ("one-few-other", 1, "one"),
        ("one-few-other", 4, "few"),
        ("one-few-other", 5, "other"),
        ("one-few-other", 14, "other"),
        ("one-few-other", 21, "one"),
    ],
)
def test_plural_categories(rule, n, category):
    assert plural_category(rule, n) == category


def test_english_receipts_and_texts():
    en = load_locale("en")
    assert en.receipt(["file"]) == "📄 got it"
    assert en.receipt(["photo"]) == "📄 got it"
    assert en.receipt(["voice"]) == "🎧 got the voice message"
    assert en.receipt(["voice", "voice"]) == "🎧 got 2 voice messages"
    assert en.receipt(["file", "photo", "voice"]) == "📄 got 3 files"
    assert en.text("stage1").startswith("For now I only accept files")
    assert en.shared_folder == "Shared"
    assert "just for me" in en.private_keywords


@pytest.mark.parametrize("code", ["../en", "EN", "", "e n", "en/../ru", None])
def test_bad_locale_codes_are_refused(code):
    with pytest.raises(LocaleError):
        load_locale(code)


def test_unknown_locale_is_refused():
    with pytest.raises(LocaleError, match="unknown locale"):
        load_locale("xx")


def test_complete_locale_from_a_directory_loads(tmp_path):
    (tmp_path / "xx.toml").write_text(COMPLETE, encoding="utf-8")
    assert load_locale("xx", directory=tmp_path).receipt(["file"] * 2) == "got 2 files"
    assert load_locale("xx", directory=tmp_path).cancel_words == ("cancel", "stop")  # compared case-blind


def test_a_locale_without_cancel_words_is_refused(tmp_path):
    (tmp_path / "xx.toml").write_text(COMPLETE.replace('words = ["Cancel", "stop"]', "words = []"), encoding="utf-8")
    with pytest.raises(LocaleError, match=r"cancel\.words"):
        load_locale("xx", directory=tmp_path)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda t: t.replace('failed = "f"\n', ""), "failed"),
        (lambda t: t.replace('hold = "g"\n', ""), "hold"),
        (lambda t: t.replace('held_dropped = "h"\n', ""), "held_dropped"),
        (lambda t: t.replace('plural = "one-other"', 'plural = "one-few-many"'), "few"),
        (lambda t: t.replace('voices = "got {n} voice messages"', 'voices = "got voice messages"'), "voices"),
        (lambda t: t.replace('private_keywords = ["just for me"]', "private_keywords = []"), "private_keywords"),
        (lambda t: t.replace('plural = "one-other"', 'plural = "dual"'), "plural"),
        (lambda t: t.replace('original_failed = "not sent: {name}"', 'original_failed = "not sent"'), "needs {name}"),
        (
            lambda t: t.replace('original_failed = "not sent: {name}"', 'original_failed = "{name} {file}"'),
            "placeholder",
        ),
    ],
)
def test_incomplete_locale_is_refused(tmp_path, mutation, message):
    (tmp_path / "xx.toml").write_text(mutation(COMPLETE), encoding="utf-8")
    with pytest.raises(LocaleError, match=message):
        load_locale("xx", directory=tmp_path)


def test_english_service_texts():
    en = load_locale("en")
    assert en.service_text("status_button") == "Status"
    line = en.service_text(
        "line",
        headline=en.service_text("all_good"),
        host=en.service_text("host_running"),
        last_intake="2026-10-05 09:00",
        snapshot=en.service_text("snapshot", generation=3, hash="a1b2c3d4", integrity="ok"),
        documents=en.service_text("ok"),
        pending_copies=0,
        failed_copies=0,
        unknown_sends=0,
    )
    assert line.startswith("✅ All good · host: running")
    assert "#3 a1b2c3d4" in line
    denied = en.service_text("alert_permission_denied", error="PermissionError", interpreter="/Applications/X")
    assert "/Applications/X" in denied


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda t: t.replace('status_button = "Status"\n', ""), "service.status_button"),
        (lambda t: t.replace('pause_button = "Pause"\n', ""), "service.pause_button"),
        (lambda t: t.replace('host_paused = "paused"\n', ""), "service.host_paused"),
        (lambda t: t.replace("{headline} {host} ", "{headline} "), "needs {host}"),
        (lambda t: t.replace('alert_host_failed = "host {reason}"', 'alert_host_failed = "host"'), "needs {reason}"),
        (
            lambda t: t.replace(
                'alert_host_model_unavailable = "model {reason}"', 'alert_host_model_unavailable = "m"'
            ),
            "needs {reason}",
        ),
        (lambda t: t.replace('unavailable = "unavailable {error}"', 'unavailable = "gone"'), "needs {error}"),
        (lambda t: t.replace('ok = "ok"', 'ok = "ok {surprise}"'), "unknown placeholder"),
    ],
)
def test_incomplete_service_texts_are_refused(tmp_path, mutation, message):
    (tmp_path / "xx.toml").write_text(mutation(COMPLETE), encoding="utf-8")
    with pytest.raises(LocaleError, match=message):
        load_locale("xx", directory=tmp_path)


def test_every_language_s_dialog_of_change_the_order_fits_a_press_answer():
    """Telegram shows at most 200 characters of the answer to a press."""
    for code in available_locales():
        assert len(load_locale(code).text("draft_order_hint")) <= 200, code
