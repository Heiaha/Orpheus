import asyncio
import logging
import math
import os
import random
from urllib.parse import urlparse, parse_qs

import discord
from discord.ext import commands
from dotenv import load_dotenv
from yt_dlp import YoutubeDL

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("orpheus")

load_dotenv()

# Constants
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
IDLE_TIMEOUT = 10  # seconds

ytdl = YoutubeDL(YTDL_OPTS)


# Utility functions
def fmt_duration(seconds: int) -> str:
    """Format seconds into HH:MM:SS or MM:SS."""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def clean_yt_watch_url(url: str) -> str:
    """Strip playlist parameters from YouTube watch URLs."""
    if "youtube.com/watch" in url:
        q = parse_qs(urlparse(url).query)
        v = q.get("v", [None])[0]
        if v:
            return f"https://www.youtube.com/watch?v={v}"
    return url


class Song:
    """Represents a song with metadata and audio source."""

    __slots__ = ("title", "url", "thumbnail", "duration", "requester", "channel", "source")

    def __init__(self, ctx: commands.Context, data: dict, src: discord.AudioSource):
        self.title = data.get("title")
        self.url = data.get("webpage_url")
        self.thumbnail = data.get("thumbnail")
        self.duration = int(data.get("duration") or 0)
        self.requester = ctx.author
        self.channel = ctx.channel
        self.source = src

    @classmethod
    async def from_search(cls, ctx: commands.Context, query: str) -> "Song":
        """Create a Song from a search query or URL."""
        query = clean_yt_watch_url(query)
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=False))

        if not info:
            raise commands.CommandError(f"No results for `{query}`")

        # Handle playlist results
        data = info if "entries" not in info else next((e for e in info["entries"] if e), None)

        if not data:
            raise commands.CommandError(f"No valid entries found for `{query}`")

        if data.get("is_live"):
            raise commands.CommandError("Live streams are not supported.")

        src = await discord.FFmpegOpusAudio.from_probe(
            data["url"], before_options=FFMPEG_BEFORE, options="-vn"
        )
        return cls(ctx, data, src)

    def create_embed(self, state: str, bot_user: discord.User | None = None) -> discord.Embed:
        """Create a Discord embed for this song."""
        embed = discord.Embed(
            description=f"Now {state}: [{self.title}]({self.url})",
            timestamp=discord.utils.utcnow(),
            color=EMBED_COLOR,
        )
        embed.add_field(name="Duration", value=fmt_duration(self.duration))
        embed.add_field(name="Requested by", value=self.requester.mention)
        embed.set_thumbnail(url=self.thumbnail)

        if bot_user and bot_user.avatar:
            embed.set_footer(text=bot_user.display_name, icon_url=bot_user.avatar.url)

        return embed


class Player:
    """Manages playback for a guild."""

    def __init__(self, ctx: commands.Context):
        self.bot = ctx.bot
        self.guild = ctx.guild
        self.voice_client: discord.VoiceClient | None = ctx.voice_client
        self.queue: asyncio.Queue[Song] = asyncio.Queue()
        self.current: Song | None = None
        self._next = asyncio.Event()
        self.task: asyncio.Task | None = None

    def start(self):
        """Start the playback loop."""
        self.task = self.bot.loop.create_task(self._playback_loop())

    async def _playback_loop(self):
        """Main playback loop that processes the queue."""
        try:
            while True:
                self._next.clear()

                # Wait for next song or timeout
                try:
                    self.current = await asyncio.wait_for(self.queue.get(), timeout=IDLE_TIMEOUT)
                except asyncio.TimeoutError:
                    await self._disconnect_idle()
                    return

                # Announce song
                await self.current.channel.send(
                    embed=self.current.create_embed("playing", self.bot.user)
                )

                # Check voice connection
                if not self.voice_client or not self.voice_client.is_connected():
                    logger.warning(f"Lost voice connection in {self.guild.name}")
                    return

                # Play song
                self.voice_client.play(self.current.source, after=self._after_playback)
                await self._next.wait()
        finally:
            self._cleanup()

    def _after_playback(self, err: Exception | None):
        """Callback after song finishes playing."""
        if err:
            logger.error(f"Playback error: {err}")
        self.current = None
        self.bot.loop.call_soon_threadsafe(self._next.set)

    async def _disconnect_idle(self):
        """Disconnect due to inactivity."""
        if self.voice_client and self.voice_client.is_connected():
            await self.voice_client.disconnect(force=True)
            logger.info(f"Disconnected from {self.guild.name} due to inactivity")

    def _cleanup(self):
        """Clean up player state."""
        self.current = None
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except asyncio.QueueEmpty:
                break

    async def add(self, song: Song):
        """Add a song to the queue."""
        await self.queue.put(song)

    def skip(self):
        """Skip the current song."""
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()

    def stop(self):
        """Stop playback and clear current song."""
        self.skip()
        self.current = None

    def cancel(self):
        """Cancel the player and stop all playback."""
        self.stop()
        if self.task and not self.task.done():
            self.task.cancel()

    def get_queue_snapshot(self) -> list[Song]:
        """Get a snapshot of the queue without modifying it."""
        items = []
        try:
            while True:
                items.append(self.queue.get_nowait())
        except asyncio.QueueEmpty:
            pass
        finally:
            for song in items:
                self.queue.put_nowait(song)
        return items

    def clear_queue(self):
        """Remove all songs from the queue."""
        try:
            while True:
                self.queue.get_nowait()
                self.queue.task_done()
        except asyncio.QueueEmpty:
            pass

    def shuffle_queue(self):
        """Shuffle the queue."""
        items = self.get_queue_snapshot()
        self.clear_queue()
        random.shuffle(items)
        for song in items:
            self.queue.put_nowait(song)

    def remove_from_queue(self, idx: int) -> bool:
        """Remove a song from the queue by index (1-based)."""
        items = self.get_queue_snapshot()
        self.clear_queue()

        if 1 <= idx <= len(items):
            del items[idx - 1]
            for song in items:
                self.queue.put_nowait(song)
            return True

        for song in items:
            self.queue.put_nowait(song)
        return False


class Music(commands.Cog):
    """Music playback commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: dict[int, Player] = {}

    async def ensure_voice(self, ctx: commands.Context):
        """Ensure bot is in the same voice channel as the user."""
        if not ctx.author.voice:
            raise commands.CommandError("Connect to a voice channel first.")

        if ctx.voice_client is None:
            await ctx.author.voice.channel.connect()
        elif ctx.voice_client.channel != ctx.author.voice.channel:
            await ctx.voice_client.move_to(ctx.author.voice.channel)

        # Update player's voice client reference
        player = self.players.setdefault(ctx.guild.id, Player(ctx))
        player.voice_client = ctx.voice_client

    def get_or_create_player(self, ctx: commands.Context) -> Player:
        """Get existing player or create new one."""
        player = self.players.setdefault(ctx.guild.id, Player(ctx))
        player.voice_client = ctx.voice_client
        return player

    @commands.command()
    async def join(self, ctx: commands.Context):
        """Join your voice channel."""
        logger.info(f"{ctx.author.name} used join in {ctx.guild.name}")
        await self.ensure_voice(ctx)
        await ctx.message.add_reaction("✅")

    @commands.command()
    async def play(self, ctx: commands.Context, *, query: str):
        """Play a song from YouTube or add it to the queue."""
        logger.info(f"{ctx.author.name} used play: {query!r}")
        await self.ensure_voice(ctx)

        song = await Song.from_search(ctx, query)
        player = self.get_or_create_player(ctx)
        await player.add(song)

        if not player.task or player.task.done():
            player.start()

        # Show queued message if something is already playing
        if ctx.voice_client and ctx.voice_client.is_playing():
            await ctx.reply(embed=song.create_embed("queued", self.bot.user))

    @commands.command()
    async def pause(self, ctx: commands.Context):
        """Pause the current song."""
        logger.info(f"{ctx.author.name} used pause")
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.pause()
            await ctx.message.add_reaction("⏸️")

    @commands.command()
    async def resume(self, ctx: commands.Context):
        """Resume the paused song."""
        logger.info(f"{ctx.author.name} used resume")
        if ctx.voice_client and ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            await ctx.message.add_reaction("▶️")

    @commands.command()
    async def skip(self, ctx: commands.Context):
        """Skip the current song."""
        logger.info(f"{ctx.author.name} used skip")
        player = self.players.get(ctx.guild.id)
        if player:
            player.skip()
        await ctx.message.add_reaction("⏭️")

    @commands.command()
    async def stop(self, ctx: commands.Context):
        """Stop playback and disconnect."""
        logger.info(f"{ctx.author.name} used stop")
        player = self.players.pop(ctx.guild.id, None)
        if player:
            player.cancel()
        if ctx.voice_client:
            await ctx.voice_client.disconnect(force=False)
        await ctx.message.add_reaction("✅")

    @commands.command(name="queue")
    async def show_queue(self, ctx: commands.Context, page: int = 1):
        """Show the current queue."""
        logger.info(f"{ctx.author.name} used queue")
        player = self.players.get(ctx.guild.id)

        # Base embed
        embed = discord.Embed(
            description="Current queue:",
            timestamp=discord.utils.utcnow(),
            color=EMBED_COLOR,
        )

        # Show current song if playing
        if player and player.current:
            embed = player.current.create_embed("playing", self.bot.user)

        # Get queue items
        items = player.get_queue_snapshot() if player else []

        if not items:
            await ctx.reply(embed=embed)
            return

        # Paginate queue
        per_page = 10
        pages = max(1, math.ceil(len(items) / per_page))
        page = max(1, min(page, pages))
        start = (page - 1) * per_page
        end = start + per_page

        lines = [
            f"#{i + 1}: [{s.title}]({s.url}) `{fmt_duration(s.duration)}`"
            for i, s in enumerate(items[start:end], start=start)
        ]

        embed.add_field(name="Up next", value="\n".join(lines), inline=False)

        footer_text = f"Page {page}/{pages}"
        if self.bot.user and self.bot.user.avatar:
            embed.set_footer(text=footer_text, icon_url=self.bot.user.avatar.url)
        else:
            embed.set_footer(text=footer_text)

        await ctx.reply(embed=embed)

    @commands.command()
    async def clear(self, ctx: commands.Context):
        """Clear the queue."""
        logger.info(f"{ctx.author.name} used clear")
        player = self.players.get(ctx.guild.id)
        if player:
            player.clear_queue()
        await ctx.message.add_reaction("✅")

    @commands.command()
    async def shuffle(self, ctx: commands.Context):
        """Shuffle the queue."""
        logger.info(f"{ctx.author.name} used shuffle")
        player = self.players.get(ctx.guild.id)
        if player:
            player.shuffle_queue()
        await ctx.message.add_reaction("✅")

    @commands.command()
    async def remove(self, ctx: commands.Context, idx: int):
        """Remove a song from the queue by position."""
        logger.info(f"{ctx.author.name} used remove")
        if idx < 1:
            raise commands.CommandError("Index must be >= 1.")

        player = self.players.get(ctx.guild.id)
        if player:
            player.remove_from_queue(idx)
        await ctx.message.add_reaction("✅")


def main():
    """Initialize and run the bot."""
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise ValueError("DISCORD_TOKEN not found in environment")

    intents = discord.Intents.default()
    intents.message_content = True
    intents.voice_states = True

    bot = commands.Bot(
        intents=intents,
        command_prefix=commands.when_mentioned_or("!")
    )

    @bot.check
    async def message_check(ctx: commands.Context):
        """Only respond in #orpheus channel."""
        return ctx.channel.name == "orpheus" and ctx.message.guild is not None

    @bot.event
    async def setup_hook():
        """Setup hook to add cogs."""
        await bot.add_cog(Music(bot))

    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()