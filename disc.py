import datetime
from collections import defaultdict

import discord
import youtube_dl
import asyncio
import secrets

from discord.ext import commands, tasks

ytdl_format_options = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0'  # bind to ipv4 since ipv6 addresses cause issues sometimes
}

ytdl = youtube_dl.YoutubeDL(ytdl_format_options)
EMBED_COLOR = 0xa84300


class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, author, volume=0.5):
        super().__init__(source, volume)

        self.title = data.get('title')
        self.video_ = data.get('url')
        self.url = data.get('webpage_url')
        self.thumbnail_url = data.get('thumbnails')[-1].get('url')
        self.duration = datetime.timedelta(seconds=data.get('duration'))
        self.author = author

    @classmethod
    async def from_url(cls, url, *, loop=None, author=None, stream=True):
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream))

        if 'entries' in data:
            # take first item from a playlist
            data = data['entries'][0]
        filename = data['url'] if stream else ytdl.prepare_filename(data)
        return cls(discord.FFmpegPCMAudio(filename, options='-vn'), author=author, data=data)


class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.playlists = defaultdict(list)
        self.curr_songs = {}

    async def on_ready(self):
        self.check_leave.start()

    async def _play(self, ctx):
        if ctx.voice_client.is_playing() or ctx.voice_client.is_paused():
            return

        while ctx.voice_client is not None and (
                self.playlists[ctx.guild.id] or ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
            if not ctx.voice_client.is_playing() and not ctx.voice_client.is_paused():
                curr_song = self.playlists[ctx.guild.id].pop(0)
                self.curr_songs[ctx.guild.id] = curr_song
                ctx.voice_client.play(curr_song, after=lambda e: print(f'Player error: {e}') if e else None)

                embed = discord.Embed(description=f'Now playing: [{curr_song.title}]({curr_song.url})',
                                      timestamp=datetime.datetime.utcnow(),
                                      color=EMBED_COLOR)
                embed.add_field(name='Duration:', value=curr_song.duration)
                embed.add_field(name='Requested by:', value=curr_song.author.mention)
                embed.set_thumbnail(url=curr_song.thumbnail_url)
                embed.set_footer(text=self.bot.user.display_name, icon_url=self.bot.user.avatar_url)

                await ctx.send(embed=embed)
            await asyncio.sleep(1)

        self.curr_songs.pop(ctx.guild.id, None)
        self.playlists.pop(ctx.guild.id, None)

    @commands.command()
    async def play(self, ctx, *, search_str: str=None):
        """Plays from a url (almost anything youtube_dl supports)."""

        print(f'{ctx.author} requested a song with string \"{search_str}\".')
        if ctx.author.voice:
            if ctx.voice_client is None:
                await ctx.author.voice.channel.connect()
        else:
            await ctx.reply("You are not connected to a voice channel.")
            raise commands.CommandError("Author not connected to a voice channel.")

        if ctx.voice_client.is_paused():
            ctx.voice_client.resume()

        if search_str:
            song = await YTDLSource.from_url(search_str, loop=self.bot.loop, author=ctx.author, stream=True)
            self.playlists[ctx.guild.id].append(song)
            if ctx.voice_client is not None and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
                embed = discord.Embed(description=f'Added to queue: [{song.title}]({song.url})',
                                      timestamp=datetime.datetime.utcnow(),
                                      color=EMBED_COLOR)
                embed.add_field(name='Duration:', value=song.duration)
                embed.add_field(name='Requested by:', value=self.curr_songs[ctx.guild.id].author.mention)
                embed.set_thumbnail(url=song.thumbnail_url)
                embed.set_footer(text=self.bot.user.display_name, icon_url=self.bot.user.avatar_url)
                await ctx.send(embed=embed)

            self.bot.loop.create_task(self._play(ctx))

    @commands.command()
    async def pause(self, ctx):
        """Pauses."""
        print(f'{ctx.author} paused.')
        ctx.voice_client.pause()

    @commands.command()
    async def stop(self, ctx):
        """Stops, clears the queue, and disconnects the bot from voice"""
        print(f'{ctx.author} stopped.')
        self.playlists.pop(ctx.guild.id, None)
        self.curr_songs.pop(ctx.guild.id, None)
        ctx.voice_client.stop()
        await ctx.voice_client.disconnect()

    @commands.command()
    async def skip(self, ctx):
        """Skips the song at the front of the queue."""
        print(f'{ctx.author} skipped.')
        ctx.voice_client.stop()
        if len(self.playlists[ctx.guild.id]) == 0:
            await ctx.voice_client.disconnect()

    @commands.command()
    async def queue(self, ctx):
        """Shows the current queue."""
        print(f'{ctx.author} requested the queue.')
        embed = discord.Embed(description='Current queue:',
                              timestamp=datetime.datetime.utcnow(),
                              color=EMBED_COLOR)
        curr_song = self.curr_songs.get(ctx.guild.id)
        if curr_song:
            embed.add_field(name=f'Now {"paused" if ctx.voice_client.is_paused() else "playing"}:',
                            value=f'[{curr_song.title}]({curr_song.url}) `{curr_song.duration}`' if curr_song else None)
            embed.set_thumbnail(url=curr_song.thumbnail_url)
        else:
            embed.set_thumbnail(url=self.bot.user.avatar_url)

        playlist_str = ''
        for i, song in enumerate(self.playlists[ctx.guild.id], start=1):
            if len(playlist_str) <= 1000:
                playlist_str += f'#{i}: [{song.title}]({song.url}) `{song.duration}`\n'
            else:
                break

        embed.add_field(name='Up next:', value=playlist_str if playlist_str else None, inline=False)
        embed.set_footer(text=self.bot.user.display_name, icon_url=self.bot.user.avatar_url)
        await ctx.send(embed=embed)

    @tasks.loop(minutes=5)
    async def check_leave(self):
        for voice_client in self.bot.voice_clients:
            if not voice_client.is_playing() and not voice_client.is_paused():
                await voice_client.disconnect()


bot = commands.Bot(command_prefix=commands.when_mentioned_or("!"))
bot.add_cog(Music(bot))


@bot.check
async def message_check(ctx):
    return ctx.channel.name == 'orpheus' and ctx.message.guild is not None


@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    music_cog = bot.get_cog('Music')
    await music_cog.on_ready()


if __name__ == '__main__':
    bot.run(secrets.DISCORD_TOKEN)
