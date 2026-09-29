# Configuration

Core reads one TOML file, passed with `--config`. Keep it **outside** the repository. Real tokens, names and Telegram IDs never go into the repository.

Start from [`config/example.toml`](../config/example.toml).

## Top level

| Key | Required | Meaning |
|---|---|---|
| `timezone` | yes | IANA time zone, for example `"Europe/Berlin"`. The month folder of every original (`incoming/YYYY/MM`) follows local time in this zone. |
| `locale` | yes | `en`, `ru`, `sr` or `uk`. Sets what Core says, the default name of the shared folder and the default private keywords. |

## `[paths]`

| Key | Required | Meaning |
|---|---|---|
| `data_dir` | yes | Absolute path to Core's data directory. It is created with mode 0700 and must be on a local disk, never inside iCloud (`Library/Mobile Documents`) or CloudStorage (`Library/CloudStorage`). |
| `documents_dir` | yes | Absolute path to the folder where copies and signed cards appear. Core treats it as a plain folder; anything may sync it. `init` creates it. `run` never does, because a folder on an unmounted volume must not be replaced by an empty local one. |

## `[telegram]`

| Key | Required | Meaning |
|---|---|---|
| `token_file` | yes | Absolute path to a file that holds only the bot token. The file must be mode 0600. |
| `api_root` | no | Default `https://api.telegram.org`. Plain `http://` is accepted only for loopback test servers. |

## `[spaces]`

| Key | Default | Meaning |
|---|---|---|
| `default` | `"shared"` | Where a file goes when its caption has no private keyword: `"shared"` or `"personal"`. |
| `shared_folder` | from the locale | Name of the shared folder inside `documents_dir`, for example `Shared`. |

## `[intake]`

| Key | Default | Meaning |
|---|---|---|
| `max_file_bytes` | `20971520` | Files above this size are recorded as `too_large`, and the sender is asked to send them another way. The Bot API does not hand bots files over 20 MB. |
| `batch_window_seconds` | `2.0` | Attachments of one chat that arrive less than this apart get one receipt. |
| `album_quiet_seconds` | `60` | Album items are copied only after no new item of that album has arrived for this long, so a late private caption still keeps the whole album out of the shared folder. |
| `poll_timeout_seconds` | `30` | Long-polling timeout for `getUpdates`. |
| `private_keywords` | from the locale | Caption words that make a file, or a whole album, personal. They match whole words, ignoring case and spacing. |

## `[schedule]`

| Key | Default | Meaning |
|---|---|---|
| `snapshot_at` | `"03:30"` | Local time (`HH:MM`) of the daily signed snapshot of `core.db`. `"off"` disables it. |
| `daily_line_at` | `"09:00"` | Local time of the service bot's daily line. `"off"` disables it. The line needs the service bot. |

## `[service_bot]`

Optional. Without it Core runs without a service bot, and alerts are only logged.

| Key | Required | Meaning |
|---|---|---|
| `token_file` | yes | Absolute path to a file that holds only the service bot's token, mode 0600. It must differ from `telegram.token_file`. A missing or unreadable token turns the service bot off; Core keeps running. |
| `api_root` | no | Defaults to `telegram.api_root`. |

## `[[members]]`

One table per family member. Exactly one member has the role `owner`.

| Key | Required | Meaning |
|---|---|---|
| `person_id` | yes | Stable id, `^[a-z0-9_-]{1,32}$`. Used in space names such as `personal:<person_id>`. |
| `telegram_id` | yes | The member's Telegram user id. Only private messages from this id are accepted. |
| `name` | yes | Display name; also the name of the member's personal folder. |
| `role` | no | `owner` or `member` (default). |

## Commands

```bash
uv run python -m klepa_core init --config PATH   # create the data layout, the signing key, core.db and the documents folder; keep keys/ out of Time Machine
uv run python -m klepa_core run --config PATH    # run Core in the foreground until SIGINT or SIGTERM
uv run python -m klepa_core service-bot bind --config PATH      # bind the service bot to the owner's chat; stop Core first
python -m klepa_core service install --config PATH [--python P] # check, install and start the launchd agent
python -m klepa_core service restart                            # for example after allowing the documents folder
python -m klepa_core service status
python -m klepa_core service uninstall                          # stop Core for good and remove the agent
python -m klepa_core keys paper-backup --config PATH            # print the signing key for a paper copy; run it yourself
python -m klepa_core keys restore --config PATH                 # type the paper copy back in (stdin)
```

Exit codes:
- `0` — normal exit;
- `2` — invalid config or key file;
- `3` — another Core already runs on this data directory;
- `4` — the service bot was not bound;
- `5` — launchd refused the agent, or the interpreter cannot run Core.
