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

[receipts]
one_file = "got it"
one_voice = "got the voice message"
voices = "got {n} voice messages"
files = { one = "got {n} file", other = "got {n} files" }

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
alert_host_conflict = "conflict"
alert_host_not_polling = "not polling"
alert_host_send_failed = "refused {error}"
alert_host_send_refused = "not taken {reason}"
alert_egress_failed = "egress {error}"
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
        (lambda t: t.replace('unavailable = "unavailable {error}"', 'unavailable = "gone"'), "needs {error}"),
        (lambda t: t.replace('ok = "ok"', 'ok = "ok {surprise}"'), "unknown placeholder"),
    ],
)
def test_incomplete_service_texts_are_refused(tmp_path, mutation, message):
    (tmp_path / "xx.toml").write_text(mutation(COMPLETE), encoding="utf-8")
    with pytest.raises(LocaleError, match=message):
        load_locale("xx", directory=tmp_path)
