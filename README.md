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

## Quick Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/SpinexLive/HLL_Map_Switcher.git
   cd HLL_Map_Switcher
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Setup environment:**
   - Copy `.env.example.single` to `.env` for single server
   - Copy `.env.example.multiple` to `.env` for multiple servers
   - Fill in your Discord token and CRCON details

4. **Run the bot:**
   ```bash
   python src/bot.py
   ```

## Usage

The bot automatically posts a persistent button in your configured Discord channel. Users simply click the button and follow the prompts to change maps!

## Contributing

Pull requests welcome! Please fork the repository and submit your changes.

## License

MIT