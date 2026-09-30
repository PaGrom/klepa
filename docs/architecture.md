# Architecture

Klepa splits a family assistant into two parts.

- **Core** is this repository, written in Python. It:
  - owns the family bot;
  - stores every original;
  - decides who may see what;
  - sends its own messages;
  - later, runs reminders, search and self-checks.
- **The agent host** (OpenClaw first, Hermes later) runs the language model, conversation sessions and the tool loop. It never holds the real bot token and never touches storage directly.

The rule between them: **the model thinks, Core acts.** Every action the model asks for is a proposal. Core checks the rights, the format and the source, then executes it or refuses with a clear reason. All the valuable state lives in Core, so the host is replaceable and its conversation history can be reset without loss.

## Components

The target design:

```
Telegram (family bot)  <-->  GATEKEEPER (Core)  <-->  agent host (OpenClaw)
                                                      ├─ adapter plugin ──> Core
                                                      └─ tool package ────> Core
Internet (model API only)  <--  EGRESS PROXY (Core)  <--  other host HTTP
Telegram (service bot)  <-->  Core
```

- **Gatekeeper.** The only process that talks to the family bot. It journals updates, takes attachments in, filters senders, and sends Core's own messages and buttons. From stage 1c it serves the host text-only updates through a Telegram-compatible API with an exact method allow-list.
- **Supervision** (stage 1c). Core starts, holds and stops the host gateway: RUNNING, HOLD, STOPPED.
- **Egress proxy** (stage 1c). The host's other HTTP traffic may reach only the model API. It fails closed.
- **Storage.** `core.db`, the originals, the documents folder, signed cards and signed snapshots.
- **Service bot.** Alerts, the daily line and a Status button for the owner, with no model behind them.
- **Watchdog** (stage 4). A tiny launchd job that can only send a fixed alarm.
- **Maintenance agent** (stage 7). It works in a strict mode, and Core executes its plan from an allow-list.

Stages 1a and 1b implement the gatekeeper's intake, storage, receipts and spaces, the service bot, snapshots and the launchd service. The parts marked with a stage come later (see [Roadmap](#roadmap)).

## Intake

Code: `klepa_core/gatekeeper/`, `klepa_core/journal.py`, `klepa_core/telegram/`.

1. **One poller.** Core long-polls `getUpdates` for `message` and `callback_query` updates. No other process may use the token.
2. **Journal first.** Each batch is stored in the inbound journal before the next `getUpdates` offset acknowledges it. The journal is a separate SQLite file, so restoring `core.db` never touches it. It is written in one durable transaction. After a crash an update is either redelivered by Telegram or already in the journal.
3. **Classification.** A message is accepted only if all of these hold:
   - it is a `message` update;
   - it comes from a private chat;
   - `chat.id` equals `from.id`;
   - the sender is a configured member and not a bot.

   Strangers, groups and channels are rejected, and other update types are ignored. Both are logged without message text.
4. **Attachments.**
   - Kinds: documents, photos (the largest size), voice messages, audio and video.
   - Size: the Bot API hands bots at most 20 MB. Larger files are recorded as `too_large`, and the sender is asked to send them another way.
   - Failures: a failed download or save is retried. After five failures the sender is asked to send the file again. A silent connection times out after 60 seconds.
5. **Text.** In stage 1a text and commands get fixed replies. From stage 1c text goes to the host.

**Identity comes from Telegram.** A file's sender (`authenticated_subject`) is the Telegram sender of the update Core received itself, never a name claimed in a caption.

## Storage

Code: `klepa_core/evidence.py`, `klepa_core/durable.py`, `klepa_core/names.py`, `klepa_core/cards.py`, `klepa_core/db.py`.

The data directory must be mode 0700, on a local disk, and never inside iCloud or CloudStorage:

```
<data_dir>/
├─ keys/                      0700: the signing key and the bot tokens, each 0600
├─ incoming/YYYY/MM/          originals, <evidence id>-<name>
├─ inbound-journal/inbound.db
├─ core.db                    members, spaces, evidence, outbound, event log
├─ core.lock                  one Core per data directory
├─ snapshots/                 signed snapshots of core.db, <day>-g<generation>/
├─ logs/                      Core's stdout and stderr under launchd, 0600, tokens redacted
└─ .metadata_never_index      keeps Spotlight out
```

**How an original is written.**
1. It goes to `incoming/YYYY/MM/` through a temporary file flushed with `F_FULLFSYNC`, followed by a rename that never replaces. A complete file left by an interrupted attempt is adopted. Other bytes under the same name are refused.
2. Its row is inserted into `core.db` (WAL, `synchronous=FULL`, `fullfsync=ON`).
3. After the batch's receipt is queued, the file is copied to the documents folder. The copy is written the same way, read back and compared by SHA-256. A signed card, `<id>.card.json`, is written next to it.

**Guarantees:**
- a row never points to a missing file;
- the code never modifies or deletes an original;
- month folders follow the installation's timezone.

**Evidence id.** It is an HMAC of the Telegram message key under the installation key. It stays the same for a given message, so a retry after a crash finds the file it already wrote. Different installations produce different ids.

**Disk names (D27).** A file is stored as `<id>-<sanitized name>`:
- NFC;
- no path separators, control or bidirectional characters;
- at most 255 bytes, with the extension kept.

Photos, voice and video arrive without a name, so they become `<id>-photo.jpg`, `<id>-voice.ogg` or `<id>-video.mp4`. The original name lives only in the database and the card.

**Cards (D24).** A card is JSON holding:
- the evidence id, space and kind;
- the original and disk names, MIME type, size and SHA-256;
- the arrival time and channel;
- the chat and message ids;
- the authenticated sender, the caption and the tags.

It is signed with HMAC-SHA256 over canonical JSON. A card without a valid signature is ignored.

**The documents folder is a plain folder.** Core knows nothing about what, if anything, syncs it: a cloud client, a NAS or nothing at all.

**It may be unavailable.** Its volume may not be mounted yet, macOS may deny access, or the disk may be full. Intake does not depend on that folder: files still land in `incoming/` and receipts still go out, while copies stay pending and are retried. Core never recreates a missing documents root, because the root may live on a volume that is not mounted yet.

**It may hang.** A stalled sync client, a network volume that went away or an unanswered macOS prompt can block a file call for minutes. Every call into the folder therefore runs on a worker thread, one at a time, with a timeout (20 seconds, 10 minutes for copying or pruning snapshots); while one call hangs, the others fail at once. Core's event loop never waits on the folder. A problem is reported to the owner once it has lasted five minutes, because at login the volume may mount after Core starts.

**Conflicts.** If the documents folder already holds different bytes under Core's name, the copy is marked as a conflict. It counts in the daily line and is tried again after each restart, once the conflict is cleared.

**Snapshots (D24).**
- Every night (03:30 by default) `VACUUM INTO` writes a consistent copy of `core.db` on the local disk.
- SQLite's integrity check and the SHA-256 go into a manifest with a monotonic generation number, signed with the snapshot key. Generation numbers are never reused.
- A snapshot is built under a `.partial-` name and renamed when complete, locally and then in `documents/_klepa/snapshots/<day>-g<generation>/`. A crash leaves nothing under a final name, and the start-up sweep removes the rest.
- Retention counts good snapshots only: the latest of each of the last 14 days and the first of each of the last 12 months. A snapshot that failed its integrity check stays two weeks for diagnosis and is never copied.
- `privacy_journal_head` is reserved for stage 3.
- The signing key lives in `keys/` and on paper: `keys paper-backup` prints it for the owner.

## Spaces and privacy

Code: `klepa_core/spaces.py`, `klepa_core/gatekeeper/service.py`.

Every record belongs to a space: `shared`, or `personal:<member>`.

**The caption decides (D32).** The space is chosen at intake, from the caption. A private keyword puts the file into the sender's personal space: "just for me", "private", and the equivalents in each locale. Keywords match whole words, ignoring case and spacing.

**Albums move together.** Telegram puts an album's caption on one of its items, so a private caption on any item makes the whole album personal. Copies of album items wait until no new item of the album has arrived for a while (`intake.album_quiet_seconds`, a minute by default), so a private caption that arrives late still keeps the whole album out of the shared folder.

**Moving later.** Making a file private after the fact is a separate tool, `make_private` (stage 3). It will record the move in the signed privacy journal.

**Folders.** The documents folder holds one shared folder, named by the locale (for example `Shared`), and one folder per member, named after the member.

## Service bot and health

Code: `klepa_core/servicebot.py`, `klepa_core/alerts.py`, `klepa_core/health.py`, `klepa_core/schedule.py`, `klepa_core/app.py`.

**A narrow channel.** A second bot talks only to the owner's private chat:
- it is bound once, with a 128-bit code sent from the owner's own account and confirmed in the terminal;
- it accepts an update only when the chat is private, the chat is the bound one and the sender is the owner;
- it sends fixed templates with no family data: counts, times and hashes;
- every send is an event in the log.

**Alerts,** at most one per class per period:
- the documents folder is unavailable, macOS denies access, or the folder does not answer (the last two name the interpreter to allow; a folder that hangs often means a macOS prompt is waiting for an answer);
- Telegram refuses the family bot's polling;
- the daily snapshot failed;
- Core restarted after an unexpected stop;
- an album became private after part of it was copied.

**The daily line.** Once a day (09:00 by default) the bot sends one line: the last intake; the newest snapshot's
generation, hash and integrity; the state of the documents folder; and the counts of waiting copies, copy conflicts
and sends left unconfirmed in the last 48 hours. It says "Needs attention" when any of these is wrong. A missing line
means trouble. The Status button under the line sends a fresh one. Its id is one-time, 128-bit, and bound to the chat,
the message and a lifetime of a week.

**The documents probe.** Core writes, reads back, lists and removes a small file with a unique name. Listing matters:
macOS lets a background process without permission write a known path in a protected folder but refuses to list it.

**Daily jobs** run once a day after their time, and once on wake after sleep or downtime. A job is marked as run
before it starts, so a job that brings Core down is not repeated in a loop.

**Fault isolation.** Only family intake can stop Core; launchd then starts it again. The service bot, the scheduler,
the snapshot copier and the start-up probe restart themselves after a failure, and a broken service bot token turns
only the service bot off.

## Delivery

Code: `klepa_core/gatekeeper/outbox.py`, `klepa_core/gatekeeper/receipts.py`.

**The outbox.** Core's own messages, receipts and fixed replies, go through an outbox in `core.db` with idempotency keys. A message moves from PENDING to SENDING and ends as CONFIRMED, RETRY_WAIT, UNKNOWN or FAILED.

**Honest statuses.** A send that may have left without being confirmed becomes UNKNOWN. It is never resent automatically.

**Receipts.** A batch is the attachments of one chat that arrive less than the batch window apart, such as an album. It gets one receipt ("📄 got 5 files"). The batch stays open while an attachment of that chat is still downloading. A message repeated under a new update id is covered by the receipt of its original.

**Format.** Messages are plain text, with link previews turned off.

**Later.** Stage 4 brings the fenced delivery state machine with leases and generations, together with reminders.

## Locales

Code: `klepa_core/locale.py`, `klepa_core/locales/`.

Everything Core says itself, the default shared folder name and the private keywords live in `locales/<code>.toml`. Shipped locales:
- `en` — English;
- `ru` — Russian;
- `sr` — Serbian, Latin script;
- `uk` — Ukrainian.

The config must name one. Each file declares its plural rule:
- `one-other`;
- `one-few-many`;
- `one-few-other`.

Receipts use the matching form ("1 file", "2 files"). Loading a locale checks that every text, form and keyword list is present.

## Security model

- **Secrets.** The bot token exists only in a 0600 file. It never reaches databases, logs, events, URLs or exception texts. Errors never include the request URL, because that URL carries the token.
- **Event log.** It keeps metadata only: no message text, captions, tokens, file paths or URLs.
- **Content is data.** Incoming content (messages, documents, recognized text, transcripts) is treated as data, never as instructions.
- **Trust boundary.** The trust boundary is the macOS user account that runs Core.
- **Keys and backups.** `keys/` is excluded from Time Machine at `init` and at `service install`. The snapshot signing key has a paper copy.
- **Logs.** Under launchd, Core's stderr goes to `logs/` (0600), and anything shaped like a bot token is redacted.
- **Egress** (stage 1c). The host reaches only the model API, through Core's proxy. The proxy checks the exact host name before any DNS lookup and fails closed.
- **Per-call signatures** (stage 1c). Every call to a Core tool is signed with an HMAC by the adapter, and the model never sees the key.

## Testing

- **Fake Telegram.** `tests/faketg.py` is a fake Telegram Bot API server with failure injection:
  - HTTP errors;
  - dropped connections;
  - truncated and stalled downloads;
  - redelivered updates.
- **Acceptance scenarios for stage 1a** (`tests/test_acceptance_stage1a.py`):
  - S2 — thirteen PDFs in two sends;
  - S3 — a crash while saving, before and after the journal;
  - S5 — duplicate delivery;
  - S7 — the sender comes from Telegram, not from the text;
  - S12 — messages sent while Core was down;
  - S14 — strangers, groups and other update types;
  - S28 — hostile file names;
  - S36 — a shuffled and repeated album;
  - also: files that are too large, an unavailable or missing documents folder, a truncated download, a full disk and a restart in the middle of an album.
- **Acceptance scenarios for stage 1b** (`tests/test_acceptance_stage1b.py`): an album whose private caption arrives after the first receipt; the snapshot and the daily line once a day across a restart; the documents folder missing at start and gone in the middle of a run; a revoked family bot token; a restart after a crash; strangers and family members writing to the service bot; a broken service bot token; a failing service loop; the service token never on disk.
- **No real system changes.** Tests never run `launchctl` or `tmutil` (`tests/conftest.py`).
- **Test data.** Tests use synthetic members only.
- **Test locale.** Tests run on the English locale. `tests/test_languages.py` checks the other languages.

## Design decisions

| ID | Decision | Why |
|---|---|---|
| D16 | One SQLite database, `core.db`. Spaces are separated by Core's code. | Atomic transactions across everything Core knows. |
| D23 | Stage order: intake → processing → knowledge → reminders and self-checks → workflows → migration → maintenance agent → setup dialog → metrics → Hermes → code generation. | Useful early, with learning before migration. |
| D24 | Snapshots and cards are readable but signed. Snapshot manifests carry a monotonic generation number, and privacy events live in a separate signed journal. | Tampering and rollback become visible; reading needs no key. |
| D26 | Core supervises the host gateway: RUNNING, HOLD, STOPPED. The host has its own launchd service, runs under external supervision, never respawns itself and has config reload turned off. | Otherwise launchd, repair tools or hot reload would change the host behind Core's back. |
| D27 | A file on disk is named by its record id. The original name lives only in the database and the card. | A name chosen by the sender must never become a path. |
| D31 | Stages 1–3 use test data only. Real documents arrive from stage 4. | No data migrations while the design is still moving. |
| D32 | The space is decided at intake, by the caption. A later "only for me" is `make_private`. | Waiting for classification adds complexity without benefit. |
| D33 | Gatekeeper: only Core talks to the family bot, and the host gets a fake token. | Intake does not depend on host hooks; durable intake is Core's job; the host needs fewer unverified capabilities. |

## Roadmap

A stage is done when its acceptance scenarios pass on the test stand.

| Stage | Scope |
|---|---|
| 1a | Core intake: journal, attachments, receipts, spaces, signed cards, locales. **Done.** |
| 1b | Service bot for alerts and buttons; signed daily snapshots with a generation number; the daily "all good" line; Core as a launchd service with its own interpreter; at start it probes the documents folder and names the macOS permission it lacks. |
| 1c | A dedicated OpenClaw install with a reference config; egress proxy; host-facing API with a fake token and accept-and-hold; adapter plugin with heartbeat; supervision with a live probe at start. |
| 2 | Processing in a sandbox: transcription and OCR, text versions of attachments, search and send tools, photo drafts to PDF, a page viewer. |
| 3 | Knowledge: facts with verified quotes, instructions, session taint, disclosure journal, `make_private`. |
| 4 | Reminders delivered by Core, fenced delivery, restore with a generation check, integrity and silence checks, watchdog. |
| 5 | Workflows the family teaches by example, with versions, owner approval and rollback. |
| 6 | Migration of existing data. |
| 7 | Maintenance agent. |
| 8 | Setup and settings dialog for other families. |
| 9 | Metrics (Prometheus, Grafana). |
| 10 | Hermes adapter. |
| 11 | Code generation, as a separate private release. |
