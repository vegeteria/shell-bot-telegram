# Telegram Shell Bot

This is a Python-based Telegram bot that provides a remote shell interface, allowing authorized users to execute shell commands on the host machine.

## Features

*   **Remote Shell Access:** Provides a persistent `bash` shell session for each authorized user.
*   **Secure:** Restricts access to a pre-defined list of authorized user IDs.
*   **File Transfer:** Supports uploading and downloading files between the user and the host machine.
*   **Rclone Integration:** Includes a specific command for running `rclone` operations with a real-time progress bar.
*   **Session Management:** Allows users to start, stop, and interrupt shell sessions.
*   **Interactive Prompts:** Can send input to commands that require interactive text entry.

## Installation

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/your-username/your-repository.git
    cd your-repository
    ```

2.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

3.  **Configure the bot:**
    Create a `.env` file in the project root and add your Telegram Bot token and a comma-separated list of authorized user IDs:
    ```
    TELEGRAM_BOT_TOKEN=your-telegram-bot-token
    AUTHORIZED_USERS=123456789,987654321
    ```

## Usage

1.  **Run the bot:**
    ```bash
    python bot.py
    ```
2.  **Interact with the bot on Telegram:**
    *   Open a chat with your bot.
    *   Use the commands below to manage your shell session.

## Commands

*   `/start` - Starts an interactive shell session.
*   `/end` - Ends the current shell session.
*   `/controlC` - Sends an interrupt signal (Ctrl+C) to the running command.
*   `/download <file_path>` - Downloads a file from the bot's machine.
*   **To upload a file**, simply send it as a document to the chat.
*   `/type <text>` - Sends text to an interactive prompt (e.g., for password entry).
*   `/rc <rclone_command>` - Executes an `rclone` command (e.g., `rc copy source: remote:`). The `-P` flag is added automatically for progress.

Any other text message will be treated as a command to be executed in the shell.

## How It Works

The bot uses `asyncio` to run a `bash` subprocess for each user. It captures `stdout` and `stderr` in a non-blocking way, buffers the output, and periodically sends it to the user as a single, editable message to provide a smooth, real-time experience without hitting Telegram's API limits.

## Contributing

Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.
