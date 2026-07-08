from flask import Flask, render_template, request, jsonify, send_file, Response
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_cors import CORS
import secrets
import time
from datetime import datetime
import threading
import yt_dlp
import os
import glob
from pathlib import Path

app = Flask(__name__)
app.config['SECRET_KEY'] = secrets.token_hex(16)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# Video cache directory
VIDEO_CACHE_DIR = '/tmp/watch_together_cache'
MAX_CACHED_VIDEOS = 2

# Create cache directory
os.makedirs(VIDEO_CACHE_DIR, exist_ok=True)

# Store active watch sessions
# Format: {room_id: {'video_id': str, 'title': str, 'host': str, 'created_at': timestamp, 'state': dict, 'cleanup_timer': obj, 'warning_timer': obj, 'video_file': str}}
watch_sessions = {}

# Store cleanup responses
# Format: {room_id: {'keep_alive': bool, 'responded': bool}}
cleanup_responses = {}

# yt-dlp configuration for 720p video download
YTDL_OPTIONS = {
    'format': 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best',
    'outtmpl': os.path.join(VIDEO_CACHE_DIR, '%(id)s.%(ext)s'),
    'noplaylist': True,
    'quiet': False,
    'no_warnings': False,
    'merge_output_format': 'mp4',
    'extractor_args': {
        'youtube': {
            'player_client': ['android']
        }
    },
}

def cleanup_old_videos():
    """Keep only the 2 most recent videos in cache"""
    try:
        video_files = glob.glob(os.path.join(VIDEO_CACHE_DIR, '*.*'))
        
        # Filter out non-video files
        video_files = [f for f in video_files if f.endswith(('.mp4', '.webm', '.mkv'))]
        
        if len(video_files) > MAX_CACHED_VIDEOS:
            # Sort by modification time (oldest first)
            video_files.sort(key=lambda x: os.path.getmtime(x))
            
            # Delete oldest files
            files_to_delete = video_files[:len(video_files) - MAX_CACHED_VIDEOS]
            for file in files_to_delete:
                try:
                    os.remove(file)
                    print(f"🗑️ Cleaned up old video: {os.path.basename(file)}")
                except Exception as e:
                    print(f"❌ Failed to delete {file}: {e}")
    except Exception as e:
        print(f"❌ Cleanup error: {e}")

def download_video(video_id: str) -> tuple:
    """Download video in 720p and return (file_path, title)"""
    try:
        # Check if already downloaded
        existing_files = glob.glob(os.path.join(VIDEO_CACHE_DIR, f'{video_id}.*'))
        if existing_files:
            print(f"✅ Video {video_id} already in cache")
            # Get video info for title
            with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
                info = ydl.extract_info(f'https://www.youtube.com/watch?v={video_id}', download=False)
                return existing_files[0], info.get('title', 'Unknown')
        
        print(f"⏬ Downloading video {video_id} in 720p...")
        with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
            info = ydl.extract_info(f'https://www.youtube.com/watch?v={video_id}', download=True)
            title = info.get('title', 'Unknown')
            
            # Find the downloaded file
            downloaded_files = glob.glob(os.path.join(VIDEO_CACHE_DIR, f'{video_id}.*'))
            if not downloaded_files:
                raise Exception("Download completed but file not found")
            
            video_file = downloaded_files[0]
            print(f"✅ Downloaded: {os.path.basename(video_file)}")
            
            # Cleanup old videos
            cleanup_old_videos()
            
            return video_file, title
            
    except Exception as e:
        print(f"❌ Download error: {e}")
        raise

@app.route('/')
def index():
    return "DeepPixel Watch Together - Music Bot Web Player"

@app.route('/watch/<room_id>')
def watch(room_id):
    """Main watch page"""
    session = watch_sessions.get(room_id)
    if not session:
        return "Session not found or expired", 404
    
    return render_template('watch.html', 
                         room_id=room_id,
                         video_id=session['video_id'],
                         title=session['title'])

@app.route('/api/create_session', methods=['POST'])
def create_session():
    """Create a new watch session - downloads video in 720p"""
    data = request.json
    video_id = data.get('video_id')
    title = data.get('title', 'Unknown Video')
    host = data.get('host', 'Unknown')
    
    if not video_id:
        return jsonify({'error': 'video_id required'}), 400
    
    try:
        # Download video in 720p
        video_file, actual_title = download_video(video_id)
        if not title or title == 'Unknown Video':
            title = actual_title
        
        # Generate unique room ID
        room_id = secrets.token_urlsafe(8)
        
        watch_sessions[room_id] = {
            'video_id': video_id,
            'title': title,
            'host': host,
            'created_at': time.time(),
            'video_file': video_file,
            'state': {
                'playing': False,
                'currentTime': 0,
                'lastUpdate': time.time()
            },
            'viewers': [],
            'cleanup_timer': None,
            'warning_timer': None
        }
        
        # Schedule cleanup check for empty room (30 seconds)
        timer = threading.Timer(30.0, check_empty_room, args=[room_id])
        timer.daemon = True
        timer.start()
        watch_sessions[room_id]['cleanup_timer'] = timer
        
        print(f"✅ Created session {room_id} with video {os.path.basename(video_file)}")
        
        return jsonify({
            'room_id': room_id,
            'url': f'/watch/{room_id}'
        })
    except Exception as e:
        return jsonify({'error': f'Failed to prepare video: {str(e)}'}), 500

@app.route('/api/session/<room_id>')
def get_session(room_id):
    """Get session info"""
    session = watch_sessions.get(room_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404
    
    return jsonify({
        'video_id': session['video_id'],
        'title': session['title'],
        'host': session['host'],
        'viewers': len(session['viewers'])
    })

@app.route('/api/video/<room_id>')
def stream_video(room_id):
    """Stream video file for a session with range support"""
    session = watch_sessions.get(room_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404
    
    video_file = session.get('video_file')
    if not video_file or not os.path.exists(video_file):
        return jsonify({'error': 'Video file not found'}), 404
    
    # Get file size
    file_size = os.path.getsize(video_file)
    
    # Handle range requests for video seeking
    range_header = request.headers.get('Range', None)
    if range_header:
        byte_range = range_header.replace('bytes=', '').split('-')
        start = int(byte_range[0]) if byte_range[0] else 0
        end = int(byte_range[1]) if len(byte_range) > 1 and byte_range[1] else file_size - 1
        length = end - start + 1
        
        def generate():
            with open(video_file, 'rb') as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk_size = min(8192, remaining)
                    data = f.read(chunk_size)
                    if not data:
                        break
                    remaining -= len(data)
                    yield data
        
        response = Response(generate(), 206, mimetype='video/mp4')
        response.headers.add('Content-Range', f'bytes {start}-{end}/{file_size}')
        response.headers.add('Accept-Ranges', 'bytes')
        response.headers.add('Content-Length', str(length))
        return response
    else:
        # Full file
        return send_file(video_file, mimetype='video/mp4')

# Socket.IO events for real-time sync
@socketio.on('join')
def on_join(data):
    """User joins a watch room"""
    room_id = data.get('room_id')
    username = data.get('username', 'Anonymous')
    
    if room_id not in watch_sessions:
        emit('error', {'message': 'Session not found'})
        return
    
    join_room(room_id)
    session = watch_sessions[room_id]
    
    # Cancel empty room cleanup timer since someone joined
    if 'cleanup_timer' in session and session['cleanup_timer']:
        session['cleanup_timer'].cancel()
        session['cleanup_timer'] = None
    
    # Add viewer
    viewer_info = {'id': request.sid, 'username': username}
    if viewer_info not in session['viewers']:
        session['viewers'].append(viewer_info)
    
    # Start warning check timer (check in 30s if video is paused)
    schedule_warning_check(room_id)
    
    # Send current state to new viewer
    emit('sync_state', session['state'], room=request.sid)
    
    # Notify others
    emit('user_joined', {
        'username': username,
        'viewers': len(session['viewers'])
    }, room=room_id, skip_sid=request.sid)
    
    print(f"{username} joined room {room_id}")

@socketio.on('leave')
def on_leave(data):
    """User leaves a watch room"""
    room_id = data.get('room_id')
    username = data.get('username', 'Anonymous')
    
    if room_id in watch_sessions:
        leave_room(room_id)
        session = watch_sessions[room_id]
        
        # Remove viewer
        session['viewers'] = [v for v in session['viewers'] if v['id'] != request.sid]
        
        # Notify others
        emit('user_left', {
            'username': username,
            'viewers': len(session['viewers'])
        }, room=room_id)
        
        # If room is now empty, schedule cleanup
        if len(session['viewers']) == 0:
            print(f"{username} left room {room_id}, room is now empty. Scheduling cleanup...")
            timer = threading.Timer(30.0, check_empty_room, args=[room_id])
            timer.daemon = True
            timer.start()
            session['cleanup_timer'] = timer
        
        print(f"{username} left room {room_id}")

@socketio.on('disconnect')
def on_disconnect():
    """Handle disconnect"""
    # Find and remove viewer from all sessions
    for room_id, session in watch_sessions.items():
        for viewer in session['viewers']:
            if viewer['id'] == request.sid:
                session['viewers'].remove(viewer)
                emit('user_left', {
                    'username': viewer['username'],
                    'viewers': len(session['viewers'])
                }, room=room_id)
                
                # If room is now empty, schedule cleanup
                if len(session['viewers']) == 0:
                    print(f"User disconnected from {room_id}, room is now empty. Scheduling cleanup...")
                    timer = threading.Timer(30.0, check_empty_room, args=[room_id])
                    timer.daemon = True
                    timer.start()
                    session['cleanup_timer'] = timer
                
                break

@socketio.on('play')
def on_play(data):
    """Video play event"""
    room_id = data.get('room_id')
    current_time = data.get('currentTime', 0)
    
    if room_id in watch_sessions:
        session = watch_sessions[room_id]
        session['state']['playing'] = True
        session['state']['currentTime'] = current_time
        session['state']['lastUpdate'] = time.time()
        
        # Reset warning timer since video is now playing
        schedule_warning_check(room_id)
        
        # Broadcast to all in room except sender
        emit('play', {'currentTime': current_time}, room=room_id, skip_sid=request.sid)
        print(f"Play in room {room_id} at {current_time}s")

@socketio.on('pause')
def on_pause(data):
    """Video pause event"""
    room_id = data.get('room_id')
    current_time = data.get('currentTime', 0)
    
    if room_id in watch_sessions:
        session = watch_sessions[room_id]
        session['state']['playing'] = False
        session['state']['currentTime'] = current_time
        session['state']['lastUpdate'] = time.time()
        
        # Schedule warning check since video is now paused
        schedule_warning_check(room_id)
        
        # Broadcast to all in room except sender
        emit('pause', {'currentTime': current_time}, room=room_id, skip_sid=request.sid)
        print(f"Pause in room {room_id} at {current_time}s")

@socketio.on('seek')
def on_seek(data):
    """Video seek event"""
    room_id = data.get('room_id')
    current_time = data.get('currentTime', 0)
    
    if room_id in watch_sessions:
        session = watch_sessions[room_id]
        session['state']['currentTime'] = current_time
        session['state']['lastUpdate'] = time.time()
        
        # Broadcast to all in room except sender
        emit('seek', {'currentTime': current_time}, room=room_id, skip_sid=request.sid)
        print(f"Seek in room {room_id} to {current_time}s")

@socketio.on('request_sync')
def on_request_sync(data):
    """Request current state from host"""
    room_id = data.get('room_id')
    
    if room_id in watch_sessions:
        session = watch_sessions[room_id]
        emit('sync_state', session['state'], room=request.sid)

@socketio.on('keep_session_alive')
def on_keep_alive(data):
    """User wants to keep session alive"""
    room_id = data.get('room_id')
    
    if room_id in cleanup_responses:
        cleanup_responses[room_id]['keep_alive'] = True
        cleanup_responses[room_id]['responded'] = True
        emit('session_kept_alive', {}, room=room_id)
        print(f"Session {room_id} kept alive by user")
        
        # Restart the warning timer
        schedule_warning_check(room_id)

@socketio.on('close_session_now')
def on_close_session(data):
    """User wants to close session"""
    room_id = data.get('room_id')
    
    if room_id in cleanup_responses:
        cleanup_responses[room_id]['keep_alive'] = False
        cleanup_responses[room_id]['responded'] = True
        print(f"Session {room_id} will be closed by user request")

def close_session(room_id):
    """Close a session and notify users"""
    if room_id in watch_sessions:
        # Cancel any pending timers
        session = watch_sessions[room_id]
        if 'cleanup_timer' in session and session['cleanup_timer']:
            session['cleanup_timer'].cancel()
        if 'warning_timer' in session and session['warning_timer']:
            session['warning_timer'].cancel()
        
        # Delete video file if it exists
        video_file = session.get('video_file')
        if video_file and os.path.exists(video_file):
            try:
                os.remove(video_file)
                print(f"🗑️ Deleted video file: {os.path.basename(video_file)}")
            except Exception as e:
                print(f"❌ Failed to delete video: {e}")
        
        # Notify all users
        socketio.emit('session_closed', {
            'message': 'Session has been closed'
        }, room=room_id)
        
        # Clean up
        del watch_sessions[room_id]
        if room_id in cleanup_responses:
            del cleanup_responses[room_id]
        
        print(f"Session {room_id} closed and cleaned up")

def check_empty_room(room_id):
    """Check if room is empty and close if needed"""
    if room_id not in watch_sessions:
        return
    
    session = watch_sessions[room_id]
    
    # If no viewers, close the session
    if len(session['viewers']) == 0:
        print(f"Room {room_id} is empty, closing session...")
        close_session(room_id)
    else:
        # Room has viewers, schedule warning check
        schedule_warning_check(room_id)

def schedule_warning_check(room_id):
    """Schedule a check to see if video is paused"""
    if room_id not in watch_sessions:
        return
    
    session = watch_sessions[room_id]
    
    # Cancel existing warning timer
    if 'warning_timer' in session and session['warning_timer']:
        session['warning_timer'].cancel()
    
    # Schedule check in 30 seconds
    timer = threading.Timer(30.0, check_paused_and_warn, args=[room_id])
    timer.daemon = True
    timer.start()
    session['warning_timer'] = timer

def check_paused_and_warn(room_id):
    """Check if video is paused and ask users if they want to close"""
    if room_id not in watch_sessions:
        return
    
    session = watch_sessions[room_id]
    
    # If no viewers, close immediately
    if len(session['viewers']) == 0:
        print(f"Room {room_id} became empty, closing...")
        close_session(room_id)
        return
    
    # If video is playing, schedule another check
    if session['state']['playing']:
        schedule_warning_check(room_id)
        return
    
    # Video is paused and has viewers - ask if they want to close
    print(f"Room {room_id} has paused video, asking users...")
    
    # Initialize response tracker
    cleanup_responses[room_id] = {
        'keep_alive': False,
        'responded': False
    }
    
    # Send warning to all users
    socketio.emit('cleanup_warning', {
        'message': 'Video durdurulmuş. 5 saniye içinde cevap verilmezse oturum kapatılacak.',
        'timeout': 5
    }, room=room_id)
    
    # Wait 5 seconds then check response
    timer = threading.Timer(5.0, handle_cleanup_response, args=[room_id])
    timer.daemon = True
    timer.start()

def handle_cleanup_response(room_id):
    """Handle user response to cleanup warning"""
    if room_id not in watch_sessions:
        return
    
    if room_id not in cleanup_responses:
        # No response system initialized, just reschedule
        schedule_warning_check(room_id)
        return
    
    response = cleanup_responses[room_id]
    
    if response['responded'] and response['keep_alive']:
        # User wants to keep session alive
        print(f"Users want to keep session {room_id} alive")
        del cleanup_responses[room_id]
        # Will reschedule in the keep_alive handler
    else:
        # No response or user wants to close
        print(f"No response or close requested for {room_id}, closing session...")
        close_session(room_id)

if __name__ == '__main__':
    print("Starting DeepPixel Watch Together Server...")
    print("Access at: http://localhost:5000")
    socketio.run(app, host='0.0.0.0', port=5000, debug=True, allow_unsafe_werkzeug=True)
