import logging

import discord

from .core import HELP, Core, Reply

log = logging.getLogger(__name__)

COMMANDS = {"saldo", "miesiac", "miesiąc", "odswiez", "odśwież", "pomoc", "help", "whoami"}


class ReplyView(discord.ui.View):
    def __init__(self, core: Core, reply: Reply):
        super().__init__(timeout=24 * 3600)
        self.core = core
        for label, data in reply.buttons:
            style = discord.ButtonStyle.danger if data.startswith("no:") else discord.ButtonStyle.primary
            button = discord.ui.Button(label=label, style=style, custom_id=data)
            button.callback = self._make_callback(data)
            self.add_item(button)

    def _make_callback(self, data: str):
        async def callback(interaction: discord.Interaction) -> None:
            await interaction.response.edit_message(view=None)
            reply = await self.core.handle_callback(f"dc:{interaction.user.id}", data)
            await interaction.followup.send(reply.text, view=_view(self.core, reply))
        return callback


def _view(core: Core, reply: Reply):
    return ReplyView(core, reply) if reply.buttons else discord.utils.MISSING


def build(core: Core) -> discord.Client:
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    allowed = core.cfg.discord_allowed
    channels = core.cfg.discord_channels

    @client.event
    async def on_ready() -> None:
        log.info("Discord bot działa jako %s", client.user)

    @client.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot:
            return
        is_dm = isinstance(message.channel, discord.DMChannel)
        if not is_dm and message.channel.id not in channels:
            return
        text = message.content.strip()
        command = text.lstrip("!/").lower() if text[:1] in "!/" else ""
        if command == "whoami":
            await message.reply(f"Twoje Discord ID: {message.author.id}")
            return
        if message.author.id not in allowed:
            return

        if command in COMMANDS:
            reply = {
                "saldo": core.balances,
                "miesiac": core.month_summary,
                "miesiąc": core.month_summary,
                "odswiez": core.refresh,
                "odśwież": core.refresh,
            }.get(command)
            reply = await reply() if reply else Reply(HELP.replace("Komendy:", "Komendy (z `!`):"))
            await message.reply(reply.text)
            return

        images = [
            (await a.read(), a.content_type.split(";")[0])
            for a in message.attachments
            if (a.content_type or "").startswith("image/")
        ]
        if not text and not images:
            return
        async with message.channel.typing():
            try:
                reply = await core.handle_message(f"dc:{message.author.id}", text, images)
            except Exception:
                log.exception("Błąd obsługi wiadomości")
                reply = Reply("⚠️ Coś poszło nie tak. Spróbuj ponownie za chwilę.")
        await message.reply(reply.text, view=_view(core, reply))

    return client
