# Security policy

Klepa stores a family's documents, so security reports are welcome and handled first.

## Reporting a vulnerability

Please **do not open a public issue.** Report privately via [GitHub security advisories](https://github.com/PaGrom/klepa/security/advisories/new) ("Report a vulnerability" on the Security tab).

Include what you found, how to reproduce it and what an attacker could reach. Do not include real personal data. You will get an answer within a week.

## Supported versions

The project is pre-release. Only the `main` branch is supported.

## What Klepa promises about data

- **Everything stays on the family's machine.** There is no telemetry and no cloud service of our own. The only network traffic goes to Telegram, plus, from stage 1c, the model API through Core's egress proxy.
- **Secrets stay in files.** The bot token lives only in a 0600 file. It never appears in databases, logs, events, URLs or error messages.
- **Originals are immutable.** They are written once, flushed to stable storage and never modified or deleted by the code.
- **Personal spaces stay personal.** A private caption keeps a file, or a whole album, out of the shared folder.
- **Content is data.** Incoming messages, documents, recognized text and transcripts are never treated as instructions.
- **Logs keep no content.** The event log holds metadata only: no message text, captions or file paths.

## Scope

This policy covers Core, the code in this repository. The agent host (OpenClaw, Hermes) has its own security policy.
