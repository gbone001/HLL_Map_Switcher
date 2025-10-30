# HLL Map Switcher Discord Bot

A simple Discord bot that allows users to change Hell Let Loose server maps through interactive buttons.

## Features

- Interactive map changing via Discord buttons
- Supports Warfare, Offensive, and Skirmish game modes
- Works with multiple RCON v2 servers
- Lets admins lock objective layouts for the current match via the CRCON HTTP API
- Shows the current map in real-time
- No slash commands needed; just click the buttons

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

### RCON v2 Setup

1. **Collect your RCON credentials:**
   - RCON host or IP address (as configured on the game server)
   - RCON port (default `7779` for HLL)
   - RCON password (set in your server configuration)
   - Optional server display names for Discord

2. **Verify RCON v2 access:**
   - Ensure your server build exposes the RCON v2 protocol
   - Confirm the RCON port is reachable from the machine running the bot

3. **Find Discord IDs:**
   - Enable Developer Mode in Discord (User Settings > Advanced)
   - Right-click your server -> Copy Server ID
   - Right-click your channel -> Copy Channel ID

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

### 4. Deploy to Railway
```bash
# Install the Railway CLI once
curl -fsSL https://railway.app/install.sh | sh

# Authenticate
railway login

# From inside this repository
railway up
```

If you change environment variables, run `railway variables set KEY=value` (or update them in the Railway dashboard) and redeploy with `railway up`.

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
