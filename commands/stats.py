import discord
from discord import app_commands
from discord.ext import commands

from database.database import get_stats
from utils.time_utils import format_display_datetime



class Stats(commands.Cog):

    def __init__(self, bot):

        self.bot = bot



    @app_commands.command(
        name="stats",
        description="Show Game Night statistics"
    )
    async def stats(
        self,
        interaction: discord.Interaction
    ):


        data = await get_stats()


        embed = discord.Embed(
            title="📊 Game Night Stats",
            colour=discord.Colour.green()
        )


        embed.add_field(
            name="👥 Multiplayer Wheel",
            value=str(data["total_games"]),
            inline=True
        )

        embed.add_field(
            name="🧍 Single Player Wheel",
            value=str(data["singleplayer_games"]),
            inline=True
        )

        embed.add_field(
            name="🌠 Wishlist",
            value=str(data["wishlist_games"]),
            inline=True
        )


        embed.add_field(
            name="🆕 Never Played",
            value=str(data["never_played"]),
            inline=True
        )


        if data["most_played"]:

            embed.add_field(
                name="🏆 Most Played",
                value=(
                    f"**{data['most_played'][0]}**\n"
                    f"{data['most_played'][1]} plays"
                ),
                inline=False
            )


        if data["last_played"]:

            last_played_time = format_display_datetime(
                data["last_played"][1]
            )

            embed.add_field(
                name="🔥 Last Played",
                value=(
                    f"**{data['last_played'][0]}**\n"
                    f"{last_played_time}"
                ),
                inline=False
            )


        if data["top_suggester"]:

            embed.add_field(
                name="👥 Top Suggestor",
                value=(
                    f"**{data['top_suggester'][0]}**\n"
                    f"{data['top_suggester'][1]} games added"
                ),
                inline=False
            )


        await interaction.response.send_message(
            embed=embed
        )



async def setup(bot):

    await bot.add_cog(
        Stats(bot)
    )
