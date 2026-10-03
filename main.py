import logging
import json
import os
from pathlib import Path
from typing import TypedDict

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("dokkan_bot")
PROJECT_DIR = Path(__file__).parent
BUILD_DATA_PATH = PROJECT_DIR / "data" / "builds.json"
THUMBS_DIR = PROJECT_DIR / "thumbs"
HIPO_EMOJI_GUILD_ID = 901246915881074709


class CharacterBuild(TypedDict):
    card_id: str
    name: str
    image: str
    aliases: list[str]
    hidden_potential: list[int]
    skill_orbs: str
    source: str


class BuildDraft(TypedDict):
    card_id: str
    name: str
    image: str
    aliases: list[str]


class BuildDataError(Exception):
    pass


def load_character_builds() -> list[CharacterBuild]:
    try:
        data = json.loads(BUILD_DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BuildDataError("Could not read character build data") from error

    if not isinstance(data, dict) or not isinstance(data.get("characters"), list):
        raise BuildDataError("Build data must contain a characters list")

    builds: list[CharacterBuild] = []
    for record in data["characters"]:
        if not isinstance(record, dict):
            raise BuildDataError("Each character build must be an object")

        card_id = record.get("card_id")
        name = record.get("name")
        image = record.get("image")
        aliases = record.get("aliases", [])
        hidden_potential = record.get("hidden_potential")
        skill_orbs = record.get("skill_orbs")
        source = record.get("source")
        if (
            not isinstance(card_id, str)
            or not card_id.strip()
            or not isinstance(name, str)
            or not name.strip()
            or not isinstance(image, str)
            or not image.strip()
            or Path(image).name != image
            or Path(image).suffix.casefold() not in {".png", ".jpg", ".jpeg", ".webp"}
            or not isinstance(aliases, list)
            or not all(isinstance(alias, str) for alias in aliases)
            or not isinstance(hidden_potential, list)
            or len(hidden_potential) != 3
            or not all(type(value) is int and value >= 0 for value in hidden_potential)
            or not isinstance(skill_orbs, str)
            or not skill_orbs.strip()
            or not isinstance(source, str)
            or not source.strip()
        ):
            raise BuildDataError("A character build is missing a required field")

        builds.append(
            {
                "card_id": card_id.strip(),
                "name": name.strip(),
                "image": image.strip(),
                "aliases": aliases,
                "hidden_potential": hidden_potential,
                "skill_orbs": skill_orbs.strip(),
                "source": source.strip(),
            }
        )
    return builds


def find_build_matches(query: str, builds: list[CharacterBuild]) -> list[CharacterBuild]:
    normalized_query = " ".join(query.casefold().split())
    exact_matches = []
    partial_matches = []

    for build in builds:
        normalized_aliases = [
            " ".join(alias.casefold().split()) for alias in build["aliases"]
        ]
        if normalized_query in normalized_aliases:
            exact_matches.append(build)
        elif any(normalized_query in alias for alias in normalized_aliases):
            partial_matches.append(build)

    return exact_matches or partial_matches


def get_card_image_path(card_id: str) -> Path | None:
    for extension in (".png", ".jpg", ".jpeg", ".webp"):
        image_path = THUMBS_DIR / f"card_{card_id}{extension}"
        if image_path.is_file():
            return image_path

    default_image_path = THUMBS_DIR / "default.png"
    return default_image_path if default_image_path.is_file() else None


def persist_character_builds(builds: list[CharacterBuild]) -> None:
    temporary_path = BUILD_DATA_PATH.with_suffix(".json.tmp")
    try:
        temporary_path.write_text(
            json.dumps({"characters": builds}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_path, BUILD_DATA_PATH)
    except OSError:
        temporary_path.unlink(missing_ok=True)
        raise


def save_character_build(build: CharacterBuild) -> None:
    builds = load_character_builds()
    if any(existing["card_id"] == build["card_id"] for existing in builds):
        raise ValueError("A build with this card ID already exists")

    builds.append(build)
    persist_character_builds(builds)


def update_character_build(
    card_id: str, hidden_potential: list[int], skill_orbs: str
) -> None:
    builds = load_character_builds()
    for index, build in enumerate(builds):
        if build["card_id"] == card_id:
            builds[index] = {
                **build,
                "hidden_potential": hidden_potential,
                "skill_orbs": skill_orbs,
            }
            persist_character_builds(builds)
            return

    raise LookupError(f"No build exists for card ID {card_id}")


class AddBuildModal(discord.ui.Modal, title="Add character build"):
    card_id_input = discord.ui.TextInput(
        label="Card ID",
        placeholder="Ex. 1031501 for LR PHY Omega Shenron",
        max_length=7,
    )
    name_input = discord.ui.TextInput(
        label="Character Name",
        placeholder="Ex. [The Surviving Savior] Hercule",
        max_length=100,)
    aliases_input = discord.ui.TextInput(
        label="Aliases (separate with commas)",
        placeholder="AGL Namek Goku, Nameku, Super Saiyan Goku",
        max_length=1000,
    )
    async def on_submit(self, interaction: discord.Interaction) -> None:
        card_id = self.card_id_input.value.strip()
        if not card_id.isascii() or not card_id.isdigit():
            await interaction.response.send_message(
                "Card ID must contain only digits.", ephemeral=True
            )
            return

        name = self.name_input.value.strip()
        if not name:
            await interaction.response.send_message(
                "Enter the character name.", ephemeral=True
            )
            return

        alias_values = [alias.strip() for alias in self.aliases_input.value.split(",")]
        aliases = []
        seen_aliases = set()
        for alias in [*alias_values, name]:
            normalized_alias = alias.casefold()
            if alias and normalized_alias not in seen_aliases:
                aliases.append(alias)
                seen_aliases.add(normalized_alias)

        image_path = get_card_image_path(card_id)
        image_name = image_path.name if image_path else f"{card_id}.png"

        draft: BuildDraft = {
            "card_id": card_id,
            "name": name,
            "image": image_name,
            "aliases": aliases,
        }
        await interaction.response.send_message(
            "Character details saved for this session. Continue with Hidden Potential and Skill Orbs.",
            view=AddBuildHipoView(interaction.user.id, draft),
            ephemeral=True,
        )


class AddBuildHipoModal(discord.ui.Modal, title="Hidden Potential"):
    dodge_input = discord.ui.TextInput(label="Dodge", placeholder="12", max_length=3)
    crit_input = discord.ui.TextInput(label="Crit", placeholder="3", max_length=3)
    additional_input = discord.ui.TextInput(
        label="Additional", placeholder="6", max_length=3
    )
    skill_orbs_input = discord.ui.TextInput(
        label="Skill orbs",
        style=discord.TextStyle.paragraph,
        max_length=1000,
    )

    def __init__(self, draft: BuildDraft) -> None:
        super().__init__()
        self.draft = draft

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw_values = [
            self.dodge_input.value.strip(),
            self.crit_input.value.strip(),
            self.additional_input.value.strip(),
        ]
        if any(not value.isascii() or not value.isdigit() for value in raw_values):
            await interaction.response.send_message(
                "Dodge, Crit, and Additional must each be a whole number.",
                ephemeral=True,
            )
            return

        skill_orbs = self.skill_orbs_input.value.strip()
        if not skill_orbs:
            await interaction.response.send_message(
                "Enter the skill orb setup as text.", ephemeral=True
            )
            return

        build: CharacterBuild = {
            **self.draft,
            "hidden_potential": [int(value) for value in raw_values],
            "skill_orbs": skill_orbs,
            "source": "Batman",
            #"source": interaction.user.display_name,
        }
        try:
            save_character_build(build)
        except ValueError:
            await interaction.response.send_message(
                f"A build for card ID `{self.draft['card_id']}` already exists.",
                ephemeral=True,
            )
            return
        except (BuildDataError, OSError):
            logger.exception(
                "Unable to save character build for card ID %s",
                self.draft["card_id"],
            )
            await interaction.response.send_message(
                "The build could not be saved. Check the build data file and try again.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"Added a build for card ID `{self.draft['card_id']}`.", ephemeral=True
        )


class AddBuildHipoButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Enter Build Details", style=discord.ButtonStyle.primary
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if isinstance(self.view, AddBuildHipoView):
            await interaction.response.send_modal(
                AddBuildHipoModal(self.view.draft)
            )


class AddBuildHipoView(discord.ui.View):
    def __init__(self, user_id: int, draft: BuildDraft) -> None:
        super().__init__(timeout=180)
        self.user_id = user_id
        self.draft = draft
        self.add_item(AddBuildHipoButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Only the person who started `/addbuild` can continue this form.",
                ephemeral=True,
            )
            return False
        return True


class EditBuildModal(discord.ui.Modal, title="Edit character build"):
    dodge_input = discord.ui.TextInput(label="Dodge", max_length=3)
    crit_input = discord.ui.TextInput(label="Crit", max_length=3)
    additional_input = discord.ui.TextInput(label="Additional", max_length=3)
    skill_orbs_input = discord.ui.TextInput(
        label="Skill orbs",
        style=discord.TextStyle.paragraph,
        max_length=1000,
    )

    def __init__(self, build: CharacterBuild) -> None:
        super().__init__()
        self.build = build
        self.dodge_input.default = str(build["hidden_potential"][0])
        self.crit_input.default = str(build["hidden_potential"][1])
        self.additional_input.default = str(build["hidden_potential"][2])
        self.skill_orbs_input.default = build["skill_orbs"]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw_values = [
            self.dodge_input.value.strip(),
            self.crit_input.value.strip(),
            self.additional_input.value.strip(),
        ]
        if any(not value.isascii() or not value.isdigit() for value in raw_values):
            await interaction.response.send_message(
                "Dodge, Crit, and Additional must each be a whole number.",
                ephemeral=True,
            )
            return

        skill_orbs = self.skill_orbs_input.value.strip()
        if not skill_orbs:
            await interaction.response.send_message(
                "Enter the skill orb setup as text.", ephemeral=True
            )
            return

        try:
            update_character_build(
                self.build["card_id"],
                [int(value) for value in raw_values],
                skill_orbs,
            )
        except LookupError:
            await interaction.response.send_message(
                "That build no longer exists. Run `/editbuild` again.", ephemeral=True
            )
            return
        except (BuildDataError, OSError):
            logger.exception(
                "Unable to update character build for card ID %s",
                self.build["card_id"],
            )
            await interaction.response.send_message(
                "The build could not be updated. Check the build data file and try again.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"Updated the build for **{self.build['name']}**.", ephemeral=True
        )


class BuildChoiceButton(discord.ui.Button):
    def __init__(self, label: str, index: int, row: int) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.primary, row=row)
        self.index = index

    async def callback(self, interaction: discord.Interaction) -> None:
        if isinstance(self.view, BuildSelectionView):
            await self.view.choose(interaction, self.index)


class BuildPageButton(discord.ui.Button):
    def __init__(self, label: str, direction: int, disabled: bool) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=4, disabled=disabled)
        self.direction = direction

    async def callback(self, interaction: discord.Interaction) -> None:
        if isinstance(self.view, BuildSelectionView):
            await self.view.change_page(interaction, self.direction)


class BuildSelectionView(discord.ui.View):
    page_size = 20

    def __init__(self, user_id: int, alias: str, matches: list[CharacterBuild]) -> None:
        super().__init__(timeout=120)
        self.user_id = user_id
        self.alias = alias
        self.matches = matches
        self.page = 0
        self.message: discord.InteractionMessage | None = None
        self.refresh_buttons()

    @property
    def page_count(self) -> int:
        return (len(self.matches) + self.page_size - 1) // self.page_size

    def refresh_buttons(self) -> None:
        self.clear_items()
        start_index = self.page * self.page_size
        page_matches = self.matches[start_index : start_index + self.page_size]
        for offset, match in enumerate(page_matches):
            label = match["name"][:80]
            self.add_item(BuildChoiceButton(label, start_index + offset, offset // 5))

        if self.page_count > 1:
            self.add_item(BuildPageButton("Previous", -1, self.page == 0))
            self.add_item(BuildPageButton("Next", 1, self.page == self.page_count - 1))

    def prompt(self) -> str:
        safe_alias = discord.utils.escape_mentions(discord.utils.escape_markdown(self.alias))
        return (
            f"Found {len(self.matches)} cards for **{safe_alias}**. "
            f"Choose a card below (page {self.page + 1}/{self.page_count})."
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Only the person who ran `/build` can choose a card.", ephemeral=True
            )
            return False
        return True

    async def choose(self, interaction: discord.Interaction, index: int) -> None:
        self.stop()
        await send_build_result(interaction, self.matches[index], ephemeral=True)

    async def change_page(self, interaction: discord.Interaction, direction: int) -> None:
        self.page += direction
        self.refresh_buttons()
        await interaction.response.edit_message(content=self.prompt(), view=self)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                logger.debug("Could not disable expired build selection buttons")


def format_hidden_potential(
    values: list[int], emojis: list[discord.Emoji]
) -> str:
    emoji_names = ("dodge", "crit", "add")
    guild_emojis = {emoji.name.casefold(): str(emoji) for emoji in emojis}
    if not all(name in guild_emojis for name in emoji_names):
        missing_names = [name for name in emoji_names if name not in guild_emojis]
        logger.warning(
            "Cannot render Hidden Potential emojis; missing %s. Available emoji names: %s",
            ", ".join(missing_names),
            ", ".join(sorted(guild_emojis)) or "none",
        )
        return f"Dodge: {values[0]}, Crit: {values[1]}, Add: {values[2]}"

    stats = zip(emoji_names, values)
    return "  ".join(
        f"{guild_emojis[name]} **{value}**" for name, value in stats
    )


async def fetch_hipo_emojis(client: discord.Client) -> list[discord.Emoji]:
    guild = client.get_guild(HIPO_EMOJI_GUILD_ID)
    if guild is None:
        try:
            guild = await client.fetch_guild(HIPO_EMOJI_GUILD_ID)
        except discord.HTTPException as error:
            logger.warning(
                "Could not access the emoji source guild %s: %s",
                HIPO_EMOJI_GUILD_ID,
                error,
            )
            return []

    emoji_names = {emoji.name.casefold() for emoji in guild.emojis}
    if {"dodge", "crit", "add"}.issubset(emoji_names):
        return list(guild.emojis)

    try:
        emojis = await guild.fetch_emojis()
    except discord.HTTPException as error:
        logger.warning(
            "Could not fetch emojis from source guild %s (%s): %s; cached emoji names: %s",
            guild.name,
            guild.id,
            error,
            ", ".join(sorted(emoji_names)) or "none",
        )
        return list(guild.emojis)

    found_names = {emoji.name.casefold() for emoji in emojis}
    missing_names = {"dodge", "crit", "add"} - found_names
    if missing_names:
        logger.warning(
            "Discord returned no complete Hidden Potential emoji set for source guild %s (%s); "
            "missing: %s; available: %s",
            guild.name,
            guild.id,
            ", ".join(sorted(missing_names)),
            ", ".join(sorted(found_names)) or "none",
        )
    return emojis


def create_build_embed(
    build: CharacterBuild, emojis: list[discord.Emoji]
) -> discord.Embed:
    embed = discord.Embed(title=build["name"], color=discord.Color.gold())
    embed.add_field(name="Card ID", value=build["card_id"], inline=True)
    embed.add_field(
        name="Hidden Potential",
        value=format_hidden_potential(build["hidden_potential"], emojis),
        inline=False,
    )
    embed.add_field(name="Skill Orbs", value=build["skill_orbs"], inline=False)
    display_name = discord.utils.escape_mentions(build["source"])
    #embed.set_footer(text=f"Build from: @{display_name}")
    embed.set_footer(text=f"Build from: @Batman")
    return embed


async def send_build_result(
    interaction: discord.Interaction, build: CharacterBuild, *, ephemeral: bool = False
) -> None:
    emojis = await fetch_hipo_emojis(interaction.client)
    embed = create_build_embed(build, emojis)
    files: list[discord.File] = []
    image_path = get_card_image_path(build["card_id"])
    if image_path:
        attachment_name = f"card_{image_path.name}"
        files.append(discord.File(image_path, filename=attachment_name))
        embed.set_thumbnail(url=f"attachment://{attachment_name}")
    else:
        embed.add_field(
            name="Card Image",
            value="The card image and `thumbs/default.png` were not found.",
            inline=False,
        )

    await interaction.response.send_message(
        embed=embed, files=files, ephemeral=ephemeral
    )

token = os.getenv("DISCORD_TOKEN")
if not token:
    raise SystemExit("DISCORD_TOKEN is missing. Copy .env.example to .env and add your bot token.")

guild_id_value = os.getenv("DISCORD_GUILD_ID")
try:
    development_guild_id = int(guild_id_value) if guild_id_value else None
except ValueError as error:
    raise SystemExit("DISCORD_GUILD_ID must be a numeric Discord server ID.") from error


class DokkanBot(commands.Bot):
    async def setup_hook(self) -> None:
        if development_guild_id:
            guild = discord.Object(id=development_guild_id)
            self.tree.copy_global_to(guild=guild)
            global_commands = await self.tree.sync()
            logger.info(
                "Synced %d global application commands: %s",
                len(global_commands),
                ", ".join(command.name for command in global_commands) or "none",
            )
            guild_commands = await self.tree.sync(guild=guild)
            logger.info(
                "Synced %d application commands to development server %s: %s",
                len(guild_commands),
                development_guild_id,
                ", ".join(command.name for command in guild_commands) or "none",
            )
        else:
            global_commands = await self.tree.sync()
            logger.info(
                "Synced %d global application commands: %s",
                len(global_commands),
                ", ".join(command.name for command in global_commands) or "none",
            )


bot = DokkanBot(command_prefix=commands.when_mentioned, intents=discord.Intents.default())


@bot.tree.command(name="ping", description="Check whether the bot is responding")
async def ping(interaction: discord.Interaction) -> None:
    latency_ms = round(bot.latency * 1000)
    await interaction.response.send_message(f"Pong! Gateway latency: {latency_ms} ms.")


@bot.tree.command(name="addbuild", description="Add a character build to the local database")
@app_commands.default_permissions(administrator=True)
async def addbuild(interaction: discord.Interaction) -> None:
    if not interaction.permissions.administrator:
        await interaction.response.send_message(
            "Administrator permission is required to add builds.", ephemeral=True
        )
        return

    await interaction.response.send_modal(AddBuildModal())


@bot.tree.command(name="editbuild", description="Edit an existing character build")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(card_id="Existing card ID")
async def editbuild(interaction: discord.Interaction, card_id: str) -> None:
    if not interaction.permissions.administrator:
        await interaction.response.send_message(
            "Administrator permission is required to edit builds.", ephemeral=True
        )
        return

    card_id = card_id.strip()
    if not card_id.isascii() or not card_id.isdigit():
        await interaction.response.send_message(
            "Card ID must contain only digits.", ephemeral=True
        )
        return

    try:
        builds = load_character_builds()
    except BuildDataError:
        logger.exception("Unable to load character build data")
        await interaction.response.send_message(
            "Character build data is unavailable right now.", ephemeral=True
        )
        return

    existing_build = next(
        (build for build in builds if build["card_id"] == card_id), None
    )
    if existing_build is None:
        await interaction.response.send_message(
            f"No build exists for card ID `{card_id}`.", ephemeral=True
        )
        return

    await interaction.response.send_modal(EditBuildModal(existing_build))


@bot.tree.command(name="build", description="Look up a character build by alias")
@app_commands.describe(name="Character name or alias")
async def build(interaction: discord.Interaction, name: str) -> None:
    try:
        builds = load_character_builds()
    except BuildDataError:
        logger.exception("Unable to load character build data")
        await interaction.response.send_message(
            "Character build data is unavailable right now.", ephemeral=True
        )
        return

    if not builds:
        await interaction.response.send_message(
            "No character builds have been added yet.", ephemeral=True
        )
        return

    normalized_name = " ".join(name.casefold().split())
    exact_name_matches = [
        character
        for character in builds
        if " ".join(character["name"].casefold().split()) == normalized_name
    ]
    if len(exact_name_matches) == 1:
        await send_build_result(interaction, exact_name_matches[0])
        return

    matches = exact_name_matches or find_build_matches(name, builds)
    if not matches:
        safe_name = discord.utils.escape_mentions(name)
        await interaction.response.send_message(
            f"No build found for **{safe_name}**. Check the spelling or try another name.",
            ephemeral=True,
        )
        return

    view = BuildSelectionView(interaction.user.id, name, matches)
    await interaction.response.send_message(
        content=view.prompt(), view=view, ephemeral=True
    )
    view.message = await interaction.original_response()


@bot.event
async def on_ready() -> None:
    logger.info("Logged in as %s (ID: %s)", bot.user, bot.user.id if bot.user else "unknown")


bot.run(token, log_handler=None)