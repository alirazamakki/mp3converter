import os
import uuid
import re
import asyncio
import logging
import shutil
import time
import random
import json
from pathlib import Path
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, HttpUrl, field_validator
import yt_dlp
import requests
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from allowed_domains import get_allowed_origins, validate_url, is_allowed_domain

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)
CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)
FILE_EXPIRY_MINUTES = 10
MAX_CONCURRENT_CONVERSIONS = 10
YOUTUBE_REGEX = r"^(https?://)?(www\.)?(youtube\.com|youtu\.?be)/.+$"

# YouTube API Configuration
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "YOUR_API_KEY_HERE")
youtube = build('youtube', 'v3', developerKey=YOUTUBE_API_KEY)

# In-memory storage
jobs = {}
conversion_semaphore = asyncio.Semaphore(MAX_CONCURRENT_CONVERSIONS)
current_conversions = set()
video_info_cache = {}

# Check FFmpeg installation
def check_ffmpeg():
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise RuntimeError("FFmpeg not found. Install ffmpeg and add to PATH.")
    logger.info(f"Using FFmpeg: {ffmpeg_path}")
    return ffmpeg_path

FFMPEG_PATH = check_ffmpeg()

# Lifespan manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    cleanup_task = asyncio.create_task(cleanup_old_files())
    logger.info("Application started")
    yield
    # Shutdown
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    logger.info("Application shutdown")

app = FastAPI(
    title="YouTube to MP3 Converter API",
    description="Robust YouTube video to MP3 conversion with automatic cleanup",
    version="2.0",
    lifespan=lifespan
)

# Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_allowed_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Domain validation middleware
@app.middleware("http")
async def validate_domain_middleware(request: Request, call_next):
    if not is_allowed_domain(request.client.host):
        return JSONResponse(
            status_code=403,
            content={"detail": "Access denied: Domain not allowed"}
        )
    return await call_next(request)

# Models
class ConversionRequest(BaseModel):
    url: HttpUrl
    quality: str = "high"  # high, medium, low

    @field_validator('url')
    def validate_youtube_url(cls, v):
        if not re.match(YOUTUBE_REGEX, str(v)):
            raise ValueError("Invalid YouTube URL")
        return v

class VideoMetadataRequest(BaseModel):
    url: HttpUrl

    @field_validator('url')
    def validate_youtube_url(cls, v):
        if not re.match(YOUTUBE_REGEX, str(v)):
            raise ValueError("Invalid YouTube URL")
        return v

# Helper functions
def sanitize_filename(title: str) -> str:
    """Sanitize filename and ensure it's not too long"""
    sanitized = re.sub(r'[\\/*?:"<>|]', "", title).strip()
    return sanitized[:100]  # Max 100 characters

def get_random_user_agent():
    """Return a random modern user agent"""
    agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0"
    ]
    return random.choice(agents)

def get_video_id(url: str) -> str:
    """Extract video ID from YouTube URL"""
    if 'youtu.be' in url:
        return url.split('/')[-1]
    elif 'youtube.com' in url:
        if 'v=' in url:
            return url.split('v=')[1].split('&')[0]
    return None

async def get_video_info_from_api(video_id: str) -> dict:
    """Get video info using YouTube Data API"""
    try:
        request = youtube.videos().list(
            part="snippet,contentDetails,statistics",
            id=video_id
        )
        response = request.execute()
        
        if not response['items']:
            raise ValueError("Video not found")
            
        video = response['items'][0]
        return {
            'title': video['snippet']['title'],
            'duration': video['contentDetails']['duration'],
            'thumbnail': video['snippet']['thumbnails']['high']['url'],
            'uploader': video['snippet']['channelTitle'],
            'view_count': int(video['statistics']['viewCount']),
            'id': video_id
        }
    except HttpError as e:
        logger.error(f"YouTube API error: {str(e)}")
        raise HTTPException(status_code=500, detail="Error fetching video info from YouTube API")

async def get_video_info(url: str) -> dict:
    """Get video info with fallback methods"""
    video_id = get_video_id(url)
    if not video_id:
        raise ValueError("Invalid YouTube URL")
        
    # Check cache first
    cache_file = CACHE_DIR / f"{video_id}.json"
    if cache_file.exists():
        try:
            with open(cache_file, 'r') as f:
                cached_info = json.load(f)
                if time.time() - cached_info.get('cache_time', 0) < 3600:  # 1 hour cache
                    return cached_info['info']
        except Exception:
            pass
    
    try:
        # Try yt-dlp first
        opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,
            'skip_download': True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            result = {
                'title': info.get('title', 'Untitled'),
                'duration': info.get('duration', 0),
                'thumbnail': info.get('thumbnail', ''),
                'uploader': info.get('uploader', 'Unknown'),
                'view_count': info.get('view_count', 0),
                'id': info.get('id', '')
            }
    except Exception as e:
        logger.warning(f"yt-dlp failed: {str(e)}, trying YouTube API")
        # Fallback to YouTube API
        result = await get_video_info_from_api(video_id)
    
    # Cache the result
    try:
        with open(cache_file, 'w') as f:
            json.dump({
                'info': result,
                'cache_time': time.time()
            }, f)
    except Exception:
        pass
    
    return result

def get_ydl_opts(output_path: str, quality: str) -> dict:
    quality_map = {
        "high": "192",
        "medium": "128",
        "low": "96"
    }
    
    return {
        'format': 'bestaudio/best',
        'outtmpl': output_path,
        'ffmpeg_location': FFMPEG_PATH,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': quality_map.get(quality, "192"),
        }],
        'quiet': False,
        'no_warnings': False,
        'user_agent': get_random_user_agent(),
        'retries': 10,
        'fragment_retries': 10,
        'skip_unavailable_fragments': True,
        'ignoreerrors': False,
        'cookiefile': None,
        'cookiesfrombrowser': None,
        'referer': 'https://www.youtube.com/',
        'http_headers': {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'User-Agent': get_random_user_agent(),
        },
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web'],
                'skip': ['hls', 'dash', 'translated_subs'],
                'formats': 'bestaudio/best'
            }
        },
        'throttledratelimit': 4194304,  # 4 MB/s
        'noresizebuffer': True,
        'socket_timeout': 10,
        'source_address': '0.0.0.0',
        'buffersize': 2048,
        'retry_sleep': 2,
        'retry_sleep_functions': {
            'http': lambda x: 2,
            'fragment': lambda x: 2,
        },
        'concurrent_fragment_downloads': 5,
        'file_access_retries': 3,
        'extractor_retries': 3,
        'fragment_retries': 5,
        'retries': 5,
        'skip_download': False,
        'keepvideo': False,
        'writethumbnail': False,
        'writesubtitles': False,
        'writeautomaticsub': False,
        'postprocessor_args': [
            '-ar', '44100',
            '-ac', '2',
            '-b:a', f'{quality_map.get(quality, "192")}k'
        ],
        'nocheckcertificate': True,
        'prefer_insecure': True,
        'geo_bypass': True,
        'geo_verification_proxy': None,
        'noprogress': False,
        'progress_with_newline': True,
        'updatetime': False,
        'writedescription': False,
        'writeinfojson': False,
        'writethumbnail': False,
        'writesubtitles': False,
        'writeautomaticsub': False,
        'skip_download': False,
        'keepvideo': False,
        'noplaylist': True,
        'extract_flat': False,
        'force_generic_extractor': False,
        'allow_unplayable_formats': True,
        'format_sort': ['res', 'ext:mp4:m4a', 'size', 'br', 'asr'],
        'format_selection': 'bestaudio/best',
        'postprocessor_hooks': [],
        'merge_output_format': 'mp3',
        'prefer_ffmpeg': True,
        'keep_fragments': False,
        'hls_prefer_native': True,
        'hls_use_mpegts': True,
        'external_downloader': None,
        'external_downloader_args': None,
        'postprocessor_args': [
            '-ar', '44100',
            '-ac', '2',
            '-b:a', f'{quality_map.get(quality, "192")}k'
        ],
    }

async def convert_video(token: str, url: str, quality: str):
    try:
        if url in current_conversions:
            raise HTTPException(status_code=400, detail="This video is already being converted")
            
        current_conversions.add(url)
        
        # Get video info with fallback
        info = await get_video_info(url)
        sanitized_title = sanitize_filename(info['title'])
        base_filename = f"{sanitized_title}-{info['id']}"
        output_path = str(DOWNLOAD_DIR / base_filename)
        
        jobs[token] = {
            'status': 'processing',
            'url': url,
            'filename': base_filename + ".mp3",
            'progress': 'Starting download...',
            'video_title': info['title'],
            'start_time': time.time()
        }
        
        ydl_opts = get_ydl_opts(output_path, quality)
        
        def progress_hook(d):
            if d['status'] == 'downloading':
                percent = d.get('_percent_str', '0%')
                speed = d.get('_speed_str', 'N/A')
                eta = d.get('_eta_str', 'N/A')
                jobs[token]['progress'] = f"Downloading: {percent} at {speed} (ETA: {eta})"
            elif d['status'] == 'finished':
                jobs[token]['progress'] = "Converting to MP3..."
        
        ydl_opts['progress_hooks'] = [progress_hook]
        
        # Try multiple download attempts with different configurations
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    await asyncio.get_event_loop().run_in_executor(None, lambda: ydl.download([url]))
                break
            except Exception as e:
                if attempt == max_attempts - 1:
                    raise
                logger.warning(f"Download attempt {attempt + 1} failed: {str(e)}")
                # Modify options for next attempt
                ydl_opts['format'] = 'bestaudio/best' if attempt == 0 else 'bestaudio'
                ydl_opts['extractor_args']['youtube']['player_client'] = ['android'] if attempt == 1 else ['web']
                await asyncio.sleep(2)  # Wait before retry
        
        # Find and process the output file
        actual_file = None
        for ext in ['.mp3', '.m4a', '.webm', '.part']:
            candidate = output_path + ext
            if os.path.exists(candidate):
                actual_file = candidate
                break
        
        if not actual_file:
            if os.path.exists(output_path):
                actual_file = output_path
            else:
                raise FileNotFoundError("No output file found after conversion")
        
        if not actual_file.endswith('.mp3'):
            mp3_file = actual_file.rsplit('.', 1)[0] + '.mp3'
            os.rename(actual_file, mp3_file)
            actual_file = mp3_file
        
        file_size = os.path.getsize(actual_file)
        if file_size < 100 * 1024:
            os.remove(actual_file)
            raise ValueError(f"File too small ({file_size} bytes), likely incomplete")
        
        conversion_time = time.time() - jobs[token]['start_time']
        
        jobs[token] = {
            'status': 'completed',
            'url': url,
            'filename': os.path.basename(actual_file),
            'file_path': actual_file,
            'expires_at': time.time() + (FILE_EXPIRY_MINUTES * 60),
            'video_title': info['title'],
            'file_size': file_size,
            'conversion_time': f"{conversion_time:.1f} seconds"
        }
        logger.info(f"Conversion completed: {actual_file} ({file_size/1024:.1f} KB) in {conversion_time:.1f} seconds")
        
    except Exception as e:
        logger.error(f"Conversion failed: {str(e)}", exc_info=True)
        jobs[token] = {
            'status': 'failed',
            'url': url,
            'error': str(e),
            'video_title': info.get('title', 'Unknown') if 'info' in locals() else 'Unknown'
        }
    finally:
        current_conversions.discard(url)

async def cleanup_old_files():
    """Delete files older than expiry time"""
    while True:
        try:
            current_time = time.time()
            for token, job in list(jobs.items()):
                if job.get('expires_at', 0) < current_time:
                    file_path = job.get('file_path')
                    if file_path and os.path.exists(file_path):
                        os.remove(file_path)
                        logger.info(f"Deleted expired file: {file_path}")
                    if job['status'] != 'processing':
                        del jobs[token]
        except Exception as e:
            logger.error(f"Cleanup error: {str(e)}")
        await asyncio.sleep(60)

# Endpoints
@app.post("/convert")
async def start_conversion(request: ConversionRequest, background_tasks: BackgroundTasks):
    # Check if URL is already being converted
    if request.url in current_conversions:
        existing_token = next((k for k, v in jobs.items() if v.get('url') == request.url and v.get('status') == 'processing'), None)
        if existing_token:
            return {"token": existing_token, "message": "Conversion already in progress"}
    
    token = str(uuid.uuid4())
    jobs[token] = {'status': 'queued', 'url': str(request.url)}
    
    async def run_conversion():
        async with conversion_semaphore:
            await convert_video(token, str(request.url), request.quality)
    
    background_tasks.add_task(run_conversion)
    return {"token": token, "message": "Conversion started"}

@app.get("/status/{token}")
async def get_status(token: str):
    job = jobs.get(token)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job

@app.get("/download/{token}")
async def download_file(token: str):
    job = jobs.get(token)
    if not job or job.get('status') != 'completed':
        raise HTTPException(status_code=404, detail="File not ready or expired")
    
    file_path = job.get('file_path')
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    
    # Update expiry time on download
    jobs[token]['expires_at'] = time.time() + (FILE_EXPIRY_MINUTES * 60)
    
    return FileResponse(
        file_path,
        media_type='audio/mpeg',
        filename=job['filename'],
        headers={
            'Content-Disposition': f'attachment; filename="{job["filename"]}"'
        }
    )

@app.post("/video/metadata")
async def get_metadata(request: VideoMetadataRequest):
    try:
        return await get_video_info(str(request.url))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/health")
async def health_check():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)