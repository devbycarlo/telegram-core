import discord
from discord.ext import commands
import os
from dotenv import load_dotenv
import asyncio
import datetime
import re

# Load environment variables from .env file
load_dotenv()

# Set up intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

# Create bot with command prefix
bot = commands.Bot(command_prefix='!', intents=intents)

# Store reminders and tasks
reminders = {}
tasks_list = {}

@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')
    activity = discord.Game(name="!helpme for commands")
    await bot.change_presence(activity=activity)

@bot.event
async def on_message(message):
    # Don't respond to the bot's own messages
    if message.author == bot.user:
        return

    # Message moderation
    await moderate_message(message)
    
    # Process commands
    await bot.process_commands(message)

@bot.event
async def on_member_join(member):
    # Welcome message
    welcome_channel = discord.utils.get(member.guild.text_channels, name='general')
    if welcome_channel:
        await welcome_channel.send(f'Welcome to the server, {member.mention}! 👋')
    
    # Send a DM with info
    try:
        await member.send(f'Welcome to {member.guild.name}! Feel free to ask for help anytime.')
    except:
        pass  # User might have DMs disabled

async def moderate_message(message):
    # Basic moderation - flag inappropriate content
    bad_words = ['badword1', 'badword2']  # Add your own words
    message_content = message.content.lower()
    
    for word in bad_words:
        if word in message_content:
            try:
                await message.delete()
                await message.channel.send(f'{message.author.mention}, please watch your language!')
            except:
                pass
            return
    
    # Spam detection
    if len(message.content.split()) > 50:  # Long spam messages
        try:
            await message.delete()
            await message.channel.send(f'{message.author.mention}, your message was too long.')
        except:
            pass

# Helper functions
def parse_time(time_str):
    """Parse time string like '1h30m', '30m', '2h' etc."""
    pattern = r'(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?'
    match = re.match(pattern, time_str)
    if not match:
        return None
    
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    
    total_seconds = hours * 3600 + minutes * 60 + seconds
    return total_seconds if total_seconds > 0 else None

# Commands
@bot.command(name='ping')
async def ping(ctx):
    latency = round(bot.latency * 1000)
    await ctx.send(f'Pong! 🏓 Latency: {latency}ms')

@bot.command(name='hello')
async def hello(ctx):
    await ctx.send(f'Hello, {ctx.author.display_name}! How can I assist you today?')

@bot.command(name='helpme')
async def helpme(ctx):
    help_text = """
    **📋 Available Commands:**
    `!ping` - Check bot latency
    `!hello` - Greet the bot
    `!remind <time> <message>` - Set a reminder (e.g., `!remind 1h30m Meeting time!`)
    `!task add <task>` - Add a task
    `!task list` - List all tasks
    `!task done <number>` - Mark task as done
    `!alert <channel/user> <message>` - Send alert (admin only)
    `!clear <number>` - Clear messages (admin only)
    """
    await ctx.send(help_text)

@bot.command(name='remind')
async def remind(ctx, time_str, *, reminder):
    """Set a reminder for a specific time"""
    seconds = parse_time(time_str)
    if not seconds:
        await ctx.send("Invalid time format! Use format like `1h30m`, `30m`, `2h`, etc.")
        return
    
    reminder_id = len(reminders) + 1
    reminders[reminder_id] = {
        'user_id': ctx.author.id,
        'channel_id': ctx.channel.id,
        'reminder': reminder,
        'time': seconds
    }
    
    await ctx.send(f"⏰ Reminder set for {time_str}! I'll remind you: '{reminder}'")
    
    # Wait for the specified time
    await asyncio.sleep(seconds)
    
    # Send the reminder in the original channel
    channel = bot.get_channel(ctx.channel.id)
    if channel:
        await channel.send(f"{ctx.author.mention} ⏰ Reminder: {reminder}")
    
    # Also send as DM
    try:
        user = await bot.fetch_user(ctx.author.id)
        await user.send(f"⏰ Reminder: {reminder}")
    except:
        pass
    
    # Remove completed reminder
    reminders.pop(reminder_id, None)

@bot.command(name='task')
async def task(ctx, action, *, task_content=None):
    """Manage personal tasks"""
    user_id = ctx.author.id
    
    if action.lower() == 'add':
        if not task_content:
            await ctx.send("Please specify a task: `!task add <your task>`")
            return
        
        if user_id not in tasks_list:
            tasks_list[user_id] = []
        
        task_id = len(tasks_list[user_id]) + 1
        tasks_list[user_id].append({
            'id': task_id,
            'content': task_content,
            'completed': False,
            'created_at': datetime.datetime.now()
        })
        
        await ctx.send(f"✅ Task added: `{task_content}`")
    
    elif action.lower() == 'list':
        if user_id not in tasks_list or not tasks_list[user_id]:
            await ctx.send("You don't have any tasks. Use `!task add <task>` to add one.")
            return
        
        task_list_msg = "**Your Tasks:**\n"
        for task in tasks_list[user_id]:
            status = "✅" if task['completed'] else "❌"
            task_list_msg += f"{status} #{task['id']} {task['content']}\n"
        
        await ctx.send(task_list_msg)
    
    elif action.lower() == 'done':
        if not task_content:
            await ctx.send("Please specify a task number: `!task done <number>`")
            return
        
        try:
            task_num = int(task_content)
            if user_id in tasks_list:
                for task in tasks_list[user_id]:
                    if task['id'] == task_num:
                        task['completed'] = True
                        await ctx.send(f"✅ Task marked as completed: `{task['content']}`")
                        return
            await ctx.send("Task not found!")
        except ValueError:
            await ctx.send("Please provide a valid task number.")
    
    elif action.lower() == 'clear':
        if user_id in tasks_list:
            tasks_list[user_id].clear()
            await ctx.send("🗑️ All tasks cleared!")
        else:
            await ctx.send("You don't have any tasks to clear.")
    else:
        await ctx.send("Invalid task action. Use: add, list, done, or clear")

@bot.command(name='clear')
@commands.has_permissions(manage_messages=True)
async def clear(ctx, amount=5):
    """Clear recent messages"""
    try:
        deleted = await ctx.channel.purge(limit=amount + 1)  # +1 to exclude the command message
        await ctx.send(f"🧹 Cleared {len(deleted) - 1} messages.")
    except Exception as e:
        await ctx.send("I don't have permission to delete messages.")

@bot.command(name='alert')
@commands.is_owner()
async def alert(ctx, target, *, message):
    """Send an alert to a specific user"""
    try:
        # Try to find user by mention or ID
        if target.startswith('<@') and target.endswith('>'):
            user_id = int(target[2:-1])
        else:
            user_id = int(target)
        
        user = await bot.fetch_user(user_id)
        await user.send(f"🔔 Alert: {message}")
        await ctx.send(f"✅ Alert sent to {user.name}!")
    except:
        await ctx.send("Could not send alert. User might not exist or have DMs disabled.")

@bot.command(name='reminders')
async def list_reminders(ctx):
    """List active reminders for the user"""
    user_reminders = {k: v for k, v in reminders.items() if v['user_id'] == ctx.author.id}
    
    if not user_reminders:
        await ctx.send("You don't have any active reminders.")
        return
    
    reminder_msg = "**Your Active Reminders:**\n"
    for rid, reminder in user_reminders.items():
        # Convert seconds to readable format
        total_seconds = reminder['time']
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        
        time_str = ""
        if hours > 0:
            time_str += f"{hours}h "
        if minutes > 0:
            time_str += f"{minutes}m "
        if seconds > 0 or time_str == "":
            time_str += f"{seconds}s"
        
        reminder_msg += f"#{rid} ⏰ {time_str} - {reminder['reminder']}\n"
    
    await ctx.send(reminder_msg)

# Error handling
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Missing required argument. Check `!helpme` for command usage.")
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have permission to use this command.")
    elif isinstance(error, commands.CommandNotFound):
        await ctx.send("Command not found. Use `!helpme` to see available commands.")

def run_bot():
    # Get token from environment variable (loaded from .env file)
    token = os.getenv('DISCORD_TOKEN')
    if not token:
        print("Error: DISCORD_TOKEN not found in environment variables!")
        print("Please ensure you have a .env file with DISCORD_TOKEN=your_token_here")
        return
    bot.run(token)

if __name__ == '__main__':
    run_bot()