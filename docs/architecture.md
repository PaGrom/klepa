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

Stages 1a and 1b implement the gatekeeper's intake, storage, receipts and spaces, the service bot, snapshots and the launchd service. Stage 1c puts OpenClaw behind the gatekeeper: the gatekeeper's API for the host, supervision, the egress proxy, and Core's own OpenClaw gateway with its pinned runtime, its reference configuration and the adapter plugin (see [Host interface](#host-interface)). Stage 2a lets the host's turns reach the model with Core's instruction, and gives the model Core's first tools, each call signed (see [Core's tools](#cores-tools)). The parts marked with a stage come later (see [Roadmap](#roadmap)).

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
5. **Text.** Without a host, text and commands get fixed replies. With a host set up, members' text goes to the host (see [Host interface](#host-interface)). Commands never do: Core answers them itself.

**Identity comes from Telegram.** A file's sender (`authenticated_subject`) is the Telegram sender of the update Core received itself, never a name claimed in a caption.

## Storage

Code: `klepa_core/evidence.py`, `klepa_core/durable.py`, `klepa_core/names.py`, `klepa_core/cards.py`, `klepa_core/db.py`.

The data directory must be mode 0700, on a local disk, and never inside iCloud or CloudStorage:

```
<data_dir>/
├─ keys/                      0700: the signing key, the bot tokens, the host's fake token and the adapter key, each 0600
├─ incoming/YYYY/MM/          originals, <evidence id>-<name>
├─ inbound-journal/inbound.db
├─ core.db                    members, spaces, evidence, outbound, event log; jobs, texts, the search index
├─ jobs/<job>/                0700: a reader's folder while it runs, with a copy of the original (with processing)
├─ jobs.stale-<time>/         a jobs folder Core could not remove at a start, moved aside (rare; remove it by hand)
├─ core.lock                  one Core per data directory
├─ snapshots/                 signed snapshots of core.db, <day>-g<generation>/
├─ logs/                      Core's stdout and stderr under launchd, 0600, tokens redacted
├─ run/adapter.sock           the adapter plugin's Unix socket, 0600 (with a host)
└─ .metadata_never_index      keeps Spotlight out
```

**How an original is written.**
1. It goes to `incoming/YYYY/MM/` through a temporary file flushed with `F_FULLFSYNC`, followed by a rename that never replaces. A complete file left by an interrupted attempt is adopted. Other bytes under the same name are refused.
2. Its row is inserted into `core.db` (WAL, `synchronous=FULL`, `fullfsync=ON`).
3. After the batch is closed (its receipt queued, or with a host its message for the host), the file is copied to the documents folder. The copy is written the same way, read back and compared by SHA-256. A signed card, `<id>.card.json`, is written next to it.

**Guarantees:**
- a row never points to a missing file;
- the code never modifies or deletes an original;
- month folders follow the installation's timezone.

**A record Core made from others** (stage 2c-1): the PDF of a document from photos. It is written and copied like an original, under the person whose photos it is, with channel `core`, the tag `derived` and its sources, in order, in `derived_from` and on its card. Its text is its pages' texts, page by page, so search finds it by what its photos say; it is in the most private of its pages' spaces. Its key names the draft and the revision it was built from, so a build a crash interrupted finds it. It needs no update to close: it is copied to the documents folder at once. When a late private caption moves an album, the records made from its items move with it until they are copied, and the owner's alert counts the copied ones too.

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

**It may be unavailable.** Its volume may not be mounted yet, macOS may deny access, or the disk may be full. Intake does not depend on that folder: files still land in `incoming/`, receipts still go out and files still reach the host, while copies stay pending and are retried. Core never recreates a missing documents root, because the root may live on a volume that is not mounted yet.

**It may hang.** A stalled sync client, a network volume that went away or an unanswered macOS prompt can block a file call for minutes. Every call into the folder therefore runs on a worker thread, one at a time, with a timeout (20 seconds, 10 minutes for copying or pruning snapshots); while one call hangs, the others fail at once. Core's event loop never waits on the folder. A problem is reported to the owner once it has lasted five minutes, because at login the volume may mount after Core starts.

**Conflicts.** If the documents folder already holds different bytes under Core's name, the copy is marked as a conflict. It counts in the daily line and is tried again after each restart, once the conflict is cleared.

**Snapshots (D24).**
- Every night (03:30 by default) `VACUUM INTO` writes a consistent copy of `core.db` on the local disk.
- SQLite's integrity check and the SHA-256 go into a manifest with a monotonic generation number, signed with the snapshot key. Generation numbers are never reused.
- A snapshot is built under a `.partial-` name and renamed when complete, locally and then in `documents/_klepa/snapshots/<day>-g<generation>/`. A crash leaves nothing under a final name, and the start-up sweep removes the rest.
- Retention counts good snapshots only: the latest of each of the last 14 days and the first of each of the last 12 months. A snapshot that failed its integrity check stays two weeks for diagnosis and is never copied.
- `privacy_journal_head` is reserved for stage 3.
- The host's messages that wait to be sent are blanked in the copy: a snapshot never holds a family conversation.
- The signing key lives in `keys/` and on paper: `keys paper-backup` prints it for the owner.

## Processing

Code: `klepa_core/processing/`, `klepa_core/search.py`. Stage 2b.

With a `[processing]` section ([configuration](configuration.md#processing)), Core reads what people send. Without it nothing is read, as in stage 2a.

**What is read** (spec 6.1 item 4):

| Original | Reader |
|---|---|
| Voice message, audio | ffmpeg makes a 16 kHz mono WAV of the first 30 minutes. A recording whose loudest sound stays under −45 dBFS is silent and gets no text: whisper makes words up for silence ("Thank you."). whisper.cpp transcribes the rest, detects the language and leaves out sounds that are not speech (`--suppress-nst`). |
| Video, video note, animation | ffprobe looks for sound; ffmpeg takes it, and it is read as a voice message's. A video without sound has no text, and its job says so. |
| PDF | pdftotext reads the text layer page by page. A page with fewer than 100 letters may be a scan: a page number, a stamp or a scanner app's mark is text too. ocrmypdf reads such pages, forced, the first 50 of them, in the installation's OCR languages, and a page keeps whichever text has more letters, its layer's or OCR's. |
| Photo, image | ffprobe reads the size first, 100 megapixels at most; an iPhone's HEIC photo is a grid of tiles, and its size is the grid's. ffmpeg turns HEIC into PNG, joining the tiles; tesseract reads the text. |
| Word, RTF, OpenDocument text, HTML | macOS's `textutil`. |
| Spreadsheet: xlsx, xlsm, ods | Core's own reader (`klepa_core/processing/sheets.py`), run as a program in the sandbox like the others: a sheet is a page, a row a line with its cells joined by tabs, empty rows and cells left out. Every member's unpacked size is checked before anything is read. The old binary `.xls` is not read. |
| Text file | `.txt`, `.md`, `.csv`: Core decodes it itself. A byte order mark first, then UTF-8, then Windows' Cyrillic code page (cp1251, which Excel still saves CSV in) when its letters come out Cyrillic, else UTF-8 with the bad bytes replaced. |

Archives and other files are not read. macOS's `sips` cannot work in the sandbox: it reads the home folder to recognise a HEIC and writes in the user's temporary folder first. ffmpeg converts HEIC instead.

**Limits, said.** A recording is transcribed up to 30 minutes, OCR reads the first 50 scanned pages of a document, and at most 2 MB of text is kept per original. When a limit cuts what was read, the job keeps a note ("OCR read the first 50 of 70 scanned pages"), and `get` returns it as `partial`.

**Jobs.** An original a reader can read gets one job, queued as soon as the original is stored. At start Core also queues the stored originals that have none: those that came before processing was set up, and one whose job a crash kept from the queue. Two workers read them, each in the order they came: one reads recordings (voice messages, audio files and videos), the other everything else, so a long scan never holds a voice message (the owner's choice of 2026-10-18). The record follows its job: `stored` → `processing` → `processed` or `failed`.
- A failed reading keeps the original, which can still be sent. The error names the program and its exit code, never what the program printed: that may quote the file.
- A job a crash interrupted runs again at the next start, three times at most, so an original that crashes its reader every time cannot stop the queue. When Core stops in the middle of a job, the job goes back to the queue without using an attempt.
- A kind that lacks what it needs (a program, the speech model, an OCR language) waits in the queue. The owner gets one alert naming what is missing, and the next start that has it reads them. A PDF waits for ocrmypdf even when it has a text layer: any of its pages may be a scan.
- A program that disappears while Core runs (Homebrew upgrades it under the same path) puts its job back without using an attempt. Its kind waits for the next start, and the owner is told.
- A reader the watchdog or its time limit stopped fails its job with the program's name and the limit.
- The event log says which job, its kind, pages, time and program, or the error's name; never a text.

**Texts.** A text is kept page by page in `evidence_text`, with what read it (`extractor`): the program and its version, as Homebrew or the engine's environment installed it, and whisper's model or tesseract's languages. A transcript, a Word document or a text file is kept in fragments of at most 4,000 characters, cut between paragraphs or sentences.

**The sandbox** (spec 8). Every reader runs under `sandbox-exec`:
- no network;
- it writes only in its job's folder, `<data_dir>/jobs/<job>/`;
- it reads nothing in the hidden folders but its job's folder and what the readers need. Hidden: the home folder, every home (`/Users`), the other disks (`/Volumes`), the shared temporary folders (`/private/tmp`, `/private/var/tmp`), the logs, the user's own temporary folders in `/private/var/folders/`, and Core's data and documents folders wherever they are. What the readers need: the programs and the speech model (the files, not their folders), the OCR languages, and Core's interpreter and environment. Of the folders above these it sees each folder's own entry, never a listing: a framework build of Python, Homebrew's, resolves its environment's folder when it starts;
- Core's data and documents folders are hidden again after what readers may read, so no readable folder reopens them, and the job's folder is allowed last. Core refuses to read anything, and tells the owner, when a readable folder is or holds a hidden one, or when the speech model or the OCR languages are kept inside Core's own folders;
- it may start only the readers: Homebrew's programs, the configured ones, macOS's `textutil` and Core's interpreter. It may signal no process but its own;
- it reaches none of macOS's services that open files, apps or addresses (LaunchServices), nor the pasteboard, Apple events, the keychain, the window server or the Dock, and launchd starts no job for it;
- only whisper's step, on the GPU, may also use Metal's shader cache, which every app of the user shares: whisper starts in under a second with it and compiles its shaders for twenty seconds without, and it reads only the WAV that ffmpeg made. The parsers of what people send (ffmpeg, pdftotext, Ghostscript under ocrmypdf, tesseract) never get it;
- its job's folder is its home, its working directory and its temporary directory, and its environment is those three, `PATH` and the locale, with what its step adds (tesseract's languages): never Core's own;
- it gets a copy of the original, never the incoming folder: a reader broken by a planted file must not reach the other originals. The copy keeps the original's suffix only when it is letters and digits;
- what it prints goes to files in its folder, and Core reads their ends: a reader that floods its output cannot fill Core's memory;
- `rlimit`: no core dumps, which would hold the file's content, and no file over 1 GiB;
- each step has a time limit, and a watchdog stops a reader whose folder passes 2 GiB or 10,000 files; a reader stopped either way is killed with every process it started. ocrmypdf runs two workers, not one per core, so that several large pages are not held in memory at once;
- the job's folder goes when the job ends, off the event loop. A reader may have taken the owner's rights on what it wrote or marked it unchangeable: Core gives them back first. At start Core removes whatever a crash left there, and moves aside a jobs folder it cannot remove (`jobs.stale-<time>`) rather than stop.

The profile names real paths: `/var` is `/private/var` on macOS, and a rule for `/var` matches nothing. macOS enforces no memory limit; the limits on pages, pixels, minutes and seconds bound memory instead. Without `sandbox-exec` (anywhere but macOS) nothing is read, and the owner is told.

What the sandbox does not close:
- a reader can see other processes' command lines and environments (`sysctl`). macOS has no rule that closes it without breaking the readers. Klepa's own processes keep no secret in either;
- reads are hidden by folder, so what is not hidden stays readable: the system's own folders (`/System`, `/usr`, `/private/etc`), the applications, `/Library`, and all of Homebrew's folder, with what services installed by Homebrew keep in `/opt/homebrew/var` and `/opt/homebrew/etc`. Keep no secret there on a Mac that reads attachments.

Smaller points:
- A program's version is read when Core starts: after an upgrade, texts name the old version until Core restarts.
- A record deleted while its job runs keeps the text the job wrote, in the texts and the index; search and `get` never return it, since they read only kept records.
- Whisper's step may list the names in the user's cache folder, since Metal looks at that folder itself; it reads nothing in it but Metal's own two folders.
- A voice message without a caption is the family's (D32), so its transcript is found by every member's search, as its name is.

**The search index.** One FTS5 table (`unicode61`, marks on Latin letters removed) holds each record's name and caption, written with the record (page 0), and its text, written with the text (pages 1 and on). A record and its index rows land in one transaction. The index also folds two letters that the tokenizer keeps, in what it holds and in what it is asked. The Cyrillic yo becomes ye: OCR and whisper mostly write ye where people write yo. The Serbian d with stroke becomes dj: it has no mark to remove, and people write dj without marks. A snippet shows the folded text; `get` returns the text as it was read.

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
- an album became private after part of it was copied;
- the host failed its start gate or a check while running;
- the host's adapter has been silent for three minutes;
- two clients poll the gatekeeper at once;
- the host has not polled for five minutes;
- Telegram refused an answer of the host, or the gatekeeper did not take one: the person may be left without it;
- the egress proxy does not start.

**The daily line.** Once a day (09:00 by default) the bot sends one line: the host's state; the last intake; the newest snapshot's
generation, hash and integrity; the state of the documents folder; and the counts of waiting copies, copy conflicts
and sends left unconfirmed in the last 48 hours. It says "Needs attention" when any of these is wrong. A missing line
means trouble. The Status button under the line sends a fresh one. Its id is one-time, 128-bit, and bound to the chat,
the message and a lifetime of a week. With a host set up, Pause sits next to Status, or Resume while the host is
paused or Core stopped it. The reply to any other word in the service chat carries the same buttons.

**The documents probe.** Core writes, reads back, lists and removes a small file with a unique name. Listing matters:
macOS lets a background process without permission write a known path in a protected folder but refuses to list it.

**Daily jobs** run once a day after their time, and once on wake after sleep or downtime. A job is marked as run
before it starts, so a job that brings Core down is not repeated in a loop.

**Fault isolation.** Only family intake can stop Core; launchd then starts it again. The service bot, the scheduler,
the snapshot copier and the start-up probe restart themselves after a failure, and a broken service bot token turns
only the service bot off.

## Host interface

Code: `klepa_core/host/`.

Stage 1c puts the agent host behind the gatekeeper. It runs when the config has a `[host]` section ([configuration](configuration.md)). With `gateway = "launchd"`, the default, Core runs its own OpenClaw gateway as well: `klepa-core host install` puts its runtime in place, and Core starts, checks and watches it.

**The gatekeeper's API** (`host/api.py`). The host talks to a Telegram-shaped API on `127.0.0.1` with a fake token:
- the fake token holds 256 random bits and is made once per installation in `keys/host-bot.token` (0600). It is a secret of its own and never the real one;
- the methods are an exact allow-list: `getUpdates`, `getMe`, `sendChatAction` and `sendMessage`, plus `deleteWebhook`, `deleteMyCommands` and `setMyCommands`, which are answered here and never passed on;
- `sendMessage` keeps `chat_id`, `text`, `parse_mode` (HTML only) and a reply to an issued message or to the host's own message in the same chat. Everything else is dropped, and link previews are always off;
- editing, deleting, pinning, reactions, copies, forwards, files and every other method are refused;
- the `Host` header must be exactly the gatekeeper's address, so no web page can reach the port through DNS rebinding, and what only browsers send is refused: `Origin`, `Sec-Fetch-Site`, `Sec-Fetch-Dest`, `Sec-Fetch-User`. `Sec-Fetch-Mode` alone passes, because Node's fetch, which the host uses, sends it with every request;
- one long poll at a time: a second one gets 409, and the owner gets an alert;
- at most 32 connections at a time, so a local process that holds idle connections can starve the host but never Core;
- the host may write only to a member's private chat with an open conversation: a message it was given and has not answered, or an answer less than ten minutes old. A turn's answer answers its chat's older messages too, but one a live turn of its own answers: the gateway runs a chat's messages in order, so an older one without a turn was joined to this one, or never gets one;
- every call is an event in the log.

**What the host gets** (`host/queue.py`, `host/served.py`, `host/peers.py`):
- members' messages, each chat strictly in order, and only while the host is RUNNING with a heartbeat at most 15 seconds old;
- Core's own update numbering, which never goes back, even after a restore;
- the fields `message_id`, `from`, `chat`, `date` and `text` only. A forwarded message is marked;
- people by **stand-ins**, never by their Telegram ids: each member's id for the host is derived from the adapter's key and the member's person id, the same on every start, and Core turns it back at the gatekeeper. The gateway's allow-list (`channels.telegram.allowFrom`) holds the stand-ins too, and `host allowed` prints them for a gateway somebody else runs. A new adapter key changes every stand-in. The gateway puts the ids it is given into every prompt it sends the model provider (see [Security model](#security-model)).

A text is built from the inbound journal when the host polls, so no message text lands in `core.db`; to know which message a turn answers, Core keeps the SHA-256 and the length of each served text in memory only. **An attachment** reaches the host as text once it is read (spec 6.1 item 4): a batch of attachments is one message, from its first file's, with the person's caption and then, for each file, a mark naming it and its record id, and what Core read from it, ending with a mark of its own. A transcript comes whole up to 8,000 characters, a document's beginning up to 3,000, and the whole message keeps to 12,000; the mark says how the model reads the rest with `get`. A file that could not be read, is not read, or waits for what the owner installs says so in its mark. The brackets that make a mark (`⟦…⟧`) and their look-alikes (`〚…〛`) are taken out of everything a file or a person wrote: in an attachment's message, in a text message, forwarded or not, and in what `search`, `get` and `send_original` return. A file's name in a mark is one line of at most 80 characters, whatever breaks a line in it, with its quotes replaced. So no text can close a mark, open one or fake a field of one. The delimiters of OpenClaw's runtime-context block (below) are taken out of the same texts. When the batch's images made or joined a draft of a document, a last mark names the draft and its pages (Delivery, below). **The chat's order:** an attachment waits for its reading, and the chat's later messages wait behind it, so a voice note asking for something and then "cancel" cannot reach the host the other way round; a file whose kind waits for a program does not hold its chat. While an attachment waits for a second try at its download, the chat's later updates wait for it in the journal, in order, and its batch stays open, so an album stays one message. A text whose last pages are blank comes whole. Commands and other updates never reach the host.

**The host's messages** (`host/outbox.py`, `host/sanitize.py`):
- **Accept and hold.** Every `sendMessage` is written to `outbound` durably before the host hears "sent", under a message id from Core's own range (2^41 and up).
- **Release.** A worker sends the messages one chat at a time, in order, once the gateway has passed the start gate and while the adapter's heartbeat is at most 15 seconds old.
- **Drop.** A message that has not gone out within ten minutes is dropped with the rest of its chat's queue, and the person gets Core's apology. So does a message Telegram refuses, and the owner gets an alert.
- **No links.** The host's HTML is rewritten from an allow-list. Formatting survives, links do not: an anchor becomes its text and its address in a code span, and bare web addresses go into code spans too, so no web address from the host is clickable and a hidden one is always shown. What Telegram would link by itself goes into code spans as well: e-mail addresses, @mentions and names with a top-level part, in any alphabet and also before the full stop that ends a sentence; a file name such as `scan.pdf` too. An address that came in the person's own message stays inactive as well. **No record ids.** A record's id is for Core's tools; the model copies them from Core's marks, so the host's text loses every one (found in the live run of 2b-2).
- **No text kept.** A finished send keeps no text, and a snapshot never holds one that waits.

**The adapter link** (`host/adapter.py`, `host/turns.py`). The adapter plugin talks to Core over a Unix socket, 0600 in a 0700 directory:
- every message is HMAC-SHA256 signed over its exact bytes with `keys/adapter.key` and carries `(boot_id, seq)`. Core's answers are signed too. A message without the right signature changes nothing;
- `heartbeat`, every 5 seconds, carries the policy the host runs with, the plugin's hash, its registrations, the model and the runtime;
- `dispatch` comes from `before_dispatch`; every issued message goes on to a turn;
- `prompt_built` comes from `before_prompt_build`. Core answers with its instruction, the tools the turn may use (none for the probes) and the text a person reads when the model fails;
- `turn_start` comes from `before_agent_run`. A turn is registered only when it answers an issued message from that sender in that chat, and only after `before_prompt_build` ran for that run, so the prompt carries Core's instruction. The turn takes the first waiting message whose text its prompt is; else the last of a long text's fragments its prompt joins (the gateway joins a text of 4000 characters or more with the message of the next id, as Telegram's pieces of one long text, so a long attachment and the person's next message are one turn: found in the live run of 2c-1); else, and for a message served before Core restarted, the oldest. A registered turn reaches the model; in HOLD and STOPPED it is blocked with the hold text. A message is taken only by a live turn of the same boot: after a crash, the new process's run of the turn takes it (scenario 32);
- `turn_reply` reports how a turn that reached the model replied: with the model's answer, or with OpenClaw's report of the model's error;
- `GET /v1/tools` and `POST /v1/tool` serve the MCP server of the adapter's package, which has no key: a call's authority is the signature inside it (see [Core's tools](#cores-tools)).

**The adapter plugin** (`host/openclaw/adapter/index.ts`: TypeScript that OpenClaw and Node run without a build step):
- it registers `before_dispatch`, `before_prompt_build`, `before_agent_run`, `before_tool_call` and `reply_payload_sending`, and a background service that sends the heartbeat;
- without Core it claims nothing in `before_dispatch`, and `before_agent_run`, a gate that OpenClaw fails closed, blocks the turn; `before_agent_run` reports the SHA-256 of the run's prompt and its length in UTF-16 units to Core, never its text;
- `before_prompt_build` gives the gateway Core's instruction as the whole system prompt (`systemPrompt`), so none of OpenClaw's own prompt reaches the model: not its runtime line with the Mac's computer name, not the workspace and documentation paths with the account name (the canary test of stage 2b-2). The one line of it the model needs went with it: the gateway adds a runtime-context block to a person's message (`<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>` … `<<<END_OPENCLAW_INTERNAL_CONTEXT>>>`), notes on the session so far, and Core's instruction now says what it is and never to mention it (found in the live run); it also narrows the turn's tools to Core's;
- `before_tool_call` signs every call of Core's tools, overwriting a `_klepa` field the model wrote, and blocks every other tool and every call without a run id (Core then checks that the run's turn is registered and live);
- `reply_payload_sending` sends a reply as text: media are dropped, so a `MEDIA:` line cannot make OpenClaw lose the whole reply; OpenClaw's English report of a model error, which it sends whatever `errorPolicy` says, becomes Core's text in the installation's language; and a blocked turn's text loses OpenClaw's "Your message could not be sent" wrapper. For a turn that reached the model it reports to Core how the turn replied;
- `mcp.ts` is the package's MCP server, which OpenClaw runs with the engine's own Node: it lists Core's tools and passes each call to Core over the socket, holding no key;
- it reaches Core over a raw Unix socket. OpenClaw routes every `node:http` and `fetch` request of its process through the egress proxy, even one that names a socket path; raw sockets are outside that routing;
- one boot is one gateway process. OpenClaw registers the plugin more than once in a process: a "full" registration runs `before_dispatch` and the service, a "discovery" one runs the turn hooks. So the boot id and the sequence live on the process;
- the heartbeat reports the plugin's own SHA-256, the values of the reference keys as the running gateway holds them, the model, the runtime and OpenClaw's version; a release other than the one the adapter was proven with fails the check. The plugin never logs what people wrote.

**Core's own gateway** (`host/runtime.py`, `host/reference.py`, `host/gateway.py`, `host/install.py`, D30):
- **Runtime.** A pinned Node from nodejs.org, checked against the SHA-256 in the code before it is unpacked, and OpenClaw 2026.9.4 from npm with the lockfile that ships with Core; npm runs no install scripts. Both live in Klepa's program folder (`host.runtime_dir`), each version and each lockfile in a folder of its own, so a new one never rebuilds what a running gateway uses; Node is for Macs with Apple silicon. The `klepa-openclaw` wrapper there runs OpenClaw as the engine does, from an empty environment and with the gateway's own HOME.
- **Reference configuration.** Core writes the gateway's `openclaw.json` from a table of exact values (spec 7.1): loopback only, no live reload, no terminal, no Control UI, no silent device pairing, no mDNS; only Telegram, the model's provider and the adapter, and no memory plugin; Core's three tools only, by exact name, through the adapter's MCP server and never through `/tools/invoke`; none of OpenClaw's own persona files or first-run ritual in the prompt (Core also deletes a `BOOTSTRAP.md` an earlier OpenClaw left in the workspace), no internal hooks, no ACP; no commands, no scheduled jobs, no statistics, no updates; every HTTP request through the egress proxy; one model on the built-in runtime. OpenClaw cannot write the file (`OPENCLAW_CONFIG_READONLY`), and a heartbeat that reports another value stops the gateway.
- **The gateway's folder** is `host/` in the data folder: config, state, workspace, logs and the gateway's own HOME, so OpenClaw finds nothing of the owner's by its default paths. It holds the model's token and the host's sessions, so Core keeps it out of Time Machine every time it starts the gateway, and no snapshot includes it. The console goes to launchd's files at the error level only; the gateway's own log rotates at 20 MB.
- **launchd.** The agent `klepa.gateway` runs under external supervision and never respawns itself; launchd brings it back after a crash but not after a clean exit. Its plist lives in the gateway's folder, not in `~/Library/LaunchAgents`, so only Core starts it. Stopping is always `launchctl bootout`.
- **Start.** Core writes the gateway's files and keeps a digest of what it loaded: config, adapter and plist. A gateway whose files still match the digest keeps running across a restart of Core; files that changed since, whoever wrote them, are validated by OpenClaw and loaded. The gateway is ready when `/startupz` says started, `/readyz` says ready, its log since this start shows no refusal of the adapter's hooks, and a model token is stored.
- **Watch.** A gateway that exits by itself, or is unloaded behind Core's back, is started again with an alert; the third time in ten minutes Core stops it instead. A new gateway process, after a crash too, passes the start gate before it gets messages. An answer from launchd that Core cannot read changes nothing. After a failed check Core stops the gateway, and it stays down until Resume or until Core itself starts again.
- **Model access.** `klepa-core host login` reads the model's setup-token (from `claude setup-token`) without echo, the whole paste even when the display broke it into lines or framed it, or from a pipe, and gives it to OpenClaw on stdin; the gateway keeps it in its own state. OpenClaw stores the token and then fails to write its config, which stays Core's; a running gateway is asked to take the new token at once. Whether the token works only a model call shows: the start gate makes one.

**Supervision** (`host/supervisor.py`, D26):
- **STARTING.** The gateway runs but gets no messages and sends nothing until it passes the start gate: a full heartbeat within 30 seconds, the gateway ready, and two probes. The probes are synthetic messages from a peer that no member has. For the first, the adapter must report `before_prompt_build` and `before_agent_run`, and the host must then write the block to the probe's chat: the turn ended there, without the model. Hooks without `allowConversationAccess` never report, so such a host never gets messages. The second probe goes to the model, through the egress proxy. A token the model refuses stops the gateway at once, and the owner is told to log in again and press Resume, before a person's question meets it. OpenClaw itself retries a busy provider for about 80 seconds within a turn, so the probe waits up to 150 seconds, and a model that erred otherwise or stayed silent is asked once more after 30 seconds. If it still errs, the host runs anyway and the owner gets one alert: people get Core's apology for each message until the provider answers again, without waiting for anyone. If it stays silent, the gateway stops, because a person would meet silence.
- **RUNNING.** The host gets messages. A new `boot_id`, or a new gateway process that Core sees in launchd before its first heartbeat, means the gateway restarted; it passes the gate again, without alerts.
- **HOLD.** Three minutes without a heartbeat, or the owner's Pause. The host gets no new messages, its sends are held, turns are blocked, and people get a fixed reply at most once per ten minutes per chat. Core keeps receiving files.
- **STOPPED.** A failed check: the start gate, or a heartbeat that stops matching. For people it looks like HOLD.

The service bot's line names the state. Resume passes the start gate again.

## Core's tools

Code: `klepa_core/host/tools.py`, `klepa_core/host/instruction.py`, `klepa_core/search.py`.

**The instruction.** Core's instruction is in English and at most 4,000 characters; the adapter adds it to every turn's system prompt. It says that only the system text instructs the model, that messages, file names, transcripts and tool results are data, and that an answer about stored records comes from Core's tools, in the language of the person's last message. It also says what Klepa cannot do yet (reminders, memory between conversations, changing or sharing records), so the model promises none of it, and that a file a person sends reaches the model as a record to look up, not as a message. With the tools' descriptions it stays within 5,000 tokens; a test checks it.

**The tools**, as the model sees them:
- `klepa__search` finds stored records by the beginnings of words, so other forms of a word match too: in their names, captions and, from stage 2b, texts. The best matches come first (FTS5's bm25, a name or caption weighing twice a page), each record once. A record whose text matches names its best page and quotes the words around, even when its name or caption ranks first. Without words it lists the latest records (1 to 50 results). The space filter is in the query itself, before the ranking (spec 5.6).
- `klepa__get` reads one record and its text page by page, up to 10,000 characters of a page (a longer one is cut and says so). `text_status` says whether the text is there:
  - `read`, with `partial` when a limit cut what was read;
  - `reading`;
  - `waiting`: its kind is not read until the owner installs what reading it needs;
  - `failed`: the original is kept and can be sent;
  - `none`: a file of this kind is not read.
- `klepa__send_original` has Core send the person the original file with the name it came with.
- `klepa__make_pdf` makes and changes a person's draft of a document from photos (below, under Delivery): `add` lists the records of more images, numbered after the draft's pages, and `order` the pages to keep by number in their new order; without a draft, `add` starts a new one. For a batch's draft the model sets the order its pages' content shows, each page once, leaving out a photo of the same page as another, and says in its answer which it left out and which page seems missing; or it closes a draft that is no document with `not_a_document` (before its first PDF, with its revision, once every page was read; an order opens it again). Later changes come from the person's words, or a missing page they send. Each change's PDF follows the model's answer. `search` and `get` name the version of a document Core made.

A tool acts for the person whose message the turn answers, over the shared space and that person's own: another member's personal records are never found, read or sent.

**A signature per call** (spec 4.5). The MCP server holds no key, and the model never sees one:
- the adapter's `before_tool_call` adds `_klepa`: the turn's run, the call's id and an HMAC over a domain string, the run, the call, the tool's name and the canonical JSON of exactly the parameters Core will run;
- Core checks the signature, that the turn is registered and live, and that the call id was never used; the parameters must be exactly the declared ones, a key twice or a fraction refuses the call;
- a signature from the model's history fits only its own call, a direct call through `/tools/invoke` has no turn and an id `http-…`, and the gateway refuses Core's tools there anyway.

**Sending an original** (spec 6.5). Core reads the file once, checks its size and SHA-256 against the record, and sends exactly those bytes from the family bot, under the name it came with, without control characters; a file changed on disk is never sent. The model hears "sent" once the file is queued; if the send then fails, the person gets Core's words saying so.

**The egress proxy** (`host/egress.py`). All the host's HTTP goes through Core's proxy on `127.0.0.1`, CONNECT tunnels and plain requests alike:
- the exact host name and port are checked against `host.egress_allow` before any DNS lookup, so a refused name never reaches DNS;
- an allowed name must resolve to public addresses only;
- the one loopback destination is the gatekeeper;
- everything else is refused: the proxy fails closed;
- a tunnel idle for ten minutes is closed; lookups run on threads of their own, with a timeout;
- a refusal is logged with a hash of the name, because a name may itself carry data.

## Delivery

Code: `klepa_core/gatekeeper/outbox.py`, `klepa_core/gatekeeper/receipts.py`.

**The outbox.** Core's own messages, receipts and fixed replies, go through an outbox in `core.db` with idempotency keys. A message moves from PENDING to SENDING and ends as CONFIRMED, RETRY_WAIT, UNKNOWN or FAILED.

**Honest statuses.** A send that may have left without being confirmed becomes UNKNOWN. It is never resent automatically.

**Batches.** A batch is the attachments of one chat that arrive less than the batch window apart, such as an album. The batch stays open while an attachment of that chat is still downloading, and a text of that chat closes it. Without a host, the batch gets one receipt ("📄 got 5 files"); a message repeated under a new update id is covered by the receipt of its original.

**While a file is read** (`gatekeeper/attachments.py`, spec 6.1 items 4-6). With a host, no receipt comes first: the model answers on what the files say. A batch whose reading takes over two seconds, counted from when the batch closed, gets Core's interim answer (the person sees it some four seconds after sending one file, the batch window being two), "🎧 listening…" for recordings or "📄 reading…" for the rest, as a reply to its first file, with a Cancel button. When the reading ends, Core edits that message: the button goes, and the text says it listened or read only when every file was read, that it could not read and kept the file when one failed, was cancelled or has no reader, or that it will read the file later when a reader the owner must install is missing. If Core never learnt whether its interim answer arrived, a cancel still gets a reply that says so. **Cancel** goes past the chat's queue: the button, or a message of only a cancel word (`[cancel] words` of any of Klepa's locales; a forwarded one is somebody else's and cancels nothing; a word Core took is kept, so after a crash it never reaches the model), ends the batch's jobs at once, the running reader's process group is killed, a result that comes later is dropped, the batch never reaches the host, and the person hears that the file is kept. While several batches of a chat are read, the word gets a question with a button for each and one for all. Buttons are one-use, live a day, work only in their own chat on their own message (on any message of that chat when Core never learnt Telegram's id for the one that carries the button), and a press that is not good says the button no longer works.

**A document from photos** (`gatekeeper/drafts.py`, `processing/pages.py`; spec 6.1 item 4, scenario 48; the owner's choice of 2026-10-27: no sheet to check, no buttons, as simple as can be). Two or more images in one batch make a draft. Once the batch's reading ends, Core looks at it once, and the host gets it only after that, with a mark naming the draft and its pages by number and record: its images each once, by their bytes, in the order they came, and its revision. The model passes `make_pdf` the order the pages' content shows (how a text runs on, headings, signatures, page numbers), each page once, leaving out a photo of the same page as another, and says in its answer which photos it left out and which page seems missing (a gap in the page numbers, a text that breaks off); a draft changed since the numbers were given is refused with its pages as they are. When the images are no document (people, places, things), the model says so with `not_a_document`, and no PDF is made. A draft the model neither ordered nor closed makes nothing. Once the model has answered (no batch of the draft waits for its answer and no run of the model in the chat is alive, or the runs are over and ten minutes have passed since the host took the batch), Core builds the PDF of the ordered revision from the originals and sends it as a reply to the batch's first photo, or to the person's words that changed it: a JPEG's image byte for byte as it came, without the segments that say where, when and with what it was taken (any other image, or a JPEG that asks to be turned, encoded upright once, no larger than A4 at 300 dpi, a transparent part white), a page each fitted into A4, with no dates in the file; when the originals would pass 50 MB, every page is encoded anew, smaller, until they fit. It waits until every page's album has been quiet for the album window, so that a late private caption still decides the document's space. Each PDF is a new version, a record Core made (Storage above), and only the latest version stays in the search index. A photo the person sends alone within an hour of the chat's draft's last change joins nothing by itself: its mark names that draft and its pages, and the model adds it with `make_pdf`, where it goes, when the person's words or the page say it belongs (a missing page sent later); the new PDF follows. The person says in words what else to change, and the model passes it to `make_pdf`. PDFs are drawn by Core's own renderer in the sandbox, from copies in a job folder, HEIC turned into PNG by ffmpeg first (3508 pixels), as readers read files: Core never decodes a person's image. Each copy is checked against its record's SHA-256 and created anew before any program runs in the folder, so a page changed on disk is never drawn and a program broken by a crafted page cannot make Core write through a link. Deciding and drawing run in loops of their own: every chat's batch waits for its look, never for a drawing, an unexpected error fails its own draft only, and a stop cancels the drawing under way, which the next start draws again. Every page must still be in the shared space or the person's own when it is drawn. The renderer needs Pillow, which the processing extra brings with ocrmypdf; without it no draft is made, and the owner's alert names it. A draft holds at most 100 pages and a PDF 50 MB; a PDF that cannot be drawn, or would be too large, is told to the person, and the photos stay as they are. A batch the person cancelled makes no draft (scenario 37). The buttons of sheets Core sent before 2026-10-27 answer that they no longer work.

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
- **The host.** It holds a fake token, and reaches Telegram only through the gatekeeper and the internet only through the egress proxy, which checks the exact host name before any DNS lookup and fails closed. The adapter's messages are signed with a key the model never sees. Its configuration is Core's and read-only, and it runs its own Node and OpenClaw, pinned by a checksum and a lockfile.
- **Readers.** Programs that parse what people send run under `sandbox-exec` (see [Processing](#processing)):
  - no network, and writes only in their job's folder;
  - nothing of the homes, the shared and temporary folders, the logs, or the data and documents folders but what they need;
  - no program but the readers, no signal to another process, and no LaunchServices, pasteboard, Apple events or keychain;
  - a copy of the original rather than the incoming folder, no core dumps, time and size limits.

  A reader broken by a planted file could still put what it may read into the text it returns. That is not the family's data, the keys or anyone's home, but it is the system's and Homebrew's folders (with what Homebrew's services keep in `/opt/homebrew/var` and `/opt/homebrew/etc`) and other processes' command lines and environments (see [Processing](#processing)).
- **Per-call signatures.** Every call of a Core tool is signed by the adapter for exactly its parameters, its run and its call, and the model never sees the key (see [Core's tools](#cores-tools)).
- **The model's replies.** They leave as text only, with every address inactive; the gatekeeper takes no file from the host, and the adapter drops media before OpenClaw would fetch them.
- **What the model provider sees.** The adapter gives the gateway Core's instruction as the whole system prompt, so none of OpenClaw's own prompt goes out: not its runtime line with the Mac's computer name, not the workspace and documentation paths with the macOS account name. People reach the host by stand-ins, so no Telegram id goes out either. What does go, besides the conversation, is declared: the speaker's configured name, their stand-in, and the time with its zone's offset, never its place; with an attachment or a search, record ids and the names of the senders of the records the speaker may see, as `search` and `get` name who sent each. A live test checks the exact requests the real gateway sends the provider against canary values (the canary test, see [Testing](#testing)). It runs through a scripted provider of OpenClaw's OpenAI-compatible kind: what the Anthropic provider adds of its own (its system field, metadata, headers) is not in what it sees.

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
- **Acceptance scenarios for stage 1c, Core's side** (`tests/test_acceptance_stage1c.py`), with a fake host (`tests/fakehost.py`) and a fake adapter plugin (`tests/fakeadapter.py`):
  - the host gets text only after the live probes, and the model's answer comes back through it;
  - S16 — no adapter, or hooks without conversation access: the host never gets a message;
  - S14 — unsigned or foreign adapter messages change nothing;
  - S43 — commands never reach the host;
  - S44 — the narrow interface: a wrong token, a foreign `Host`, `Origin`, a second poll, refused methods, a stripped keyboard, a quote from another chat;
  - S38 — sends held while the adapter is silent leave in order, a restart keeps them, and ten minutes drop them with an apology;
  - S41 — a gateway restart passes the gate again quietly, and the waiting turn registers;
  - S12 — while Core is down the host gets nothing, then the backlog in order;
  - also: HOLD after silence and back, Pause and Resume, and the host's traffic through the egress proxy.
- **Stage 2b, processing.** The readers run as stand-ins (`tests/fakereaders.py`): small scripts that take the real programs' arguments, write what the real ones would, leave their arguments in the job's folder, and refuse any file outside it.
  - `tests/test_readers.py` runs each reader with and without the real sandbox: scans with a little text of their own, the notes on what a limit left out, encodings, video, HEIC, Word, spreadsheets, a program gone since the start, a crafted suffix.
  - `tests/test_sandbox.py` checks the sandbox itself (macOS): writes, reads of the hidden folders, the network, signals, the programs that may run, macOS's services, links and clones of hidden files, a killed process group, the rlimits, the watchdog, the reader's environment, the installations Core refuses.
  - `tests/test_jobs.py`: the queue's order, a crash in the middle of a job, Core stopping, a missing original, a kind that waits, a program that disappears, the folders a reader locked, the event log without texts.
  - `tests/test_acceptance_stage2b.py`: a voice message, a PDF and a photo through intake and the real sandbox; files kept before processing was set up; a missing program; no sandbox; a job a crash left running.
  - `tests/test_readers_real.py` runs the real programs in the real sandbox on English samples it makes, only where `KLEPA_TEST_PROCESSORS` names a speech model. The samples: speech by `say`; a PDF by `cupsfilter` and its scan by `pdftoppm`; a mixed PDF whose scans carry a page number and a scanner app's mark; a Word document by `textutil`; a spreadsheet; a tiled HEIC photo by `sips`; a video with sound and one without.
- **Stage 2b-2, the conversation's side.**
  - `tests/test_served.py`: the marks, the budget, what each file without text says, no text forging a mark, no name or MIME type breaking one, look-alike brackets, the page count of a text loaded in part.
  - `tests/test_host_queue.py`: an attachment waits for its reading and its chat behind it, a batch is one message, a batch redone after a crash is given once and one redone with a new file gives it, a kind that waits does not hold its chat, two voice notes in order, no text holding a mark, a long text not loaded whole.
  - `tests/test_attachments.py`: no interim answer for a reading under two seconds, listening and reading with a Cancel button, the edit when the reading ends, cancel by button and by word, presses that are not good, the question when several files are read, an interim answer that never left or whose fate is unknown, an edit true to every file, a word replayed after a crash, a word of any of Klepa's languages, a press after the batch reached the host, the tick finding its rows by index.
  - `tests/test_jobs.py`: a cancelled job that waits never runs, a running one's reader is killed, a result after the cancel is dropped, a recording read while a long scan is.
  - `tests/test_peers.py` and the host tests: the host knows members by stand-ins, and a Telegram id from the host stands for nobody.
  - `tests/test_acceptance_stage2b2.py`: S1 (a long voice note, listening, the answer on its transcript, the edit), S34 (the word right after a voice note; a text after a voice note), S6 (a planted instruction between Core's marks), cancel by the button, an album as one message, a text after a voice note whose download is retried, another person's voice note not held by a long scan, a forwarded cancel, stand-ins that clash turning the host off.
  - In `tests/test_gateway_live.py`: **the canary test**, which checks every request the real gateway sends the provider for the members' Telegram ids, the other member's name, the tokens, the adapter's key, the bot's id, the computer and account names, the account's full name, the time zone's name and place, and the folders, over a run with a voice note, a text and a search, reporting what leaked by name and the fixture by its hash; a voice note reaching the real model as its transcript; and S38's other half, the plugin going silent in the middle of a real turn.
- **Stage 2c-1, a document from photos.**
  - `tests/test_pages.py`: Core's renderer: a JPEG in the PDF byte for byte and the same PDF each time, a turned JPEG and a PNG encoded upright once, poppler reading the PDF with its pages fitted into A4, a page encoded anew no larger than A4 at 300 dpi, originals that would not fit encoded anew smaller, a JPEG without its EXIF and comments, a transparent page white, the exit codes of what is not an image, too large, or past its size.
  - `tests/test_drafts.py`: which batches make a draft (two images; one; a cancelled batch; a photo twice; files that are no images; a batch given to the host before drafts), the batch waiting until it was looked at, a photo sent alone later offered to the chat's draft and added by the model, nothing offered after an hour or to another person, an album sent soon after a draft of its own; the PDF of the model's order after its answer, from copies, as a reply to the first photo, a repeat left out, no PDF for a draft the model never ordered, the PDF after ten minutes when the answer never came, the hold while a run in the chat or a run on a later message is alive, HEIC first turned into PNG, a PDF that cannot be drawn; old sheets' buttons no longer working; make_pdf's added photos numbered after the pages, a new draft from photos and the changes it refuses, numbers of an older revision refused; version 2 and the older version leaving search, a personal page making the document personal, a PDF too large for Telegram, a build after a crash done once, a file put in place of the built PDF never sent (S46); one draft's unexpected error failing it alone, a page changed on disk never built, a link a sandboxed step leaves never written through, a PDF waiting until its album is quiet, deciding never waiting for drawing, a stop cancelling the drawing under way, a page no longer the person's to use never built; marks after the model ordered or closed the draft; photos the model finds no document getting no PDF, a page not read never closed, a closed draft opened again.
  - `tests/test_tools.py`: make_pdf for the person and the message the turn answers, lists of their own type only, what it cannot do; the version in search and get.
  - `tests/test_acceptance_stage2c1.py`: S48 and the forum's example #77177 (3) (an album, its PDF after the model's answer, the order fixed in words after a restart, version 2 of the originals in that order, search citing the latest version and its page, both in the documents folder), the model ordering an album by what its pages show, leaving out a second photo of one page and adding the missing page sent later as version 2, an album of a trip getting an answer and no PDF, two people with a page whose words ask to send the document to the other staying data, S37 (a cancelled album whose page hung makes no draft, the next one does).
  - In `tests/test_gateway_live.py`: make_pdf's list of pages through the real gateway, signed by the plugin, and the PDF in that order after the model's answer; `not_a_document` through the real gateway.
- **The adapter plugin.** `tests/adapter/` runs Node's own test runner on the plugin, and `tsc` checks its types. `tests/test_adapter_plugin.py` runs the real plugin under Node against Core's adapter server.
- **The real gateway** (`tests/test_gateway_live.py`). These tests run only where `KLEPA_TEST_RUNTIME` names an installed runtime, and each loads a launchd agent of its own:
  - the start gate and an answer of the model (a scripted one, reached through the egress proxy) through the real OpenClaw, with Core's instruction in the system prompt;
  - S41 — a killed gateway is checked again, and the message that waited gets one answer;
  - a gateway unloaded behind Core's back comes back, with an alert;
  - S16 — without conversation access the gateway never gets a message;
  - S40 — the model has Core's tools and none of the host's;
  - S43 — commands never reach the gateway;
  - S18 — a search through Core's signed tool; another member's personal records stay theirs, even when both ask at once; a direct call through `/tools/invoke` runs nothing;
  - S29 — a reply full of `MEDIA:`, image links, hidden links, e-mail addresses and @mentions reaches the person as inactive text, and the gateway asks the network for nothing;
  - S31 — a message sent while the model thinks gets its own turn; S32 — a turn cut by a crash is answered once after the restart;
  - the model's error: a refused token stops the host at the gate with an alert; a provider busy for 100 seconds at the start lets the host run without waking the owner; in a person's turn the person reads Core's words and the owner gets one alert;
  - OpenClaw's first-run ritual and persona files, left in the workspace, never reach the model's prompt;
  - `send_original` — Core sends the person their file, with its name and its exact bytes; when the stored file is gone, the person hears so;
  - `make_pdf` — the model orders a draft of photos, and the PDF in that order follows its answer.
- **No real system changes.** Tests never run `launchctl` or `tmutil` (`tests/conftest.py`), except the real-gateway tests, which load and unload only agents of their own.
- **Test data.** Tests use synthetic members only.
- **Test locale.** Tests run on the English locale. `tests/test_languages.py` checks the other languages. Code and tests hold no Cyrillic outside it and the locales (`tests/test_language_neutral.py`).

## Design decisions

| ID | Decision | Why |
|---|---|---|
| D16 | One SQLite database, `core.db`. Spaces are separated by Core's code. | Atomic transactions across everything Core knows. |
| D23 | Stage order: intake → processing → knowledge → reminders and self-checks → workflows → migration → maintenance agent → setup dialog → metrics → Hermes → code generation. | Useful early, with learning before migration. |
| D24 | Snapshots and cards are readable but signed. Snapshot manifests carry a monotonic generation number, and privacy events live in a separate signed journal. | Tampering and rollback become visible; reading needs no key. |
| D26 | Core supervises the host gateway: RUNNING, HOLD, STOPPED. The host has its own launchd service, runs under external supervision, never respawns itself and has config reload turned off. | Otherwise launchd, repair tools or hot reload would change the host behind Core's back. |
| D30 | The engine runs its own OpenClaw and Node, pinned, in a folder of its own, with its own config and state. It never touches another OpenClaw install. | Another install's updates, plugins or settings would change the engine's host behind Core's back. |
| D27 | A file on disk is named by its record id. The original name lives only in the database and the card. | A name chosen by the sender must never become a path. |
| D31 | Stages 1–3 use test data only. Real documents arrive from stage 4. | No data migrations while the design is still moving. |
| D32 | The space is decided at intake, by the caption. A later "only for me" is `make_private`. | Waiting for classification adds complexity without benefit. |
| D33 | Gatekeeper: only Core talks to the family bot, and the host gets a fake token. | Intake does not depend on host hooks; durable intake is Core's job; the host needs fewer unverified capabilities. |

## Roadmap

A stage is done when its acceptance scenarios pass on the test stand.

| Stage | Scope |
|---|---|
| 1a | Core intake: journal, attachments, receipts, spaces, signed cards, locales. **Done.** |
| 1b | Service bot for alerts and buttons; signed daily snapshots with a generation number; the daily "all good" line; Core as a launchd service with its own interpreter; at start it probes the documents folder and names the macOS permission it lacks. **Done.** |
| 1c | A dedicated OpenClaw install with a reference config; egress proxy; host-facing API with a fake token and accept-and-hold; adapter plugin with heartbeat; supervision with a live probe at start. **Done.** |
| 2 | Processing. 2a: the model answers with Core's instruction and Core's signed tools (search, get, send an original); replies as text only; a model probe at start. **Done.** 2b: transcription, OCR and document text in a sandbox, kept as texts that search finds (**done**); the attachment given to the host as text, with the chat's order, an interim answer and cancel, people known to the host by stand-ins, and Core's instruction as the whole system prompt (**done**). 2c-1: photos of a document to a PDF in the order the model reads, after its answer (**done**). 2c-2: a page viewer, a photo as a picture. |
| 3 | Knowledge: facts with verified quotes, instructions, session taint, disclosure journal, `make_private`. |
| 4 | Reminders delivered by Core, fenced delivery, restore with a generation check, integrity and silence checks, watchdog. |
| 5 | Workflows the family teaches by example, with versions, owner approval and rollback. |
| 6 | Migration of existing data. |
| 7 | Maintenance agent. |
| 8 | Setup and settings dialog for other families. |
| 9 | Metrics (Prometheus, Grafana). |
| 10 | Hermes adapter. |
| 11 | Code generation, as a separate private release. |
