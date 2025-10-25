import asyncio
import math
import os
import random
from urllib.parse import urlparse, parse_qs

import discord
from discord.ext import commands
from dotenv import load_dotenv
from yt_dlp import YoutubeDL

load_dotenv()

YTDL_OPTS = {
    "format": "bestaudio/best",
    "restrictfilenames": True,
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "source_address": "0.0.0.0",
}
FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
EMBED_COLOR = 0xA84300
IDLE_TIMEOUT = 300  # seconds

ytdl = YoutubeDL(YTDL_OPTS)


def fmt_duration(seconds: int) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"



def clean_yt_watch_url(url: str) -> str:
    """Strip playlist params from YouTube watch URLs."""
    if "youtube.com/watch" in url:
        q = parse_qs(urlparse(url).query)
        v = q.get("v", [None])[0]
        if v:
            return f"https://www.youtube.com/watch?v={v}"
    return url



class Song:
    __slots__ = ("title", "url", "thumbnail", "duration", "requester", "channel", "source", "ctx")

    def __init__(self, ctx: commands.Context, data: dict, src: discord.AudioSource):
        self.ctx = ctx
        self.title = data.get("title")
        self.url = data.get("webpage_url")
        self.thumbnail = data.get("thumbnail")
        self.duration = int(data.get("duration") or 0)
        self.requester = ctx.author
        self.channel = ctx.channel
        self.source = src

    @classmethod
    async def from_search(cls, ctx: commands.Context, query: str) -> "Song":
        query = clean_yt_watch_url(query)
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=False))
        if not info:
            raise commands.CommandError(f"No results for `{query}`")

        data = info.get("entries") or info
        if isinstance(data, dict) and data.get("entries"):
            data = next((e for e in data["entries"] if e), None)
        if not data:
            raise commands.CommandError(f"No playable result for `{query}`")

        if data.get("is_live"):
            raise commands.CommandError("Live streams are not supported.")

        src = await discord.FFmpegOpusAudio.from_probe(
            data["url"], before_options=FFMPEG_BEFORE, options="-vn"
        )
        return cls(ctx, data, src)

    def embed(self, state: str) -> discord.Embed:
        e = (
            discord.Embed(
                description=f"Now {state}: [{self.title}]({self.url})",
                timestamp=discord.utils.utcnow(),
                color=EMBED_COLOR,
            )
            .add_field(name="Duration", value=fmt_duration(self.duration))
            .add_field(name="Requested by", value=self.requester.mention)
            .set_thumbnail(url=self.thumbnail)
        )
        bot_user = self.ctx.bot.user
        if bot_user and bot_user.avatar:
            e.set_footer(text=bot_user.display_name, icon_url=bot_user.avatar.url)
        return e


class Player:
    def __init__(self, ctx: commands.Context):
        self.bot = ctx.bot
        self.guild_id = ctx.guild.id
        self.voice_client: discord.VoiceClient | None = ctx.voice_client
        self.queue: asyncio.Queue[Song] = asyncio.Queue()
        self.current: Song | None = None
        self._next = asyncio.Event()
        self.task = self.bot.loop.create_task(self._runner())

    async def _runner(self):
        try:
            while True:
                self._next.clear()
                try:
                    self.current = await asyncio.wait_for(self.queue.get(), timeout=IDLE_TIMEOUT)
                except asyncio.TimeoutError:
                    if self.voice_client and self.voice_client.is_connected():
                        await self.voice_client.disconnect(force=True)
                    return

                await self.current.channel.send(embed=self.current.embed("playing"))

                def _after(err: Exception | None):
                    if err:
                        print(f"player error: {err}")
                    self.current = None
                    self.bot.loop.call_soon_threadsafe(self._next.set)

                if not self.voice_client or not self.voice_client.is_connected():
                    # voice lost mid-loop; bail
                    return

                self.voice_client.play(self.current.source, after=_after)
                await self._next.wait()
        finally:
            self.current = None
            # drain leftover to avoid pending awaits
            while not self.queue.empty():
                try:
                    self.queue.get_nowait()
                    self.queue.task_done()
                except Exception:
                    break

    async def add(self, song: Song):
        await self.queue.put(song)

    def stop(self):
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()
        self.current = None

    def skip(self):
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()
        self.current = None

    def cancel(self):
        self.stop()
        if not self.task.done():
            self.task.cancel()
        self.current = None


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: dict[int, Player] = {}

    async def ensure_voice(self, ctx: commands.Context):
        if not ctx.author.voice:
            raise commands.CommandError("Connect to a voice channel.")
        if ctx.voice_client is None:
            await ctx.author.voice.channel.connect()
        elif ctx.voice_client.channel != ctx.author.voice.channel:
            await ctx.voice_client.move_to(ctx.author.voice.channel)
        # refresh stored voice on player
        player = self.players.setdefault(ctx.guild.id, Player(ctx))
        player.voice_client = ctx.voice_client

    @commands.command()
    async def join(self, ctx: commands.Context):
        await self.ensure_voice(ctx)
        await ctx.message.add_reaction("✅")

    @commands.command()
    async def play(self, ctx: commands.Context, *, query: str):
        print(f'{ctx.author} requested "{query}"')
        await self.ensure_voice(ctx)
        song = await Song.from_search(ctx, query)
        player = self.players.setdefault(ctx.guild.id, Player(ctx))
        await player.add(song)
        if ctx.voice_client and ctx.voice_client.is_playing():
            await ctx.reply(embed=song.embed("queued"))

    @commands.command()
    async def pause(self, ctx: commands.Context):
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.pause()
            await ctx.message.add_reaction("⏸️")

    @commands.command()
    async def resume(self, ctx: commands.Context):
        if ctx.voice_client and ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            await ctx.message.add_reaction("▶️")

    @commands.command()
    async def skip(self, ctx: commands.Context):
        player = self.players.get(ctx.guild.id)
        if player:
            player.skip()
        await ctx.message.add_reaction("⏭️")

    @commands.command()
    async def stop(self, ctx: commands.Context):
        player = self.players.pop(ctx.guild.id, None)
        if player:
            player.cancel()
        if ctx.voice_client:
            await ctx.voice_client.disconnect(force=False)
        await ctx.message.add_reaction("✅")

    @commands.command(name="queue")
    async def _queue(self, ctx: commands.Context, page: int = 1):
        player = self.players.get(ctx.guild.id)
        embed = discord.Embed(
            description="Current queue:",
            timestamp=discord.utils.utcnow(),
            color=EMBED_COLOR,
        )
        if not player:
            await ctx.reply(embed=embed)
            return

        if player.current:
            embed = player.current.embed("playing")

        # snapshot queue contents without poking internals
        items: list[Song] = []
        try:
            # non-blocking drain-copy
            while True:
                s = player.queue.get_nowait()
                items.append(s)
        except asyncio.QueueEmpty:
            pass
        finally:
            # push back
            for s in items:
                player.queue.put_nowait(s)

        if not items:
            await ctx.reply(embed=embed)
            return

        per_page = 10
        pages = max(1, math.ceil(len(items) / per_page))
        page = max(1, min(page, pages))
        start = (page - 1) * per_page
        lines = [
            f"#{i+1}: [{s.title}]({s.url}) `{fmt_duration(s.duration)}`"
            for i, s in enumerate(items[start : start + per_page], start=start)
        ]
        embed.add_field(name="Up next", value="\n".join(lines), inline=False)
        if self.bot.user and self.bot.user.avatar:
            embed.set_footer(text=f"Page {page}/{pages}", icon_url=self.bot.user.avatar.url)
        else:
            embed.set_footer(text=f"Page {page}/{pages}")
        await ctx.reply(embed=embed)

    @commands.command()
    async def clear(self, ctx: commands.Context):
        p = self.players.get(ctx.guild.id)
        if not p:
            await ctx.message.add_reaction("✅")
            return
        # drain queue
        try:
            while True:
                p.queue.get_nowait()
                p.queue.task_done()
        except asyncio.QueueEmpty:
            pass
        await ctx.message.add_reaction("✅")

    @commands.command()
    async def shuffle(self, ctx: commands.Context):
        player = self.players.get(ctx.guild.id)
        if not player:
            await ctx.message.add_reaction("✅")
            return
        items = []
        try:
            while True:
                items.append(player.queue.get_nowait())
        except asyncio.QueueEmpty:
            pass
        random.shuffle(items)
        for s in items:
            player.queue.put_nowait(s)
        await ctx.message.add_reaction("✅")

    @commands.command()
    async def remove(self, ctx: commands.Context, idx: int):
        if idx < 1:
            raise commands.CommandError("Index must be >= 1.")
        p = self.players.get(ctx.guild.id)
        if not p:
            await ctx.message.add_reaction("✅")
            return
        items = []
        try:
            while True:
                items.append(p.queue.get_nowait())
        except asyncio.QueueEmpty:
            pass
        if 1 <= idx <= len(items):
            del items[idx - 1]
        for s in items:
            p.queue.put_nowait(s)
        await ctx.message.add_reaction("✅")


def main():
    token = os.getenv("DISCORD_TOKEN")
    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(intents=intents, command_prefix=commands.when_mentioned_or("!"))

    @bot.check
    async def message_check(ctx: commands.Context):
        return ctx.channel.name == "orpheus" and ctx.message.guild is not None

    @bot.event
    async def setup_hook():
        await bot.add_cog(Music(bot))

    bot.run(token)


if __name__ == "__main__":
    main()
