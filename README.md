# Theme Bot

A Discord bot that plays a custom audio clip whenever a user joins a voice channel. Each server member can set their own personal intro theme from YouTube or SoundCloud.

## Features

- `/addtheme <url>` — Set a YouTube or SoundCloud URL as your intro (max 10 minutes)
- `/setclip <start> <duration>` — Choose which portion of the track to play (1–30 seconds)
- `/toggletheme` — Enable or disable your intro without removing it
- `/cleartheme` — Remove your theme and delete the cached audio file
- `/mytheme` — View your current theme settings

Audio is pre-downloaded and cached on disk so intros play instantly on join. Multiple simultaneous joins are queued per guild so no intro is dropped.

## Requirements

- Python 3.10+
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- FFmpeg
- libopus (`sudo apt install libopus0`)

## Setup

**1. Clone the repo**

```bash
git clone https://github.com/mschroeder2022/theme-bot.git
cd theme-bot
```

**2. Create a virtual environment and install dependencies**

```bash
python3 -m venv venv
source venv/bin/activate
pip install discord.py python-dotenv yt-dlp
```

**3. Configure your bot token**

Copy `.env.example` to `.env` and fill in your Discord bot token:

```bash
cp .env.example .env
# edit .env and set DISCORD_BOT_TOKEN=your_token_here
```

Create a Discord application and bot at [discord.com/developers](https://discord.com/developers/applications). Enable the **Voice States** and **Message Content** intents.

**4. Run the bot**

```bash
python bot1.py
```

## Running as a systemd service (Linux)

Copy and edit the included service file:

```bash
sudo cp "theme-bot(1).service" /etc/systemd/system/theme-bot.service
# Edit the file and replace YOUR_LINUX_USERNAME with your username
sudo systemctl daemon-reload
sudo systemctl enable --now theme-bot
```

## Project structure

```
theme-bot/
├── bot1.py                  # Main bot (slash commands, queue-based playback)
├── bot.py                   # Earlier version (prefix commands)
├── theme-bot(1).service     # systemd service template
├── .env.example             # Environment variable template
└── data/
    └── themes.json          # Created at runtime — stores user theme configs
```

## License

MIT
