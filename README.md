# klepa-engine (working name)

Family memory engine for agent hosts (OpenClaw first, Hermes later).
Stage 1a: Core receives files from Telegram and stores them durably.

Installation data (tokens, keys, databases, documents, real names and IDs) never goes into this repository.

## Develop

    uv sync
    uv run pytest
