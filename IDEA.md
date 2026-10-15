# Hell Let Loose Map Switcher - Docker Deployment

## Docker Deployment Overview

This repository contains a Discord automation bot for Hell Let Loose CRCON-backed server control.

### Key Features
- **Discord Control Panel**: Persistent panel for map changes, objectives, dynamic weather, timers
- **CRCON Integration**: Token-based HTTP client with direct RCON fallback
- **Multi-Server Support**: Configure multiple servers via environment variables
- **Docker Ready**: Fully containerized with Docker Compose support

## Docker Deployment

### Quick Start

```bash
# Build and run with Docker Compose
docker compose up -d

# Build image manually
docker build -t hll-map-switcher .

# Run container
docker run -d --name hll-bot \
  -v $(pwd)/data:/app/data \
  -e DISCORD_TOKEN=your_token \
  -e API_ENDPOINT=your_api_endpoint \
  -e MAPS_API_KEY=your_maps_key \
  hll-map-switcher
```

### Docker Compose

The `docker-compose.yml` file provides a complete environment:

- **Persistent Storage**: Data volume for map cache and configuration
- **Environment Variables**: Load from `.env` file
- **Restart Policy**: Automatically restart on failure

### Configuration

#### Environment Variables

For single server setup (`.env.example.single`):

```yaml
# Discord Settings
DISCORD_TOKEN=your_discord_bot_token
DISCORD_GUILD_ID=your_discord_guild_id
DISCORD_CHANNEL_ID=your_discord_channel_id

# Single Server Configuration
SERVER_NAME=Your Server Name

# CRCON HTTP API
CRCON_BASE_URL=https://your-crcon-host/api
CRCON_TOKEN=crcon-django-token

# Optional direct game-server RCON fallback
RCON_HOST=your-game-server-host
RCON_PORT=12345
RCON_PASSWORD=your_rcon_password

# Optional Settings
WARFARE_MATCH_TIMER_MINUTES=90
WARFARE_WARMUP_TIMER_MINUTES=3
DEBUG=True
```

For multiple servers setup (`.env.example.multiple`):

```yaml
# Configure multiple servers with SERVER{N}_ prefix
SERVER1_CRCON_BASE_URL=https://crcon1.example.com/api
SERVER1_CRCON_TOKEN=token1
SERVER1_NAME=Server 1
SERVER2_CRCON_BASE_URL=https://crcon2.example.com/api
SERVER2_CRCON_TOKEN=token2
SERVER2_NAME=Server 2
```

#### Configuration Files

- **`requirements.txt`**: Python dependencies (auto-copied)
- **`data/`**: Persistent storage directory
- **`src/`**: Bot source code

### Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY data/ ./data/

RUN mkdir -p /app/data && chmod 755 /app/data

ENV MAP_CACHE_DIR=/app/data

CMD ["python", "src/bot.py"]
```

### Data Management

The `data/` directory is persistent across container restarts:

- **Map Cache**: `data/maps_cache.json` stores CRCON map catalog
- **Configuration**: All persistent state is stored here
- **Access**: Container mounts host directory to `/app/data`

### Bot Startup

On first run:

1. Environment variables are validated
2. Map cache is refreshed from CRCON
3. Discord bot connects and registers slash commands
4. Persistent control panel is created

### Common Operations

#### Restart Container

```bash
docker compose restart
docker restart hll-bot
```

#### Stop/Close Container

```bash
# Graceful shutdown (SIGTERM)
docker compose down
docker stop hll-bot

# Force stop (SIGKILL) if needed
docker compose kill
docker kill hll-bot

# Stop and remove container + volumes
docker compose down -v
docker stop hll-bot && docker rm -v hll-bot
```

#### Access Logs

```bash
docker compose logs -f
docker logs hll-bot
```

#### Map Cache Refresh

```bash
# From within container
docker exec hll-bot python -c "from src.utils.map_data import refresh_map_cache; refresh_map_cache(force=True)"
```

#### Configuration Updates

Update environment variables in `.env` file and restart:

```bash
docker compose down
docker compose up -d
```

## Production Considerations

### Security

- Use `.env` file with restricted permissions
- Environment variables should not be committed to version control
- Consider using Docker secrets for production deployments

### Monitoring

- Bot logs to stdout (captured by Docker)
- Watch for CRCON API errors in logs
- Monitor Discord connection status

### Scaling

- Single container deployment for most use cases
- Multiple containers for high availability
- Consider load balancing for high traffic guilds

## Troubleshooting

### Common Issues

1. **Missing Environment Variables**
   - Check `.env` file exists
   - Restart container after changes

2. **CRCON API Errors**
   - Verify `CRCON_BASE_URL` and `CRCON_TOKEN`
   - Check if CRCON service is available

3. **Discord Connection Issues**
   - Verify `DISCORD_TOKEN` is valid
   - Check bot permissions in target channel

4. **Map Cache Issues**
   - Delete `data/maps_cache.json` to force refresh
   - Restart container to rebuild cache

### Debugging

```bash
# Check container status
docker ps
docker exec -it hll-bot /bin/bash

# Python debugging
python -c "import src; print(src.__file__)"
```

## Health Checks

Monitor these indicators:

- **Discord Connection**: Bot should show "has connected to Discord!"
- **CRCON API**: No repeated "CRCON API unavailable" warnings
- **Map Cache**: Should refresh within 24 hours
- **Panel Functionality**: All buttons should respond correctly

## Conclusion

This Docker deployment provides a complete, containerized solution for the Hell Let Loose map switcher bot.

Key benefits:

- **Portability**: Works on any Docker-compatible system
- **Persistence**: Data survives container restarts
- **Scalability**: Easy to scale for multiple guilds
- **Maintenance**: Simplified updates and configuration

For production deployments, consider:

- Using Docker Swarm or Kubernetes for orchestration
- Setting up external monitoring and logging
- Implementing proper backup strategies for configuration
- Using environment-specific Docker configurations

The bot is ready to manage your Hell Let Loose servers via Discord with full CRCON integration and fallback capabilities.
