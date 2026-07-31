# Discord Game Night Bot

A self-hosted Discord bot that turns Steam and Epic Games Store suggestions
into fast animated game-night wheels. It maintains multiplayer and
single-player libraries, enriches game metadata, tracks played games and
keeps unreleased suggestions on a wishlist.

This release is designed for **one bot instance connected to one Discord
server**. Run a separate instance and database for each server.

## See the wheel in action

The wheel cycles through a fresh random selection and reveals the winner in
one animated Discord message.

![Game Night Bot wheel selecting a random game](assets/spin-demo.gif)

## Features

- Steam and Epic Games Store link imports from a Discord suggestion thread
- Smooth, single-file animated wheel with a random selection each spin
- Online multiplayer and single-player/local-only wheels
- SteamGridDB artwork fallback, especially for Epic titles
- IGDB metadata enrichment for player limits, multiplayer support and genres
- Automatic wishlist release checks using the configured local timezone
- Local, compressed artwork cache for fast spins and low memory use
- Game history, statistics and recently-played avoidance
- Safe moderator undo for the latest lock-in
- Daily validated SQLite backups with seven-copy rotation
- Admin sync, repair, audit and delete commands
- Automatic database schema upgrades and startup integrity checks

## Before you begin

You need:

- Python 3.13 or newer
- A Discord application and bot token
- A Discord thread where members will post store links
- Optional SteamGridDB and IGDB credentials for the best artwork and metadata

Never publish `.env`, `config.json`, `database/games.db`, backups or cached
artwork. They are ignored by Git by default.

## 1. Create the Discord bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications)
   and create an application.
2. Open **Bot**, create the bot user and copy/reset its token.
3. Enable **Message Content Intent**. The bot needs this to read store links
   posted in the suggestion thread.
4. Under **OAuth2 > URL Generator**, select the `bot` and
   `applications.commands` scopes.
5. Give the bot these permissions:

   - View Channels
   - Send Messages
   - Send Messages in Threads
   - Embed Links
   - Attach Files
   - Read Message History

6. Open the generated URL and add the bot to your server.

Custom server emojis are optional. If you configure them, the bot may also
need **Use External Emojis** when posting outside their home server.

## 2. Install the bot

Download or clone this repository, open a terminal in its folder and install
the dependencies:

```console
python -m pip install -r requirements.txt
```

Copy the example files:

```powershell
Copy-Item .env.example .env
Copy-Item config.example.json config.json
```

On Linux or macOS:

```console
cp .env.example .env
cp config.example.json config.json
```

Put the Discord token in `.env`:

```dotenv
DISCORD_TOKEN=your_real_bot_token
```

Do not add quotes or spaces around the token.

## 3. Configure your server

Enable **Developer Mode** in Discord under **User Settings > Advanced**.
Right-click the suggestion thread, choose **Copy ID**, then replace the
example in `config.json`:

```json
{
  "suggestion_thread_id": "123456789012345678",
  "display_timezone_offset": "+10:00",
  "steam_country_code": "AU",
  "steam_language": "english",
  "store_accept_language": "en-AU,en;q=0.9",
  "epic_store_locale": "en-AU"
}
```

`display_timezone_offset` is a fixed UTC offset. Examples are `+10:00`,
`-05:00` and `+05:30`. Use `+00:00` for UTC. Environment values with the
uppercase names shown in `.env.example` override `config.json`.

The default platform icons are ordinary Unicode emoji. Optional Discord
custom emoji values can be placed in `.env`:

```dotenv
STEAM_EMOJI=<:Steam:123456789012345678>
EPIC_EMOJI=<:EpicGames:123456789012345678>
```

## 4. Optional metadata integrations

The bot works without these integrations, but some Epic artwork and player
metadata may be missing.

### SteamGridDB

Create a SteamGridDB API key and add it to `.env`:

```dotenv
STEAMGRIDDB_API_KEY=your_key
```

### IGDB

Create a Twitch developer application for IGDB and add the client credentials:

```dotenv
IGDB_CLIENT_ID=your_client_id
IGDB_CLIENT_SECRET=your_client_secret
```

The secret is a Twitch application client secret, not your Twitch password.

## 5. Start and populate the bot

Start it with:

```console
python bot.py
```

The first startup creates `database/games.db` and runtime directories.
Post Steam or Epic Games Store links in the configured suggestion thread,
then run `/syncgames` as a server moderator. Discord slash-command changes
can take a short time to appear after the first startup.

## Commands

Everyone:

- `/ping` checks whether the bot is online.
- `/games` lists the online multiplayer wheel.
- `/singleplayergames` lists the single-player/local-only wheel.
- `/spin` spins the online multiplayer wheel.
- `/singleplayerspin` spins the single-player/local-only wheel.
- `/history` shows recent game-night history.
- `/stats` shows game-night statistics.
- `/wishlist` lists unreleased suggestions.

Members with **Manage Server**:

- `/syncgames` performs a full thread, metadata and artwork sync.
- `/repairgames` repairs Steam metadata, demos and duplicates.
- `/auditgames` exports missing metadata information.
- `/checkreleases` immediately checks wishlist release status.
- `/undo` previews and reverses the latest lock-in.
- `/deletegame` removes an incorrectly imported game.
- `/resetplaycounts` resets play history after confirmation.

## Storage and backups

Runtime data stays local to the bot:

```text
database/games.db
database/artwork/
database/backups/
```

The bot checks every six hours and makes a consistent SQLite snapshot when
the latest automatic backup is at least 24 hours old. It validates the
snapshot and keeps the latest seven automatic backups:

```text
database/backups/games-auto-YYYYMMDD-HHMMSSZ.db
```

Only files beginning with `games-auto-` are rotated. Manual backup files are
not removed.

When updating the code, preserve `database/games.db`, `database/artwork/`,
`database/backups/`, `.env` and `config.json`. Replacing those with files
from a release will erase local configuration or force artwork to rebuild.

## Tests

Compile-check the source:

```console
python -m compileall bot.py settings.py commands database ui utils
```

Run the automated tests:

```console
python -m unittest discover -s tests -v
```

## Troubleshooting

- **Commands do not appear:** confirm the invite used both `bot` and
  `applications.commands`, then restart the bot.
- **Suggestions are ignored:** confirm Message Content Intent is enabled,
  the thread ID is correct and the bot can view/read that thread.
- **Missing artwork:** configure SteamGridDB, then run `/syncgames`.
- **Missing player data:** configure IGDB, then run `/syncgames`.
- **Permission errors:** recheck the permissions listed in the Discord setup.
- **A spin waits before rendering:** GIF creation is intentionally limited to
  one at a time per bot instance to protect small hosting plans from memory
  and CPU spikes.

## Privacy and third-party content

The bot stores suggestion details, Discord user IDs associated with
suggestions/lock-ins and game history in its local SQLite database. Operators
are responsible for securing and backing up that data and for complying with
their community's privacy requirements.

Game names, artwork, store data and platform marks belong to their respective
owners. They are fetched at runtime and are not included in this repository.
The MIT licence applies to the source code, not third-party artwork,
trademarks or API data.

## Contributing

Bug reports, feature ideas, documentation improvements, testing and pull
requests are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) before starting.

Please follow the [Code of Conduct](CODE_OF_CONDUCT.md). Potential security
vulnerabilities must be reported privately using the instructions in
[SECURITY.md](SECURITY.md), not through a public issue.

## Licence

Released under the [MIT License](LICENSE).
