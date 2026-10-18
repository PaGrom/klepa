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

## `[host]`

Optional. With it, members' text goes to an agent host behind the gatekeeper ([architecture](architecture.md#host-interface)); without it, Core answers text itself.

| Key | Default | Meaning |
|---|---|---|
| `api_port` | `19201` | The gatekeeper's Telegram-shaped API for the host, on `127.0.0.1`. The host's API root is `http://127.0.0.1:<api_port>`, exactly. |
| `proxy_port` | `19202` | The egress proxy on `127.0.0.1`, the host's proxy for all its HTTP. |
| `egress_allow` | `[]` | Exact `"name:port"` destinations the host may reach through the proxy, for example `"api.anthropic.com:443"`. No masks and no addresses. The gatekeeper is always allowed. |
| `socket` | `<data_dir>/run/adapter.sock` | The adapter plugin's Unix socket, in a directory of its own (0700). The path must fit in 103 bytes. |
| `gateway` | `"launchd"` | `"launchd"`: Core runs its own OpenClaw gateway under launchd (install it with `host install`). `"external"`: somebody else runs the gateway, and only the heartbeat and the live probe gate it. |
| `gateway_port` | `19300` | The gateway's own port on `127.0.0.1`. OpenClaw also takes some ports above it, so keep it well away from another OpenClaw's (18789 by default). |
| `model` | `"anthropic/claude-sonnet-5"` | The one model the gateway may use, through Anthropic's API. |
| `runtime_dir` | `~/Library/Application Support/Klepa/openclaw` | Where `host install` puts Node, OpenClaw and the adapter plugin. |

At its first `run` or `host install` with this section Core makes two secrets, each 0600 in `keys/`: `host-bot.token`, the host's fake family bot token, and `adapter.key`, which signs the messages between the adapter plugin and Core. The host's configuration points at both files; neither goes into chats, logs or the repository.

The gateway reaches the model only through the egress proxy, so `egress_allow` needs `"api.anthropic.com:443"`; `host install` warns without it. Its own files live in `<data_dir>/host/`, which Core writes and the gateway may not change.

## `[processing]`

Optional, macOS only. With it, Core reads what people send, in a sandbox: voice messages and the sound of videos are transcribed, PDFs, photos, Word documents and spreadsheets give their text, and search finds a record by what it says ([architecture](architecture.md#processing)). Without it nothing is read, and search knows a record by its name and caption.

| Key | Default | Meaning |
|---|---|---|
| `whisper` | `/opt/homebrew/bin/whisper-cli` | whisper.cpp's program (Homebrew's `whisper-cpp`). |
| `whisper_model` | none | The speech model, a whisper.cpp ggml file such as `ggml-large-v3-turbo-q8_0.bin`. Without it voice messages wait unread. |
| `ffmpeg` | `/opt/homebrew/bin/ffmpeg` | Turns a recording or a video's sound into the WAV whisper reads, and a HEIC photo into PNG. |
| `ffprobe` | `/opt/homebrew/bin/ffprobe` | Reads an image's size before OCR, and whether a video has sound. |
| `pdftotext` | `/opt/homebrew/bin/pdftotext` | Poppler's: a PDF's text layer. |
| `tesseract` | `/opt/homebrew/bin/tesseract` | OCR of photos and images. |
| `ocrmypdf` | `ocrmypdf` beside Core's interpreter | OCR of scanned PDF pages, from the engine's `processing` extra. |
| `tessdata` | `~/Library/Application Support/Klepa/tessdata` | Tesseract's languages: `<language>.traineddata` for each OCR language, with Homebrew's `configs`, `tessconfigs`, `pdf.ttf`, `eng` and `osd` linked in. |
| `ocr_languages` | by the locale: `eng`; `rus+eng`; `ukr+rus+eng`; `srp+srp_latn+eng` | Tesseract's languages joined by `+`. More languages make OCR slower. |
| `textutil` | `/usr/bin/textutil` | macOS's: Word, RTF, OpenDocument text and HTML as text. |

The sandbox lets a reader read, of the home folder and the other hidden folders, only its job, the programs and the speech model themselves, `tessdata`, and Core's interpreter and environment. Core reads nothing, and says so, when one of these is the home folder or holds Core's data or documents folder, which it would open, or when the speech model or `tessdata` is inside Core's data or documents folder, which the readers never see. So keep the programs outside the home folder (Homebrew's are) or in the engine's environment, and the model and the languages outside Core's folders. Whatever is missing, Core says once through the service bot; the originals it would read wait and are read after a start that has it.

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
python -m klepa_core host install --config PATH                 # put the gateway's Node, OpenClaw, adapter and config in place
python -m klepa_core host login --config PATH                   # give the gateway the model's setup-token: pasted without echo, or piped in
python -m klepa_core host status --config PATH                  # the runtime, the launchd agent, model access, Core's view
python -m klepa_core host uninstall --config PATH               # unload the gateway; refused while Core runs, which would load it again
```

Exit codes:
- `0` — normal exit;
- `2` — invalid config or key file;
- `3` — another Core already runs on this data directory;
- `4` — the service bot was not bound;
- `5` — launchd refused the agent, or the interpreter cannot run Core;
- `6` — the gateway's runtime could not be installed, or OpenClaw refused a step.
