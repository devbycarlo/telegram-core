
 ```python
   # discord_chat_bot.py
   import discord
   from discord.ext import commands
   import os
   from dotenv import load_dotenv

   load_dotenv()
   TOKEN = os.getenv('DISCORD_TOKEN')

   intents = discord.Intents.default()
   intents.message_content = True

   client = commands.Bot(command_prefix="!", intents=intents)

   @client.event
   async def on_ready():
       print(f'{client.user} is online and ready!')
       await client.change_presence(activity=discord.Game(name="Type !help for commands"))

   @client.event
   async def on_message(message):
       # Don't respond to ourselves
       if message.author == client.user:
           return

       msg = message.content.lower()

       # Simple chat responses
       if msg == "hello" or msg == "hi":
           await message.channel.send(f"Hello {message.author.mention}! How can I help you today?")

       if msg == "goodbye" or msg == "bye":
           await message.channel.send(f"Goodbye {message.author.mention}! Have a great day!")

       if "how are you" in msg:
           await message.channel.send("I'm doing well, thanks for asking!")

       if "your name" in msg:
           await message.channel.send("I'm your friendly Discord bot!")

       if "help" in msg and not msg.startswith("!"):
           await message.channel.send("Send me a message and I'll try to respond!")

       await client.process_commands(message)

   @client.command()
   async def ping(ctx):
       await ctx.send("Pong! 🏓")

   @client.command()
   async def help(ctx):
       await ctx.send("Commands:\n!ping - Check if I'm online\n!help - Show this help message\nJust
 say hello, goodbye, or ask me how I am!")

   # Run the bot
   client.run(TOKEN)
 ```