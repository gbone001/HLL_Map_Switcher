# Discord Map Bot

This project is a Discord bot that allows users to change maps through interactive buttons. Users can select different game modes (Warfare, Skirmish, Offensive) and choose maps and their variants (e.g., Day, Night).

## Features

- Interactive buttons for map selection
- Support for multiple game modes
- Easy-to-use command structure
- Environment variable management for sensitive data

## Project Structure

```
discord-map-bot
├── src
│   ├── bot.py                # Entry point for the Discord bot
│   ├── commands
│   │   └── map_commands.py   # Command definitions for map selection
│   ├── handlers
│   │   └── button_handlers.py # Button interaction handlers
│   ├── utils
│   │   └── map_data.py       # Utility functions for map data management
│   └── config
│       └── settings.py       # Configuration settings and environment variable loading
├── requirements.txt           # Project dependencies
├── .env.example               # Template for environment variables
├── .gitignore                 # Files and directories to ignore in Git
└── README.md                  # Project documentation
```

## Installation

1. Clone the repository:
   ```
   git clone https://github.com/yourusername/discord-map-bot.git
   cd discord-map-bot
   ```

2. Create a virtual environment:
   ```
   python -m venv venv
   source venv/bin/activate  # On Windows use `venv\Scripts\activate`
   ```

3. Install the required packages:
   ```
   pip install -r requirements.txt
   ```

4. Set up your environment variables:
   - Copy `.env.example` to `.env` and fill in the required values.

## Usage

1. Run the bot:
   ```
   python src/bot.py
   ```

2. Interact with the bot in your Discord server to change maps.

## Contributing

Contributions are welcome! Please fork the repository and submit a pull request for any changes or improvements.

## License

This project is licensed under the MIT License. See the LICENSE file for details.