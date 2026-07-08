# Music Bot Connection Loop Fix

## What Was Fixed

### 1. **Added Proper Logging**
- Replaced all `print()` statements with proper `logging` module
- All logs now save to `musicbot.log` file AND display in console
- This will help you see what's actually happening

### 2. **Fixed Voice State Update Bug**
The original `on_voice_state_update` function had a critical flaw:
- It was counting ALL channel members including bots
- Now it only counts human members when checking if bot is alone
- Added better timing checks to prevent premature disconnects
- Added `force=False` to disconnect to prevent forceful disconnections

### 3. **Improved Voice Connection Error Handling**
- Added timeout to connection attempts (10 seconds)
- Added try-catch blocks with detailed error messages
- Added checks for `is_connected()` before trying to play audio
- Added reconnect=False to prevent automatic reconnection loops

### 4. **Enhanced Diagnostics**
- Opus loading now shows critical errors if it fails
- FFmpeg check on bot startup
- Voice client status checks before playing
- Better error messages in audio playback

## How to Use

### Step 1: Run Diagnostics
```bash
cd /home/coxaexs/Downloads/musicbot-main
python diagnose.py
```

This will check:
- Python version
- FFmpeg installation
- Opus library
- Discord.py
- yt-dlp
- .env configuration

### Step 2: Fix Any Issues Found

**If FFmpeg is missing:**
```bash
sudo apt install ffmpeg
```

**If Opus is missing:**
```bash
sudo apt install libopus0 libopus-dev
```

**If Discord.py voice support is missing:**
```bash
pip install discord.py[voice]
```

### Step 3: Restart the Bot
```bash
python music.py
```

### Step 4: Check the Logs
Now when you run the bot, check:
1. **Console output** - Should show detailed startup info
2. **musicbot.log file** - Contains all logs with timestamps

Look for these messages:
- `✅ Opus loaded from: ...` - Opus is working
- `✅ FFmpeg found at: ...` - FFmpeg is working
- `Connecting to voice channel: ...` - When joining
- `✅ Successfully connected to ...` - Connection successful

## Common Issues & Solutions

### Issue 1: "Opus not loaded"
**Symptoms:** Bot connects but immediately disconnects, no audio plays
**Solution:**
```bash
sudo apt install libopus0 libopus-dev
# Restart bot
```

### Issue 2: "FFmpeg not found"
**Symptoms:** Bot connects but can't play audio
**Solution:**
```bash
sudo apt install ffmpeg
# Restart bot
```

### Issue 3: Connection timeout
**Symptoms:** "Failed to connect to voice channel: Connection timeout"
**Causes:**
- Network issues
- Bot doesn't have permission to join voice channel
- Discord API issues

**Solution:**
- Check bot permissions (Connect, Speak permissions in voice channel)
- Check your internet connection
- Try again in a few minutes

### Issue 4: Still getting connect/disconnect loop
**Possible causes:**
1. **Empty queue:** Bot connects but has nothing to play
   - Make sure you queue a song with `/play`
   
2. **Permissions:** Bot doesn't have Speak permission
   - Go to Discord Server Settings → Roles → Your Bot Role
   - Enable "Connect" and "Speak" permissions
   
3. **Region issues:** Discord voice region problems
   - Try changing your server's voice region
   - Server Settings → Overview → Server Region

4. **Intents not enabled:** Voice state intent not enabled in Discord Developer Portal
   - Go to: https://discord.com/developers/applications
   - Select your bot
   - Go to "Bot" section
   - Enable "PRESENCE INTENT" and "SERVER MEMBERS INTENT"
   - Save changes and restart bot

## Debugging Steps

1. **Check the log file:**
   ```bash
   tail -f musicbot.log
   ```
   This shows live logs as they happen

2. **Look for repeatng patterns:**
   - "Connecting to voice channel" repeated → Connection failing
   - "No voice client" → Bot disconnecting too fast
   - "Player error" → Audio playback issue

3. **Test with a simple command:**
   ```
   /play never gonna give you up
   ```
   Watch the logs to see exactly where it fails

4. **Check Discord permissions:**
   - Bot needs "Connect" and "Speak" in voice channels
   - Bot needs "Use Voice Activity" (not push-to-talk)

## Still Having Issues?

If the problem persists, share the contents of `musicbot.log` file. The new logging will show exactly where the issue is happening.

You can check the last 50 lines with:
```bash
tail -n 50 musicbot.log
```

Or watch it live:
```bash
tail -f musicbot.log
```

## What Changed in the Code

1. **music.py line 1-30:** Added logging module and configuration
2. **music.py load_opus():** Better error handling and logging
3. **music.py ensure_voice():** Added timeout, error handling, connection checks  
4. **music.py play_next():** Added voice client status checks
5. **music.py on_voice_state_update():** Fixed member counting logic
6. **music.py on_ready():** Added system diagnostics on startup

All these changes make the bot more reliable and help you debug issues quickly!
