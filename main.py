import asyncio
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


def _env_int(name: str, default: int | None = None) -> int | None:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be a numeric Discord ID.") from error


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().casefold()
    if not value:
        return default
    return value not in {"0", "false", "no", "off"}


PROJECT_DIR = Path(__file__).parent
BUILD_DATA_PATH = PROJECT_DIR / "data" / "builds.json"
THUMBS_DIR = PROJECT_DIR / "thumbs"
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")

# Server that hosts the Hidden Potential emojis (dodge / crit / add).
HIPO_EMOJI_GUILD_ID = _env_int("HIPO_EMOJI_GUILD_ID", 901246915881074709)
# /addbuild and /editbuild are only accepted when typed in this server.
BUILD_ADMIN_GUILD_ID = _env_int("BUILD_ADMIN_GUILD_ID", 901246915881074709)
# Optional development server: commands are synced there instantly.
DEVELOPMENT_GUILD_ID = _env_int("DISCORD_GUILD_ID")
# Command syncing is rate limited; set SYNC_COMMANDS_ON_START=0 to skip it.
SYNC_COMMANDS_ON_START = _env_flag("SYNC_COMMANDS_ON_START", True)
# Whether /build results are only visible to the person who ran the command.
BUILD_RESULTS_EPHEMERAL = False

# Serializes the load -> modify -> persist cycle. File I/O runs in worker
# threads (asyncio.to_thread), so other commands can run between those steps;
# the lock keeps two writers from overwriting each other's changes.
BUILD_LOCK = asyncio.Lock()
BUILD_LOCK = asyncio.Lock()


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


async def report_interaction_error(
    interaction: discord.Interaction, error: Exception, context: str
) -> None:
    """Log an unhandled error and tell the user something went wrong."""
    logger.error("Unhandled error in %s", context, exc_info=error)
    message = "Something went wrong. Please try again."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        logger.debug("Could not notify the user about the error in %s", context)


async def ensure_admin_context(interaction: discord.Interaction, label: str) -> bool:
    """Refuse an interaction unless it comes from the designated admin server.

    ``label`` is the command name shown in the refusal message (e.g. ``/addbuild``).

    Every entry point that can lead to a write calls this: the slash commands
    themselves, the modals that persist data, and the view that opens the second
    modal. A caller may only proceed when this returns ``True``.
    """
    if interaction.guild_id != BUILD_ADMIN_GUILD_ID:
        await interaction.response.send_message(
            f"`{label}` can only be used in the designated server.", ephemeral=True
        )
        return False

    if not interaction.permissions.administrator:
        await interaction.response.send_message(
            f"Administrator permission is required to use `{label}`.", ephemeral=True
        )
        return False

    return True


def validate_character_build(record: object) -> CharacterBuild:
    if not isinstance(record, dict):
        raise BuildDataError("Each character build must be an object")

    label = f"build with card_id={record.get('card_id')!r}"

    def text_field(key: str) -> str:
        value = record.get(key)
        if not isinstance(value, str) or not value.strip():
            raise BuildDataError(f"{label}: '{key}' must be a non-empty string")
        return value.strip()

    card_id = text_field("card_id")
    if not (card_id.isascii() and card_id.isdigit()):
        raise BuildDataError(f"{label}: 'card_id' must contain only digits")

    name = text_field("name")

    image = text_field("image")
    if (
        Path(image).name != image
        or Path(image).suffix.casefold() not in IMAGE_EXTENSIONS
    ):
        raise BuildDataError(
            f"{label}: 'image' must be a bare file name ending in "
            f"{', '.join(IMAGE_EXTENSIONS)}"
        )

    aliases = record.get("aliases", [])
    if not isinstance(aliases, list) or not all(
        isinstance(alias, str) for alias in aliases
    ):
        raise BuildDataError(f"{label}: 'aliases' must be a list of strings")

    hidden_potential = record.get("hidden_potential")
    if (
        not isinstance(hidden_potential, list)
        or len(hidden_potential) != 3
        or not all(type(value) is int and value >= 0 for value in hidden_potential)
    ):
        raise BuildDataError(
            f"{label}: 'hidden_potential' must be three non-negative integers"
        )

    skill_orbs = text_field("skill_orbs")
    source = text_field("source")

    return {
        "card_id": card_id,
        "name": name,
        "image": image,
        "aliases": [alias.strip() for alias in aliases if alias.strip()],
        "hidden_potential": hidden_potential,
        "skill_orbs": skill_orbs,
        "source": source,
    }


def load_character_builds(*, strict: bool = True) -> list[CharacterBuild]:
    """Read and validate builds.

    With ``strict`` (the default) any invalid record raises, which is what the
    write paths need so a save never rewrites the file without the records it
    could not parse. Read paths pass ``strict=False`` so a single bad record
    only costs that record instead of hiding every other build.
    """
    try:
        data = json.loads(BUILD_DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BuildDataError("Could not read character build data") from error

    if not isinstance(data, dict) or not isinstance(data.get("characters"), list):
        raise BuildDataError("Build data must contain a characters list")

    builds: list[CharacterBuild] = []
    for index, record in enumerate(data["characters"]):
        try:
            builds.append(validate_character_build(record))
        except BuildDataError as error:
            if strict:
                raise
            logger.warning(
                "Skipping invalid character build at index %d: %s", index, error
            )
    return builds


def find_build_matches(query: str, builds: list[CharacterBuild]) -> list[CharacterBuild]:
    normalized_query = " ".join(query.casefold().split())
    if not normalized_query:
        return []

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


def find_card_image(card_id: str) -> Path | None:
    """Return the card-specific thumbnail for ``card_id`` if one exists."""
    for extension in IMAGE_EXTENSIONS:
        image_path = THUMBS_DIR / f"card_{card_id}{extension}"
        if image_path.is_file():
            return image_path
    return None


def get_card_image_path(card_id: str) -> Path | None:
    """Return the card thumbnail, falling back to ``thumbs/default.png``."""
    card_image = find_card_image(card_id)
    if card_image:
        return card_image

    default_image_path = THUMBS_DIR / "default.png"
    return default_image_path if default_image_path.is_file() else None


def resolve_build_image(build: CharacterBuild) -> Path | None:
    """Pick the image to show: card thumbnail, the stored file, then the default."""
    card_image = find_card_image(build["card_id"])
    if card_image:
        return card_image

    stored_image = THUMBS_DIR / build["image"]
    if stored_image.is_file():
        return stored_image

    return get_card_image_path(build["card_id"])


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


async def save_character_build(build: CharacterBuild) -> None:
    async with BUILD_LOCK:
        builds = await asyncio.to_thread(load_character_builds)
        if any(existing["card_id"] == build["card_id"] for existing in builds):
            raise ValueError("A build with this card ID already exists")

        builds.append(build)
        await asyncio.to_thread(persist_character_builds, builds)


async def update_character_build(
    card_id: str, hidden_potential: list[int], skill_orbs: str
) -> None:
    async with BUILD_LOCK:
        builds = await asyncio.to_thread(load_character_builds)
        for index, build in enumerate(builds):
            if build["card_id"] == card_id:
                builds[index] = {
                    **build,
                    "hidden_potential": hidden_potential,
                    "skill_orbs": skill_orbs,
                }
                await asyncio.to_thread(persist_character_builds, builds)
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
        max_length=100,
    )
    aliases_input = discord.ui.TextInput(
        label="Aliases (separate with commas)",
        placeholder="AGL Namek Goku, Nameku, Super Saiyan Goku",
        max_length=1000,
    )

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        await report_interaction_error(interaction, error, "add-build modal")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await ensure_admin_context(interaction, "/addbuild"):
            return

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

        # Use the card's own thumbnail name when it exists; otherwise record the
        # name the thumbnail should be given later (thumbs/card_<id>.png).
        image_path = find_card_image(card_id)
        image_name = image_path.name if image_path else f"card_{card_id}.png"

        draft: BuildDraft = {
            "card_id": card_id,
            "name": name,
            "image": image_name,
            "aliases": aliases,
        }
        view = AddBuildHipoView(interaction.user.id, draft)
        await interaction.response.send_message(
            "Character details saved for this session. Continue with Hidden Potential and Skill Orbs.",
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()


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

    def __init__(self, draft: BuildDraft, view: "AddBuildHipoView") -> None:
        super().__init__()
        self.draft = draft
        self.prompt_view = view

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        await report_interaction_error(interaction, error, "add-build details modal")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await ensure_admin_context(interaction, "/addbuild"):
            return

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
            "source": ".batman.616",
            #"source": interaction.user.display_name,
        }
        try:
            await save_character_build(build)
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
        await self.clear_prompt()

    async def clear_prompt(self) -> None:
        self.prompt_view.stop()
        message = self.prompt_view.message
        if message is None:
            return
        try:
            await message.edit(content="Build saved.", view=None)
        except discord.HTTPException:
            logger.debug("Could not clear the add-build prompt message")


class AddBuildHipoButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Enter Build Details", style=discord.ButtonStyle.primary
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if isinstance(self.view, AddBuildHipoView):
            await interaction.response.send_modal(
                AddBuildHipoModal(self.view.draft, self.view)
            )


class AddBuildHipoView(discord.ui.View):
    def __init__(self, user_id: int, draft: BuildDraft) -> None:
        super().__init__(timeout=180)
        self.user_id = user_id
        self.draft = draft
        self.message: discord.InteractionMessage | None = None
        self.add_item(AddBuildHipoButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Only the person who started `/addbuild` can continue this form.",
                ephemeral=True,
            )
            return False
        return await ensure_admin_context(interaction, "/addbuild")

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                logger.debug("Could not disable expired add-build button")

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        await report_interaction_error(interaction, error, "add-build view")


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

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        await report_interaction_error(interaction, error, "edit-build modal")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await ensure_admin_context(interaction, "/editbuild"):
            return

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
            await update_character_build(
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
            suffix = f" ({match['card_id']})"
            label = (match["name"][: max(0, 80 - len(suffix))] + suffix)[:80]
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
        build = self.matches[index]
        self.stop()
        try:
            await send_build_result(interaction, build)
        except Exception:
            await self.clear_picker("Could not load that card.")
            raise
        await self.clear_picker("Card selected.")

    async def clear_picker(self, text: str = "Card selected.") -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(content=text, view=None)
        except discord.HTTPException:
            logger.debug("Could not clear the build picker message")

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

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        await report_interaction_error(interaction, error, "build selection view")


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
    skill_orbs = build["skill_orbs"]
    if len(skill_orbs) > 1024:  # Discord's embed field value limit
        skill_orbs = skill_orbs[:1021] + "..."
    embed.add_field(name="Skill Orbs", value=skill_orbs, inline=False)
    display_name = discord.utils.escape_mentions(build["source"])
    embed.set_footer(text=f"Build from: {display_name}")
    return embed


async def send_build_result(
    interaction: discord.Interaction,
    build: CharacterBuild,
    *,
    ephemeral: bool = BUILD_RESULTS_EPHEMERAL,
) -> None:
    # Acknowledge right away: looking up the emojis can call the Discord API,
    # which could otherwise push us past the 3-second interaction deadline.
    await interaction.response.defer(ephemeral=ephemeral)

    emojis = await fetch_hipo_emojis(interaction.client)
    embed = create_build_embed(build, emojis)
    files: list[discord.File] = []
    image_path = resolve_build_image(build)
    if image_path:
        attachment_name = image_path.name
        files.append(discord.File(image_path, filename=attachment_name))
        embed.set_thumbnail(url=f"attachment://{attachment_name}")
    else:
        embed.add_field(
            name="Card Image",
            value="The card image and `thumbs/default.png` were not found.",
            inline=False,
        )

    await interaction.followup.send(embed=embed, files=files, ephemeral=ephemeral)


class DokkanBot(commands.Bot):
    async def setup_hook(self) -> None:
        if not SYNC_COMMANDS_ON_START:
            logger.info("Skipping command sync (SYNC_COMMANDS_ON_START is off)")
            return

        if DEVELOPMENT_GUILD_ID:
            # Copy the global commands into the development server and sync only
            # there. The global set is cleared (and the now-empty global set is
            # synced) so the development server does not list every command twice.
            guild = discord.Object(id=DEVELOPMENT_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
            guild_commands = await self.tree.sync(guild=guild)
            logger.info(
                "Synced %d application commands to development server %s: %s",
                len(guild_commands),
                DEVELOPMENT_GUILD_ID,
                ", ".join(command.name for command in guild_commands) or "none",
            )
        else:
            global_commands = await self.tree.sync()
            logger.info(
                "Synced %d global application commands: %s",
                len(global_commands),
                ", ".join(command.name for command in global_commands) or "none",
            )

        await self.sync_admin_commands()

    async def sync_admin_commands(self) -> None:
        """Register the server-restricted admin commands (/addbuild, /editbuild)."""
        admin_guild = discord.Object(id=BUILD_ADMIN_GUILD_ID)
        if not self.tree.get_commands(guild=admin_guild):
            logger.error(
                "No commands are registered locally for admin server %s; check the "
                "@app_commands.guilds decorators on /addbuild and /editbuild",
                BUILD_ADMIN_GUILD_ID,
            )
            return

        if DEVELOPMENT_GUILD_ID == BUILD_ADMIN_GUILD_ID:
            logger.info(
                "Admin server %s already synced as the development server",
                BUILD_ADMIN_GUILD_ID,
            )
            return

        try:
            synced = await self.tree.sync(guild=admin_guild)
        except discord.HTTPException:
            logger.exception(
                "Could not sync commands to admin server %s; /addbuild and /editbuild "
                "will not appear until the bot is a member of that server",
                BUILD_ADMIN_GUILD_ID,
            )
            return

        logger.info(
            "Synced %d commands to admin server %s: %s",
            len(synced),
            BUILD_ADMIN_GUILD_ID,
            ", ".join(command.name for command in synced) or "none",
        )


bot = DokkanBot(command_prefix=commands.when_mentioned, intents=discord.Intents.default())


@bot.tree.command(name="ping", description="Check whether the bot is responding")
async def ping(interaction: discord.Interaction) -> None:
    latency_ms = round(bot.latency * 1000)
    await interaction.response.send_message(f"Pong! Gateway latency: {latency_ms} ms.")


@bot.tree.command(name="addbuild", description="Add a character build to the local database")
@app_commands.default_permissions(administrator=True)
@app_commands.guilds(BUILD_ADMIN_GUILD_ID)
async def addbuild(interaction: discord.Interaction) -> None:
    if not await ensure_admin_context(interaction, "/addbuild"):
        return

    await interaction.response.send_modal(AddBuildModal())


@bot.tree.command(name="editbuild", description="Edit an existing character build")
@app_commands.default_permissions(administrator=True)
@app_commands.guilds(BUILD_ADMIN_GUILD_ID)
@app_commands.describe(card_id="Existing card ID")
async def editbuild(interaction: discord.Interaction, card_id: str) -> None:
    if not await ensure_admin_context(interaction, "/editbuild"):
        return

    card_id = card_id.strip()
    if not card_id.isascii() or not card_id.isdigit():
        await interaction.response.send_message(
            "Card ID must contain only digits.", ephemeral=True
        )
        return

    try:
        builds = await asyncio.to_thread(load_character_builds, strict=False)
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
async def build_command(interaction: discord.Interaction, name: str) -> None:
    normalized_name = " ".join(name.casefold().split())
    if not normalized_name:
        await interaction.response.send_message(
            "Enter a character name or alias to search for.", ephemeral=True
        )
        return

    try:
        builds = await asyncio.to_thread(load_character_builds, strict=False)
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


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    command_name = interaction.command.name if interaction.command else "unknown"
    await report_interaction_error(interaction, error, f"/{command_name}")


@bot.event
async def on_ready() -> None:
    logger.info("Logged in as %s (ID: %s)", bot.user, bot.user.id if bot.user else "unknown")


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit(
            "DISCORD_TOKEN is missing. Copy .env.example to .env and add your bot token."
        )
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
