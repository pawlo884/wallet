import logging

import discord

from .core import HELP, Core, Reply

log = logging.getLogger(__name__)


def _view(reply: Reply):
    """Przyciski bez własnych callbacków — kliknięcia obsługuje on_interaction,
    dzięki czemu działają także po restarcie bota (stan płatności jest w pliku)."""
    if not reply.buttons:
        return discord.utils.MISSING
    view = discord.ui.View(timeout=None)
    for i, (label, data) in enumerate(reply.buttons[:25]):
        style = discord.ButtonStyle.danger if data.startswith(("no:", "ss:")) else discord.ButtonStyle.primary
        if data.startswith("pk:"):
            style = discord.ButtonStyle.secondary
        # Discord: max 5 wierszy po 5 przycisków; lista wyboru układa się po 5 w wierszu, wg dat.
        row = i // 5 if reply.column else None
        view.add_item(discord.ui.Button(label=label[:80], style=style, custom_id=data, row=row))
    return view


def build(core: Core) -> discord.Client:
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    allowed = core.cfg.discord_allowed
    channels = core.cfg.discord_channels

    commands = {
        "saldo": core.balances,
        "miesiac": core.month_summary,
        "miesiąc": core.month_summary,
        "zaplanowane": core.planned_overview,
        "plany": core.plans_list,
        "odswiez": core.refresh,
        "odśwież": core.refresh,
    }

    async def send(channel, replies: Reply | list[Reply], reference=None) -> None:
        for i, reply in enumerate(replies if isinstance(replies, list) else [replies]):
            await channel.send(reply.text, view=_view(reply), reference=reference if i == 0 else None)

    @client.event
    async def on_ready() -> None:
        log.info("Discord bot działa jako %s", client.user)

    @client.event
    async def on_interaction(interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        if interaction.user.id not in allowed:
            await interaction.response.send_message("Brak dostępu", ephemeral=True)
            return
        await interaction.response.edit_message(view=None)
        data = (interaction.data or {}).get("custom_id", "")
        try:
            reply = await core.handle_callback(f"dc:{interaction.user.id}", data)
        except Exception:
            log.exception("Błąd obsługi przycisku")
            reply = Reply("⚠️ Coś poszło nie tak. Spróbuj ponownie za chwilę.")
        await interaction.followup.send(reply.text, view=_view(reply))

    @client.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot:
            return
        is_dm = isinstance(message.channel, discord.DMChannel)
        if not is_dm and message.channel.id not in channels:
            return
        text = message.content.strip()
        command, args = "", ""
        if text[:1] in ("!", "/"):
            parts = text[1:].strip().split(None, 1)  # po komendzie może być nowa linia (wklejony wyciąg)
            command = parts[0].lower() if parts else ""
            args = parts[1] if len(parts) > 1 else ""
        if command == "whoami":
            await message.reply(f"Twoje Discord ID: {message.author.id}")
            return
        if message.author.id not in allowed:
            return

        if command in commands:
            await send(message.channel, await commands[command](), reference=message)
            return
        if command == "wyciag":
            async with message.channel.typing():
                replies = await core.statement(f"dc:{message.author.id}", args)
            await send(message.channel, replies, reference=message)
            return
        doc = next(
            (a for a in message.attachments if a.filename.lower().endswith((".eml", ".txt"))), None
        )
        if doc:  # udostępniony mail (.eml) albo tekst wyciągu
            async with message.channel.typing():
                replies = await core.statement_file(f"dc:{message.author.id}", await doc.read())
            await send(message.channel, replies, reference=message)
            return
        if command == "kurs":
            await send(message.channel, await core.fx_quote(args), reference=message)
            return
        if command == "plan":
            async with message.channel.typing():
                reply = await core.plan_add(f"dc:{message.author.id}", args)
            await send(message.channel, reply, reference=message)
            return
        if command in ("pomoc", "help", "start"):
            await message.reply(HELP.replace("Komendy:", "Komendy (z `!`):"))
            return

        voice = next((a for a in message.attachments if (a.content_type or "").startswith("audio/")), None)
        if voice:  # wiadomość głosowa Discorda / plik audio
            async with message.channel.typing():
                try:
                    reply = await core.handle_voice(f"dc:{message.author.id}", await voice.read())
                except Exception:
                    log.exception("Błąd obsługi głosówki")
                    reply = Reply("⚠️ Coś poszło nie tak. Spróbuj ponownie za chwilę.")
            await send(message.channel, reply, reference=message)
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
        await send(message.channel, reply, reference=message)

    return client


def notifier(client: discord.Client, core: Core):
    """Wysyła wiadomość z inicjatywy bota w DM do każdej dozwolonej osoby."""

    async def notify(reply: Reply) -> None:
        await client.wait_until_ready()
        for uid in core.cfg.discord_allowed:
            try:
                user = client.get_user(uid) or await client.fetch_user(uid)
                await user.send(reply.text, view=_view(reply))
            except Exception:
                log.exception("Discord: nie udało się wysłać DM do %s", uid)

    return notify
