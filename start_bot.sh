#!/bin/bash

# Music Bot Startup Script

cd "$(dirname "$0")"

echo "🎵 Starting Music Bot..."
echo "========================="

# Check if virtual environment exists
if [ ! -d "env" ]; then
    echo "❌ Virtual environment not found!"
    echo "Creating virtual environment..."
    python3 -m venv env
    source env/bin/activate
    echo "Installing requirements..."
    pip install -r requirements.lock
else
    # Activate virtual environment
    source env/bin/activate
fi

# Check if .env exists
if [ ! -f ".env" ]; then
    echo "⚠️  WARNING: .env file not found!"
    echo "Please create .env with your DISCORD_TOKEN"
    exit 1
fi

# Display system status
echo ""
echo "System Checks:"
echo "--------------"
python diagnose.py | grep "✓\|✗"
echo ""

# Check if logs directory exists
mkdir -p logs 2>/dev/null

echo "🚀 Launching bot..."
echo "📝 Logs will be saved to musicbot.log"
echo "   Press Ctrl+C to stop the bot"
echo ""

# Run the bot
python music.py

# Deactivate on exit
deactivate
