# HLL Map Switcher Discord Bot

A simple Discord bot that allows users to change Hell Let Loose server maps through interactive buttons.

## Features

- 🗺️ Interactive map changing via Discord buttons
- ⚔️ Support for all game modes (Warfare, Offensive, Skirmish)
- 🌍 Multiple server support
- 🔄 Shows current map in real-time
- 🎯 No slash commands needed - just click buttons!

## Prerequisites

### Discord Bot Setup

1. **Create Discord Application:**
   - Go to https://discord.com/developers/applications
   - Click "New Application"
   - Give it a name (e.g., "HLL Map Switcher")

2. **Create Bot:**
   - Go to "Bot" tab
   - Click "Add Bot"
   - Copy the bot token (you'll need this for `.env`)
   - Enable "Message Content Intent" under Privileged Gateway Intents

3. **Invite Bot to Server:**
   - Go to "OAuth2" > "URL Generator"
   - Select scopes: `bot` and `applications.commands`
   - Select permissions: `Send Messages`, `Use Slash Commands`, `Embed Links`
   - Copy the generated URL and open it to invite the bot to your server

### CRCON API Setup

1. **Get your CRCON details:**
   - CRCON URL (e.g., `http://your-server.com:8010`)
   - API Token (found in your CRCON settings)
   - RCON host, port, and password

2. **Find Discord IDs:**
   - Enable Developer Mode in Discord (User Settings > Advanced)
   - Right-click your server → Copy Server ID
   - Right-click your channel → Copy Channel ID

## Installation

### 1. Install System Dependencies
```bash
sudo apt update
sudo apt install python3-full python3-pip tmux git
```

### 2. Clone Repository
```bash
git clone https://github.com/SpinexLive/HLL_Map_Switcher.git
cd HLL_Map_Switcher
```

### 3. Setup Environment
```bash
# For single server
cp .env.example.single .env

# OR for multiple servers
cp .env.example.multiple .env

# Edit the .env file with your details
nano .env
```

### 4. Run Bot in tmux Session
```bash
# Create new tmux session
tmux new-session -s hll-bot

# Inside tmux session, setup and run:
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python src/bot.py

# Detach from tmux (Ctrl+B, then D)
# Bot will keep running in background
```

## Managing the Bot

### Start Bot (One Command)
```bash
tmux new-session -d -s hll-bot 'cd /path/to/HLL_Map_Switcher && python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt && python src/bot.py'
```

### Check Bot Status
```bash
tmux list-sessions
tmux attach-session -t hll-bot
```

### Stop Bot
```bash
tmux kill-session -t hll-bot
```

### View Bot Logs
```bash
tmux attach-session -t hll-bot
```

## Usage

The bot automatically posts a persistent button in your configured Discord channel. Users simply click the button and follow the prompts to change maps!

## Troubleshooting

- **Bot offline:** Check tmux session with `tmux attach-session -t hll-bot`
- **Environment issues:** The virtual environment is created automatically in tmux
- **Missing packages:** Dependencies are installed automatically when starting

## Contributing

Pull requests welcome! Please fork the repository and submit your changes.

## License

MIT License