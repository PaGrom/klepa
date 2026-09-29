"""Shared test data. Synthetic members only: real names and IDs never go into the repository."""
OWNER = 111111
MEMBER = 222222
STRANGER = 999999
TEST_TOKEN = "123:TEST-TOKEN"

BASE_CONFIG = """
timezone = "Europe/Berlin"

[paths]
data_dir = "{data_dir}"
documents_dir = "{documents_dir}"

[telegram]
token_file = "{token_file}"
api_root = "{api_root}"

[intake]
batch_window_seconds = 0.2
poll_timeout_seconds = 1

[[members]]
person_id = "owner"
telegram_id = 111111
name = "Owner"
role = "owner"

[[members]]
person_id = "member"
telegram_id = 222222
name = "Member"
role = "member"
"""
