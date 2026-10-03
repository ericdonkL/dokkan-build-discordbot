# Dokkan Build Bot

`discord.py` Dokkan build bot with `/ping`, `/build`, `/addbuild`, and `/editbuild` slash commands. Build records are stored in `data/builds.json`.

## Run on Windows

1. Create a bot application in the [Discord Developer Portal](https://discord.com/developers/applications) and copy its bot token.
2. Invite it to your server with the `bot` and `applications.commands` scopes.
3. Copy `.env.example` to `.env` and set `DISCORD_TOKEN`. Keep `.env` private; it is ignored by Git.
4. Optionally set `DISCORD_GUILD_ID` to your test server's ID. Commands sync globally for every server where the bot is installed, and also sync to this test server for faster development updates.
5. In PowerShell, run:

   ```powershell
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   python main.py
   ```

Try `/ping` in the server. `/build name:<name>` searches saved aliases only. An exact card-name match shows the build directly; every other successful search opens an ephemeral button picker, even if only one card matched. Picker buttons show card names and results are paginated when needed. `/addbuild` and `/editbuild` are restricted to server administrators. `/addbuild` collects card ID, display name, and comma-separated aliases, then opens a follow-up form for Dodge, Crit, Additional, and Skill Orbs. `/editbuild card_id:<id>` edits a record only when that exact card ID already exists, with its current hipo values and Skill Orbs prefilled. The display name is automatically included in aliases.

When `DISCORD_GUILD_ID` is set, startup syncs commands globally and to that test server. The guild commands update quickly during development, while other installed servers receive the global commands after Discord propagates them.

Each record in the `characters` array must include:

- `card_id`: card ID stored as a string
- `name`: character name shown in the result
- `image`: image filename only, for example `1020281.png`
- `aliases`: alternate names used for searching; `/addbuild` accepts comma-separated entries
- `hidden_potential`: three integers in `[dodge, crit, additional]` order
- `skill_orbs`: recommended skill orb setup
- `source`: submitting user's display name for builds added with `/addbuild`

Put card images in `thumbs/` with `card_` followed by the card ID, such as `card_1020281.png`. PNG, JPG, JPEG, and WebP are supported. The bot attaches the matching card-ID image and falls back to `thumbs/default.png` if none exists. Existing character names are included in each record's aliases so they remain searchable.

Build output is a single embed with the card art as a small thumbnail and Hidden Potential values in one line. The bot fetches `dodge`, `crit`, and `add` from testing guild `901246915881074709`, then uses them in build results across servers. The bot must be a member of that source guild and have **Use External Emojis** in the server where the result is posted; otherwise it falls back to `Dodge: X, Crit: Y, Add: Z`. `/addbuild` accepts comma-separated values such as `3, 20, 12` and stores them as `[3, 20, 12]`.

The build embed footer displays `Build from: @<display name>` as plain text, not a user mention. The bot cannot reconstruct submitter names for older records that predate this change. The bot supports case-insensitive exact and partial alias matches. Card IDs are not searched directly. Recommendations should be sourced and identified as community advice where applicable.

Do not share or commit your bot token.