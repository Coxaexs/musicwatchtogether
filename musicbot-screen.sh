#!/bin/bash
# Runs the music bot in the screen session "musicbot" and restarts it when it
# exits. Run as the user that owns the bot's files (server); the server user's
# crontab starts it at boot with:  @reboot /path/to/musicbot-screen.sh
#
# To restart the bot, touch .restart in this folder. Members of the server
# group can do that, so it works without sudo.
cd "$(dirname "$(readlink -f "$0")")" || exit 1

if [ "$1" != "--inner" ]; then
    if screen -list musicbot | grep -q "\.musicbot"; then
        echo "The music bot is already running (screen -r musicbot)."
        exit 0
    fi
    exec screen -dmS musicbot "$0" --inner
fi

while true; do
    rm -f .restart
    env/bin/python music.py &
    pid=$!
    while kill -0 "$pid" 2>/dev/null; do
        if [ -e .restart ]; then
            echo "[musicbot] restart requested"
            kill "$pid"
        fi
        sleep 2
    done
    wait "$pid"
    echo "[musicbot] exited, starting again in 5 s"
    sleep 5
done
