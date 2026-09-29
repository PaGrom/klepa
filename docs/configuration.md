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
| `documents_dir` | yes | Absolute path to the folder where copies and signed cards appear, for example a Google Drive folder. `init` creates it. `run` never does, because an unmounted Drive folder must not be replaced by an empty local one. |

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
| `poll_timeout_seconds` | `30` | Long-polling timeout for `getUpdates`. |
| `private_keywords` | from the locale | Caption words that make a file, or a whole album, personal. They match whole words, ignoring case and spacing. |

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
uv run python -m klepa_core init --config PATH   # create the data layout, the signing key, core.db and the documents folder
uv run python -m klepa_core run --config PATH    # run Core in the foreground until SIGINT or SIGTERM
```

Exit codes:
- `0` — normal exit;
- `2` — invalid config or key file;
- `3` — another Core already runs on this data directory.
