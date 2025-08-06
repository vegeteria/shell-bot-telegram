

import asyncio
import os
import signal
import html
import re
from dotenv import load_dotenv
from telegram import Update, Document
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import BadRequest

# --- Globals ---
load_dotenv()
user_sessions = {}
try:
    AUTHORIZED_USERS = {int(uid) for uid in os.environ.get("AUTHORIZED_USERS", "").split(",")}
except (ValueError, TypeError):
    print("Error: AUTHORIZED_USERS is not set or contains invalid user IDs. Please check your .env file.")
    AUTHORIZED_USERS = set()

# --- Helper Functions ---
def is_authorized(user_id: int) -> bool:
    return user_id in AUTHORIZED_USERS

async def send_and_update_prompt(update: Update, user_id: int):
    if user_id in user_sessions:
        cwd = user_sessions[user_id]['cwd']
        await update.message.reply_text(f"<code>{cwd} $</code>", parse_mode='HTML')

def create_progress_bar(percentage: int) -> str:
    """Creates a text-based progress bar."""
    filled_blocks = round(percentage / 10)
    empty_blocks = 10 - filled_blocks
    bar = "█" * filled_blocks + "░" * empty_blocks
    return f"[{bar}] {percentage}%"

# --- Core Shell Logic ---
async def periodic_flusher(user_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE, interval: int):
    """Periodically flushes the output buffer by editing a message."""
    while True:
        try:
            await asyncio.sleep(interval)
            if user_id not in user_sessions:
                break

            session = user_sessions[user_id]
            async with session['buffer_lock']:
                full_output = "".join(session['output_buffer']).strip()

                if full_output and full_output != session.get('last_message_text'):
                    sanitized_output = html.escape(full_output)
                    if not sanitized_output:
                        continue

                    if session.get('last_message_id'):
                        try:
                            await context.bot.edit_message_text(
                                text=f"<code>{sanitized_output}</code>",
                                chat_id=update.message.chat_id,
                                message_id=session['last_message_id'],
                                parse_mode='HTML'
                            )
                            session['last_message_text'] = full_output
                        except BadRequest as e:
                            if "Message is not modified" not in e.message:
                                session['last_message_id'] = None
                    
                    if not session.get('last_message_id'):
                        sent_message = await update.message.reply_text(f"<code>{sanitized_output}</code>", parse_mode='HTML')
                        session['last_message_id'] = sent_message.message_id
                        session['last_message_text'] = full_output
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Error in periodic flusher for user {user_id}: {e}")

async def start_shell_session(user_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Starts a new persistent shell session for a user."""
    proc = await asyncio.create_subprocess_shell(
        'bash -i', stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        preexec_fn=os.setsid
    )
    user_sessions[user_id] = {
        'proc': proc, 'cwd': os.path.expanduser("~"),
        'lock': asyncio.Lock(), 'output_buffer': [],
        'buffer_lock': asyncio.Lock(), 'last_message_id': None,
        'last_message_text': ''
    }
    session = user_sessions[user_id]
    session['stdout_task'] = asyncio.create_task(read_stream(proc.stdout, user_id, update, context, "stdout"))
    session['stderr_task'] = asyncio.create_task(read_stream(proc.stderr, user_id, update, context, "stderr"))
    session['flusher_task'] = asyncio.create_task(periodic_flusher(user_id, update, context, 30))

    initial_cd_command = f"cd {os.path.expanduser('~')}\n"
    proc.stdin.write(initial_cd_command.encode())
    await proc.stdin.drain()
    await send_and_update_prompt(update, user_id)

async def read_stream(stream, user_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE, stream_name: str):
    """Continuously reads from a stream, handles markers, and buffers output."""
    CWD_MARKER = "---CWD_MARKER---"
    END_OF_COMMAND_MARKER = "---EOC_MARKER---"

    while True:
        try:
            if user_id not in user_sessions: break
            session = user_sessions[user_id]

            chunk = await stream.read(4096)
            if not chunk: break

            output = chunk.decode(errors='ignore')

            if END_OF_COMMAND_MARKER in output:
                pre_marker_output, _, _ = output.partition(END_OF_COMMAND_MARKER)
                async with session['buffer_lock']:
                    if pre_marker_output:
                        session['output_buffer'].append(pre_marker_output)
                    
                    full_output = "".join(session['output_buffer']).strip()
                    sanitized_output = html.escape(full_output.replace(CWD_MARKER, ""))

                    if session.get('last_message_id') and sanitized_output and sanitized_output != session.get('last_message_text'):
                        try:
                            await context.bot.edit_message_text(
                                text=f"<code>{sanitized_output}</code>",
                                chat_id=update.message.chat_id,
                                message_id=session['last_message_id'],
                                parse_mode='HTML'
                            )
                        except BadRequest as e:
                            if "Message is not modified" not in e.message:
                                await update.message.reply_text(f"<code>{sanitized_output}</code>", parse_mode='HTML')
                    elif not session.get('last_message_id') and sanitized_output:
                         await update.message.reply_text(f"<code>{sanitized_output}</code>", parse_mode='HTML')

                    session['output_buffer'].clear()
                    session['last_message_id'] = None
                    session['last_message_text'] = ''

                if session['lock'].locked():
                    session['lock'].release()
                await send_and_update_prompt(update, user_id)
            else:
                output_to_buffer = output
                if CWD_MARKER in output:
                    try:
                        parts = output.split(CWD_MARKER)
                        new_cwd = parts[1].strip().split('\n')[0]
                        if new_cwd:
                            session['cwd'] = new_cwd
                        output_to_buffer = parts[0] + (parts[2] if len(parts) > 2 else "")
                    except (IndexError, KeyError):
                        output_to_buffer = output
                if output_to_buffer:
                    async with session['buffer_lock']:
                        session['output_buffer'].append(output_to_buffer)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Error reading stream for user {user_id}: {e}")
            break

# --- Rclone Progress Bar Logic ---
async def rc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("You are not authorized.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /rc <rclone_command>")
        return

    # Manually expand ~ in arguments
    command_args = [os.path.expanduser(arg) if arg.startswith('~') else arg for arg in context.args]

    # Ensure -P flag is present for progress parsing
    if command_args[0] == 'rclone' and '-P' not in command_args and '--progress' not in command_args:
        command_args.append('-P')
    
    full_command = " ".join(command_args)

    sent_message = await update.message.reply_text(f"Starting: <code>{full_command}</code>", parse_mode='HTML')
    message_id = sent_message.message_id

    proc = await asyncio.create_subprocess_shell(
        full_command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    last_update_text = ""
    
    # Main loop to read output and update message
    while True:
        try:
            # Wait for a new line of output for up to 5 seconds
            line = await asyncio.wait_for(proc.stderr.readline(), timeout=5.0)
            if not line:
                # If stream ends, break the loop
                break
            
            output = line.decode().strip()
            
            # Regex to capture the main progress line from rclone
            match = re.search(
                r'Transferred:\s+(?P<transferred>\d+\.\d+\s+\w+)\s+/\s+(?P<total>\d+\.\d+\s+\w+), (?P<percent>\d+)%, (?P<speed>\d+\.\d+\s+\w+/s), ETA (?P<eta>\S+)',
                output
            )
            
            if match:
                data = match.groupdict()
                percentage = int(data['percent'])
                progress_bar = create_progress_bar(percentage)
                
                # Construct the clean, overwriting message
                text = (
                    f"<b>Transferring...</b>\n"
                    f"<b>Progress:</b> {progress_bar}\n"
                    f"<b>Size:</b> <code>{data['transferred']} / {data['total']}</code>\n"
                    f"<b>Speed:</b> <code>{data['speed']}</code>\n"
                    f"<b>ETA:</b> <code>{data['eta']}</code>"
                )

                # Edit the message only if the text has changed
                if text != last_update_text:
                    try:
                        await context.bot.edit_message_text(
                            text=text,
                            chat_id=update.message.chat_id,
                            message_id=message_id,
                            parse_mode='HTML'
                        )
                        last_update_text = text
                    except BadRequest as e:
                        # Ignore "message is not modified" errors, print others
                        if "Message is not modified" not in e.message:
                            print(f"Error updating message: {e}")
            # We simply ignore lines that aren't progress lines

        except asyncio.TimeoutError:
            # This is expected. If 5 seconds pass with no new output, we just continue.
            pass
        except Exception as e:
            print(f"Error in rc_command loop: {e}")
            break # Exit loop on other errors
        
        # Check if the process has finished after attempting to read a line or after a timeout
        if proc.returncode is not None:
            break

    # Wait for the process to fully finish and collect final output
    await proc.wait()
    stdout, stderr = await proc.communicate()
    final_output = (stdout.decode() + stderr.decode()).strip()

    # Final message after completion
    final_text = f"<b>Transfer complete!</b>\n\n<code>{full_command}</code>"
    if final_output:
        # Add any final, non-progress output (like errors or summary)
        final_text += f"\n\n<b>Final Output:</b>\n<code>{html.escape(final_output)}</code>"

    try:
        await context.bot.edit_message_text(
            text=final_text,
            chat_id=update.message.chat_id,
            message_id=message_id,
            parse_mode='HTML'
        )
    except BadRequest:
        # If editing fails (e.g., message deleted), send a new one
        await update.message.reply_text(final_text, parse_mode='HTML')

# --- Command Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("You are not authorized.")
        return
    if user_id in user_sessions and user_sessions[user_id]['proc'].returncode is None:
        await update.message.reply_text("An interactive shell is already running.")
    else:
        await update.message.reply_text("Starting interactive shell...\nType commands directly. Use /type for interactive prompts, /download, /end, or /controlC.")
        await start_shell_session(user_id, update, context)

async def end_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_authorized(user_id) or user_id not in user_sessions:
        await update.message.reply_text("No shell session is active.")
        return

    session = user_sessions[user_id]
    for task_name in ['stdout_task', 'stderr_task', 'flusher_task']:
        if task_name in session and session[task_name]:
            session[task_name].cancel()

    proc = session['proc']
    proc.terminate()
    await proc.wait()
    del user_sessions[user_id]
    await update.message.reply_text("Shell session ended.")

async def control_c_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_authorized(user_id) or user_id not in user_sessions:
        await update.message.reply_text("No shell session is active.")
        return
    session = user_sessions[user_id]
    try:
        os.killpg(os.getpgid(session['proc'].pid), signal.SIGINT)
        await update.message.reply_text("Interrupt signal (Ctrl+C) sent.")
        if session['lock'].locked():
            session['lock'].release()
        await send_and_update_prompt(update, user_id)
    except ProcessLookupError:
        await update.message.reply_text("Process seems to have already ended.")
    except Exception as e:
        await update.message.reply_text(f"Failed to send signal: {e}")

async def type_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_authorized(user_id) or user_id not in user_sessions:
        await update.message.reply_text("No shell session is active.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /type <text_to_send>")
        return
    session = user_sessions[user_id]
    input_text = " ".join(context.args) + "\n"
    session['proc'].stdin.write(input_text.encode())
    await session['proc'].stdin.drain()
    await update.message.reply_text(f"Typed: {input_text.strip()}")

async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_authorized(user_id):
        return
    if user_id not in user_sessions:
        await update.message.reply_text("No active shell. Use /start to begin.")
        return

    session = user_sessions[user_id]
    if session['lock'].locked():
        await update.message.reply_text("A command is already running. Please wait or use /controlC.")
        return

    await session['lock'].acquire()
    command = update.message.text
    if command.strip().startswith('cd '):
        full_command = f"{command} && echo ---CWD_MARKER--- && pwd && echo ---CWD_MARKER--- && echo ---EOC_MARKER---\n"
    else:
        full_command = f"{command} ; echo ---EOC_MARKER---\n"
    session['proc'].stdin.write(full_command.encode())
    await session['proc'].stdin.drain()

async def download_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_authorized(user_id) or user_id not in user_sessions:
        await update.message.reply_text("You must start a shell session first with /start.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /download <file_path>")
        return
    session = user_sessions[user_id]
    file_path = os.path.join(session['cwd'], context.args[0])
    try:
        await update.message.reply_document(document=open(file_path, 'rb'))
    except FileNotFoundError:
        await update.message.reply_text("File not found.")
    except Exception as e:
        await update.message.reply_text(f"An error occurred: {e}")

async def upload_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_authorized(user_id) or user_id not in user_sessions:
        await update.message.reply_text("You must start a shell session first with /start.")
        return
    session = user_sessions[user_id]
    doc = update.message.document
    file_name = doc.file_name
    file_path = os.path.join(session['cwd'], file_name)
    try:
        file = await doc.get_file()
        await file.download_to_drive(file_path)
        await update.message.reply_text(f"File '{file_name}' uploaded successfully to {session['cwd']}.")
    except Exception as e:
        await update.message.reply_text(f"Failed to upload file: {e}")

# --- Main Bot Setup ---
def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token or not AUTHORIZED_USERS:
        print("FATAL: TELEGRAM_BOT_TOKEN or AUTHORIZED_USERS not set. Check your .env file.")
        return

    application = Application.builder().token(token).build()
    handlers = [
        CommandHandler("start", start_command),
        CommandHandler("end", end_command),
        CommandHandler("controlC", control_c_command),
        CommandHandler("download", download_command),
        CommandHandler("type", type_command),
        CommandHandler("rc", rc_command),
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler),
        MessageHandler(filters.Document.ALL, upload_handler)
    ]
    application.add_handlers(handlers)

    print("Bot is running...")
    application.run_polling()

if __name__ == "__main__":
    main()