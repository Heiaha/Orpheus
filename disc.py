import os
import datetime
import discord
import youtube_dl
import asyncio
import math
import itertools
import random

from discord.ext import commands

ORPHEUS_DISCORD_TOKEN = os.environ.get("ORPHEUS_DISCORD_TOKEN")

ytdl_format_options = {
    "format": "bestaudio/best",
    "outtmpl": "%(extractor)s-%(id)s-%(title)s.%(ext)s",
    "restrictfilenames": True,
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "source_address": "0.0.0.0",  # bind to ipv4 since ipv6 addresses cause issues sometimes
}
FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

ytdl = youtube_dl.YoutubeDL(ytdl_format_options)
EMBED_COLOR = 0xA84300


# class queue idea from a gist by vbe0201
class SongQueue(asyncio.Queue):
    def __getitem__(self, item):
        if isinstance(item, slice):
            return list(itertools.islice(self._queue, item.start, item.stop, item.step))
        else:
            return self._queue[item]

    def __iter__(self):
        return self._queue.__iter__()

    def __len__(self):
        return self.qsize()

    def clear(self):
        self._queue.clear()

    def shuffle(self):
        random.shuffle(self._queue)

    def remove(self, index: int):
        del self._queue[index]


class Song(discord.PCMVolumeTransformer):
    def __init__(
        self,
        ctx: commands.Context,
        source: discord.FFmpegPCMAudio,
        *,
        data: dict,
        volume: float = 0.5,
    ):
        super().__init__(source, volume)

        self.title = data.get("title")
        self.video = data.get("url")
        self.url = data.get("webpage_url")
        self.thumbnail = data.get("thumbnail")
        self.duration = datetime.timedelta(seconds=data.get("duration"))

        self.requester = ctx.author
        self.channel = ctx.channel
        self.data = data

    @classmethod
    async def from_search(cls, ctx, search, *, loop=None, stream=True):
        loop = loop or asyncio.get_event_loop()
        entries = await loop.run_in_executor(
            None, lambda: ytdl.extract_info(search, download=not stream)
        )

        if entries is None:
            raise ValueError(f"Couldn't find anything that matches `{search}`")
        data = entries.get("entries")
        if data is None:
            data = entries
        else:
            for entry in entries["entries"]:
                if entry:
                    data = entry
                    break
        if data["is_live"]:
            await ctx.reply(
                "Youtube is giving me back live videos, which I can't currently deal with. Try a different search string or give me a url."
            )
            raise ValueError("Live stream")
        return cls(
            ctx, discord.FFmpegPCMAudio(data["url"], **FFMPEG_OPTIONS), data=data
        )

    def embed(self, *, state):
        """Create an embed with state in {'playing', 'queued'}"""
        if state not in ("playing", "queued"):
            raise ValueError

        return (
            discord.Embed(
                description=f"Now {state}: [{self.title}]({self.url})",
                timestamp=datetime.datetime.utcnow(),
                color=EMBED_COLOR,
            )
            .add_field(name="Duration:", value=self.duration_str)
            .add_field(name="Requested by:", value=self.requester.mention)
            .set_thumbnail(url=self.thumbnail)
            .set_footer(text=bot.user.display_name, icon_url=bot.user.avatar_url)
        )

    @property
    def duration_str(self):
        minutes, seconds = divmod(self.duration.seconds, 60)
        hours, minutes = divmod(minutes, 60)

        duration = []

        if hours > 0:
            duration.append(f"{hours}".rjust(2, "0"))
        if minutes > 0:
            duration.append(f"{minutes}".rjust(2, "0"))
        if seconds > 0:
            duration.append(f"{seconds}".rjust(2, "0"))

        return ":".join(duration)


class Player:
    def __init__(self, bot: commands.Bot, ctx: commands.Context):
        self.bot = bot
        self.ctx = ctx

        self.current = None
        self.queue = SongQueue()
        self.next = asyncio.Event()
        self.voice = ctx.voice_client

        self.task = self.bot.loop.create_task(self.play())

    async def play(self):
        while True:
            self.next.clear()
            try:
                self.current = await asyncio.wait_for(self.queue.get(), timeout=300)
            except asyncio.TimeoutError:
                await self.voice.disconnect()
                await self.stop()
                return
            await self.current.channel.send(embed=self.current.embed(state="playing"))
            self.voice.play(self.current, after=lambda e: self.next.set())
            await self.next.wait()

    async def stop(self):
        self.queue.clear()
        if self.voice.is_playing():
            self.voice.stop()

    def skip(self):
        self.current = None
        if self.voice.is_playing():
            self.voice.stop()

    async def add(self, song: Song, ctx: commands.Context):
        await self.queue.put(song)
        if ctx.voice_client is not None and ctx.voice_client.is_playing():
            await ctx.reply(embed=song.embed(state="queued"))

    def __del__(self):
        self.task.cancel()


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players = {}

    @commands.command()
    async def join(self, ctx: commands.Context):
        """Join the current voice channel."""
        if ctx.author.voice:
            if ctx.voice_client is None:
                await ctx.author.voice.channel.connect()
            elif ctx.voice_client.channel != ctx.author.voice.channel:
                await ctx.voice_client.move_to(ctx.author.voice.channel)
        else:
            await ctx.reply("You are not connected to a voice channel.")
            raise commands.CommandError("Author not connected to a voice channel.")

    @commands.command()
    async def play(self, ctx: commands.Context, *, search: str):
        """Joins your voice channel and plays from a search string (almost anything youtube_dl supports)."""

        print(f'{ctx.author} requested a song with string "{search}".')

        song = await Song.from_search(ctx, search, loop=self.bot.loop, stream=True)
        await ctx.invoke(self.join)
        player = self.player(ctx)
        if not player:
            self.players[ctx.guild.id] = Player(bot, ctx)
        await self.players[ctx.guild.id].add(song, ctx)

    @commands.command()
    async def stop(self, ctx: commands.Context):
        """Stops, clears the queue, and disconnects the bot from voice"""
        print(f"{ctx.author} stopped.")
        if player := self.players.get(ctx.guild.id):
            await player.stop()
            del self.players[ctx.guild.id]
        await ctx.message.add_reaction("✅")
        if ctx.voice_client:
            await ctx.voice_client.disconnect()

    @commands.command()
    async def skip(self, ctx: commands.Context):
        """Skips the song at the front of the queue."""
        print(f"{ctx.author} skipped.")
        self.player(ctx).skip()
        await ctx.message.add_reaction("✅")

    @commands.command()
    async def shuffle(self, ctx: commands.Context):
        """Shuffles the queue."""
        player = self.player(ctx)
        player.queue.shuffle()
        await ctx.message.add_reaction("✅")

    @commands.command()
    async def clear(self, ctx: commands.Context):
        """Clears the queue."""
        player = self.player(ctx)
        player.queue.clear()

    @commands.command()
    async def remove(self, ctx: commands.Context, idx: int):
        """Removes the given number from the queue."""
        player = self.player(ctx)
        player.queue.remove(idx - 1)
        await ctx.message.add_reaction("✅")

    @commands.command()
    async def queue(self, ctx: commands.Context, page: int = 1):
        """Shows the current queue."""
        print(f"{ctx.author} requested the queue.")

        player = self.player(ctx)
        embed = discord.Embed(
            description=f"Current queue:",
            timestamp=datetime.datetime.utcnow(),
            color=EMBED_COLOR,
        )
        if not player:
            return await ctx.reply(embed=embed)

        if player.current:
            embed = player.current.embed(state="playing")

        if len(player.queue) == 0:
            return await ctx.reply(embed=embed)

        items_per_page = 10
        pages = math.ceil(player.queue.qsize() / items_per_page)

        start = (page - 1) * items_per_page
        end = start + items_per_page

        queue_str = ""
        for i, song in enumerate(player.queue[start:end], start=1):
            queue_str += f"#{i}: [{song.title}]({song.url}) `{song.duration}`\n"

        embed.add_field(name="Up next:", value=queue_str, inline=False)
        embed.set_footer(text=f"Page {page}/{pages}", icon_url=self.bot.user.avatar_url)
        await ctx.reply(embed=embed)

    def player(self, ctx: commands.Context):
        return self.players.get(ctx.guild.id)


bot = commands.Bot(command_prefix=commands.when_mentioned_or("!"))
bot.add_cog(Music(bot))


@bot.check
async def message_check(ctx: commands.Context):
    return ctx.channel.name == "orpheus" and ctx.message.guild is not None


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


@bot.event
async def on_command_error(ctx: commands.Context, exception: Exception):
    await ctx.author.send(f"I ran into an exception!\n```{exception}```")


if __name__ == "__main__":
    bot.run(ORPHEUS_DISCORD_TOKEN)
