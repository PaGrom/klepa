# klepa-engine (working name)

Family memory engine for agent hosts (OpenClaw first, Hermes later).
Stage 1a: Core receives files from Telegram and stores them durably.

Installation data (tokens, keys, databases, documents, real names and IDs) never goes into this repository.

## Develop

    uv sync
    uv run pytest

## Run stage 1a against a test bot

Stage 1a works with test data only. Never point it at the live bot: a second poller on the same token
makes Telegram answer 409 to both.

1. Create a separate test bot in @BotFather. Turn off `/setjoingroups` for it.
2. Copy `config/example.toml` outside the repository, for example to `~/KlepaData-test/config.toml`, and fill in:
   - paths outside iCloud/CloudStorage;
   - your Telegram ID;
   - the test bot token file path.
3. Put the token into its file without echo and without shell history:

       mkdir -p ~/KlepaData-test/keys && chmod 700 ~/KlepaData-test ~/KlepaData-test/keys
       umask 077; read -rs TOKEN; printf %s "$TOKEN" > ~/KlepaData-test/keys/family-bot.token; unset TOKEN

4. Initialize and run in the foreground:

       uv run python -m klepa_core init --config ~/KlepaData-test/config.toml
       uv run python -m klepa_core run --config ~/KlepaData-test/config.toml

5. Stop with Ctrl-C. Core finishes pending receipts before it exits.
