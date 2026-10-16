#!/usr/bin/env python3
"""
Diagnostic script to check if all dependencies for the music bot are working
"""
import sys
import os
import ctypes.util
import shutil

def check_python():
    print(f"✓ Python version: {sys.version}")
    if sys.version_info < (3, 11):
        print("✗ Python 3.11 or newer is required")
        return False
    return True

def check_ffmpeg():
    ffmpeg = shutil.which('ffmpeg')
    if ffmpeg:
        print(f"✓ FFmpeg found: {ffmpeg}")
        return True
    else:
        print("✗ FFmpeg NOT found!")
        print("  Install with: sudo apt install ffmpeg (Ubuntu/Debian)")
        print("            or: brew install ffmpeg (macOS)")
        return False

def check_opus():
    """Check if opus library is available"""
    opus_paths = [
        # Linux paths
        '/usr/lib/x86_64-linux-gnu/libopus.so.0',
        '/usr/lib/aarch64-linux-gnu/libopus.so.0',
        '/usr/lib/libopus.so.0',
        '/usr/lib/libopus.so',
        # macOS paths
        '/opt/homebrew/lib/libopus.dylib',
        '/usr/local/lib/libopus.dylib',
    ]
    
    found = False
    for path in opus_paths:
        if os.path.exists(path):
            print(f"✓ Opus library found: {path}")
            found = True
            break
    
    # Try system library finder
    if not found:
        opus = ctypes.util.find_library('opus')
        if opus:
            print(f"✓ Opus found via system: {opus}")
            found = True
    
    if not found:
        print("✗ Opus library NOT found!")
        print("  Install with: sudo apt install libopus0 (Ubuntu/Debian)")
        print("            or: brew install opus (macOS)")
        return False
    
    return True

def check_discord():
    try:
        import discord
        print(f"✓ discord.py version: {discord.__version__}")
        
        # Try loading opus in discord.py
        if discord.opus.is_loaded():
            print("✓ Discord.py has Opus loaded")
        else:
            print("⚠ Discord.py Opus NOT loaded yet (will be loaded on bot startup)")
        
        return True
    except ImportError:
        print("✗ discord.py NOT installed!")
        print("  Install with: pip install discord.py[voice]")
        return False

def check_ytdlp():
    try:
        import yt_dlp
        print(f"✓ yt-dlp installed")
        
        # Check CLI availability
        ytdlp_cli = shutil.which('yt-dlp')
        if ytdlp_cli:
            print(f"✓ yt-dlp CLI found: {ytdlp_cli}")
        else:
            print("⚠ yt-dlp CLI not in PATH (using Python module)")
        
        return True
    except ImportError:
        print("✗ yt-dlp NOT installed!")
        print("  Install with: pip install yt-dlp")
        return False

def check_env():
    data_dir = os.getenv("DATA_DIR", "")
    if os.path.exists('.env'):
        from dotenv import load_dotenv
        load_dotenv()
    elif data_dir and os.path.exists(os.path.join(data_dir, '.env')):
        from dotenv import load_dotenv
        load_dotenv(os.path.join(data_dir, '.env'))
    
    token = os.getenv('DISCORD_TOKEN')
    if token:
        print(f"✓ DISCORD_TOKEN is set (length: {len(token)} chars)")
        return True
    else:
        print("✗ DISCORD_TOKEN not set!")
        print("  Set DISCORD_TOKEN in environment or .env file")
        return False

def check_features():
    try:
        import config
        features = [
            ("DJ Booth", getattr(config, "DJ_ENABLED", True)),
            ("Watch Together", getattr(config, "WATCHTOGETHER_ENABLED", True)),
            ("Stems Separation", getattr(config, "STEMS_ENABLED", True)),
            ("Shared Co-Browser", getattr(config, "COBROWSER_ENABLED", True)),
            ("Web Dashboard", getattr(config, "WEB_UI_ENABLED", True)),
            ("Autoplay", getattr(config, "AUTOPLAY_ENABLED", True)),
            ("Lyrics", getattr(config, "LYRICS_ENABLED", True)),
            ("Huddle Integration", getattr(config, "HUDDLE_ENABLED", True)),
        ]
        for name, enabled in features:
            status = "✓ Enabled" if enabled else "○ Disabled"
            print(f"  {status}: {name}")
        return True
    except Exception as e:
        print(f"⚠ Could not inspect feature flags: {e}")
        return True

def main():
    print("=" * 60)
    print("Music Bot Diagnostic Tool")
    print("=" * 60)
    print()
    
    checks = [
        ("Python", check_python),
        ("FFmpeg", check_ffmpeg),
        ("Opus Library", check_opus),
        ("Discord.py", check_discord),
        ("yt-dlp", check_ytdlp),
        ("Environment", check_env),
        ("Features", check_features),
    ]
    
    results = []
    for name, check_func in checks:
        print(f"\nChecking {name}...")
        try:
            result = check_func()
            results.append(result)
        except Exception as e:
            print(f"✗ Error checking {name}: {e}")
            results.append(False)
    
    print()
    print("=" * 60)
    if all(results):
        print("✓ All checks passed! Bot should work correctly.")
    else:
        print("✗ Some checks failed. Please fix the issues above.")
    print("=" * 60)

if __name__ == "__main__":
    main()
