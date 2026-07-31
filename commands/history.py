import discord
from discord import app_commands
from discord.ext import commands

from database.database import get_recent_history
from utils.time_utils import format_display_datetime


class History(commands.Cog):

    def __init__(self, bot):

        self.bot = bot


    @app_commands.command(
        name="history",
        description="Show recent Game Night history"
    )
    async def history(
        self,
        interaction: discord.Interaction
    ):

        history = await get_recent_history(10)


        if not history:

            await interaction.response.send_message(
                "📜 No games have been locked in yet."
            )

            return



        embed = discord.Embed(
            title="📜 Game Night History",
            colour=discord.Colour.gold()
        )


        for game in history:

            game_name = game[0]
            played_date = game[1]
            locked_by = game[2]


            date = format_display_datetime(
                played_date,
                date_only=True,
                fallback=str(played_date).split("T")[0],
            )


            embed.add_field(
                name=f"🎮 {game_name}",
                value=(
                    f"🗓️ {date}\n"
                    f"🔒 Locked in by **{locked_by}**"
                ),
                inline=False
            )


        embed.set_footer(
            text="Showing last 10 games"
        )


        await interaction.response.send_message(
            embed=embed
        )



async def setup(bot):

    await bot.add_cog(
        History(bot)
    )
