# ModMail

A minimal Discord ModMail bot: members DM the bot, staff answer from a private
channel. Inspired by the architecture of the well-known open-source ModMail
projects, but written from scratch — no code was copied from them.

## What works in Phase 1

- A member's DM opens a private staff channel in a `ModMail` category.
- Every further DM is relayed into that channel.
- Staff messages in the channel are relayed back to the member as a DM.
- **One open thread per member**, enforced in the database and in code.
- `?close [reason]` archives the thread, notifies the member and the log
  channel, then deletes the channel.
- `?reply <text>` DMs the member without posting in the channel.
- `?threadinfo` shows the current thread.
- Every relayed message is stored, and a closed thread gets an unguessable
  archive key ready for a future log viewer.
- DMs from one member are processed in order (a queue per member).

## Requirements

- The Python runtime in `../../tools/py-embed/py312` (already set up).
- A Discord application with a bot user.

## Setup

1. Copy `env.example` to `.env` and fill in the values.

   At minimum:

   ```
   DISCORD_TOKEN=your-bot-token
   DISCORD_GUILD_ID=your-server-id
   ```

   Optionally set `MODMAIL_CATEGORY_ID`, `LOG_CHANNEL_ID`, `COMMAND_PREFIX`.

   `.env` is gitignored. Never commit it, and never paste the token anywhere
   else.

2. In the Discord Developer Portal, enable these **Privileged Gateway
   Intents** for your bot, or it will not start:

   - **Server Members Intent**
   - **Message Content Intent**

3. Invite the bot to your server with at least these permissions:
   *View Channels*, *Send Messages*, *Manage Channels*, *Read Message History*,
   *Attach Files*, *Embed Links*, *Add Reactions*.

## Running

```powershell
& "C:\Users\hiki\Downloads\my ai\project\modmail\.venv\Scripts\python.exe" -m modmail
```

Run it from the project folder (`project\modmail`) so that `modmail.db` and
`logs\` land next to the code.

## Tests

```powershell
& "C:\Users\hiki\Downloads\my ai\project\modmail\.venv\Scripts\python.exe" -m pytest
```

32 tests, no network and no real Discord connection required.

## Layout

```
src/modmail/
  config.py           typed settings from .env; refuses to start without a token
  logging_setup.py    console + rotating file logs, with token redaction
  db.py               async engine and sessions (only place the DB URL is read)
  models.py           threads, thread_messages, log_entries
  thread_manager.py   thread lifecycle rules and queries
  relay.py            message translation between DM and staff channel
  bot.py              Discord wiring, channel creation, staff commands
  __main__.py         entry point
tests/                32 tests covering config, thread rules, and relay
```

## Later phases

The seams are already in place: change `DATABASE_URL` for PostgreSQL, and read
`log_entries.key` from a web service for the log viewer. Neither requires
touching the bot logic.
