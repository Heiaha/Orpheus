import asyncio
import collections
import logging
import math
import os
import random
import subprocess
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
    "noplaylist": True,
    "nocheckcertificate": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "source_address": "0.0.0.0",
}
EMBED_COLOR = 0xA84300
IDLE_TIMEOUT = 600  # seconds

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


def apply_footer(embed: discord.Embed, text: str, user: discord.User | None) -> discord.Embed:
    """Set an embed footer, with the user's avatar when available."""
    embed.set_footer(text=text, icon_url=user.avatar.url if user and user.avatar else None)
    return embed


class YTDLOpusAudio(discord.FFmpegOpusAudio):
    """FFmpegOpusAudio fed by a yt-dlp downloader subprocess.

    YouTube serves a single long-lived GET (what ffmpeg does with a bare URL)
    below realtime, but serves ranged chunks at full speed, so yt-dlp does the
    downloading and pipes the bytes to ffmpeg.
    """

    def __init__(self, data: dict, query: str):
        self.proc = subprocess.Popen(
            [
                "yt-dlp", "-q", "--no-warnings", "--no-playlist", "--no-cache-dir",
                "-f", f"{data.get('format_id') or 'bestaudio'}/bestaudio/best",
                "--http-chunk-size", "10M",
                "-o", "-",
                data.get("webpage_url") or query,
            ],
            stdout=subprocess.PIPE,
        )
        codec = "copy" if (data.get("acodec") or "").startswith("opus") else "libopus"
        try:
            super().__init__(self.proc.stdout, pipe=True, codec=codec, options="-vn")
        except Exception:
            self.proc.kill()
            self.proc.wait()
            raise

    def cleanup(self) -> None:
        """Stop the downloader along with ffmpeg."""
        self.proc.kill()
        self.proc.wait()
        super().cleanup()


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

    def destroy(self):
        """Release the audio pipeline of a song that will never be played."""
        self.source.cleanup()

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

        src = YTDLOpusAudio(data, query)
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

        if bot_user:
            apply_footer(embed, bot_user.display_name, bot_user)

        return embed


class Player:
    """Manages playback for a guild or group DM."""

    def __init__(self, ctx: commands.Context):
        self.bot = ctx.bot
        self.guild = ctx.guild
        self.voice_client: discord.VoiceClient | None = ctx.voice_client
        self.songs: collections.deque[Song] = collections.deque()
        self.current: Song | None = None
        self._song_added = asyncio.Event()
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
                while not self.songs:
                    self._song_added.clear()
                    try:
                        await asyncio.wait_for(self._song_added.wait(), timeout=IDLE_TIMEOUT)
                    except asyncio.TimeoutError:
                        await self._disconnect_idle()
                        return
                self.current = self.songs.popleft()

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
        if self.current:
            self.current.destroy()
        self.current = None
        self.clear_queue()

    def add(self, song: Song):
        """Add a song to the queue."""
        self.songs.append(song)
        self._song_added.set()

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

    def clear_queue(self):
        """Remove all songs from the queue and stop their downloads."""
        while self.songs:
            self.songs.popleft().destroy()

    def shuffle_queue(self):
        """Shuffle the queue."""
        random.shuffle(self.songs)

    def remove_from_queue(self, idx: int) -> bool:
        """Remove a song from the queue by index (1-based)."""
        if not 1 <= idx <= len(self.songs):
            return False
        song = self.songs[idx - 1]
        del self.songs[idx - 1]
        song.destroy()
        return True


class Music(commands.Cog):
    """Music playback commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: dict[int, Player] = {}

    def _get_player_key(self, ctx: commands.Context) -> int:
        """Get the player key for the current context (guild or channel ID)."""
        return ctx.guild.id if ctx.guild else ctx.channel.id

    async def ensure_voice(self, ctx: commands.Context) -> Player:
        """Ensure bot is in the same voice channel as the user and return the player."""
        if not ctx.author.voice:
            raise commands.CommandError("Connect to a voice channel first.")

        if ctx.voice_client is None:
            await ctx.author.voice.channel.connect()
        elif ctx.voice_client.channel != ctx.author.voice.channel:
            await ctx.voice_client.move_to(ctx.author.voice.channel)

        player = self.players.setdefault(self._get_player_key(ctx), Player(ctx))
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
        player = await self.ensure_voice(ctx)

        song = await Song.from_search(ctx, query)
        player.add(song)

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
        player = self.players.get(self._get_player_key(ctx))
        if player:
            player.skip()
        await ctx.message.add_reaction("⏭️")

    @commands.command()
    async def stop(self, ctx: commands.Context):
        """Stop playback and disconnect."""
        logger.info(f"{ctx.author.name} used stop")
        player = self.players.pop(self._get_player_key(ctx), None)
        if player:
            player.cancel()
        if ctx.voice_client:
            await ctx.voice_client.disconnect(force=False)
        await ctx.message.add_reaction("✅")

    @commands.command(name="queue")
    async def show_queue(self, ctx: commands.Context, page: int = 1):
        """Show the current queue."""
        logger.info(f"{ctx.author.name} used queue")
        player = self.players.get(self._get_player_key(ctx))

        # Show current song if playing, otherwise a base embed
        if player and player.current:
            embed = player.current.create_embed("playing", self.bot.user)
        else:
            embed = discord.Embed(
                description="Current queue:",
                timestamp=discord.utils.utcnow(),
                color=EMBED_COLOR,
            )

        # Get queue items
        items = list(player.songs) if player else []

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
        apply_footer(embed, f"Page {page}/{pages}", self.bot.user)

        await ctx.reply(embed=embed)

    @commands.command()
    async def clear(self, ctx: commands.Context):
        """Clear the queue."""
        logger.info(f"{ctx.author.name} used clear")
        player = self.players.get(self._get_player_key(ctx))
        if player:
            player.clear_queue()
        await ctx.message.add_reaction("✅")

    @commands.command()
    async def shuffle(self, ctx: commands.Context):
        """Shuffle the queue."""
        logger.info(f"{ctx.author.name} used shuffle")
        player = self.players.get(self._get_player_key(ctx))
        if player:
            player.shuffle_queue()
        await ctx.message.add_reaction("✅")

    @commands.command()
    async def remove(self, ctx: commands.Context, idx: int):
        """Remove a song from the queue by position."""
        logger.info(f"{ctx.author.name} used remove")
        if idx < 1:
            raise commands.CommandError("Index must be >= 1.")

        player = self.players.get(self._get_player_key(ctx))
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
        # Allow in group DMs (private channels with multiple recipients)
        if isinstance(ctx.channel, discord.GroupChannel):
            return True
        # Allow in guild channels named "orpheus"
        return ctx.guild is not None and ctx.channel.name == "orpheus"

    @bot.event
    async def setup_hook():
        """Setup hook to add cogs."""
        await bot.add_cog(Music(bot))

    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()