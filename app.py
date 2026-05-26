"""
Summarify Pro — Backend API Server
AI Document Summarizer — International Edition
Powered by Zhipu AI GLM-4-Flash
PayPal-powered Premium subscription (monthly)
"""

import os
import re
import json
import hashlib
import secrets
import uuid
import datetime
import ipaddress
import socket
from functools import wraps
from collections import defaultdict
from threading import Lock
import time
import logging
from urllib.parse import urlparse

from flask import Flask, request, jsonify, send_from_directory, render_template, Response, stream_with_context
from flask_cors import CORS
import jwt
import requests
import PyPDF2
from docx import Document
from bs4 import BeautifulSoup
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import bcrypt

# Supabase
from supabase import create_client, Client



# YouTube Transcript
from youtube_transcript_api import YouTubeTranscriptApi

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

# ── App Initialization ────────────────────────────────────────────────

app = Flask(__name__, static_folder='static', static_url_path='')
CORS_ALLOWED_ORIGINS = os.getenv('CORS_ORIGINS', '').strip()
if CORS_ALLOWED_ORIGINS:
    _origins = [o.strip() for o in CORS_ALLOWED_ORIGINS.split(',') if o.strip()]
    CORS(app, resources={r"/api/*": {"origins": _origins, "allow_headers": ["Content-Type", "Authorization", "X-Anon-Id"], "supports_credentials": True}})
else:
    CORS(app, resources={r"/api/*": {"origins": "*", "allow_headers": ["Content-Type", "Authorization", "X-Anon-Id"]}})

app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', secrets.token_hex(32))
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB
app.config['UPLOAD_FOLDER'] = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'uploads'
)

try:
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
except OSError:
    # Vercel / serverless: read-only filesystem — fall back to /tmp
    app.config['UPLOAD_FOLDER'] = '/tmp/uploads'
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# ── AI Config (from .env) ─────────────────────────────────────────────

AI_PROVIDER    = os.getenv('AI_PROVIDER', 'zhipu')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')
OPENAI_BASE_URL = os.getenv('OPENAI_BASE_URL', 'https://open.bigmodel.cn/api/paas/v4')
OPENAI_MODEL   = os.getenv('OPENAI_MODEL', 'glm-4-flash')

# ── Supabase Client ───────────────────────────────────────────────────

SUPABASE_URL = os.getenv('PUBLIC_SUPABASE_URL', '')
SUPABASE_KEY = os.getenv(
    'SUPABASE_SERVICE_ROLE_KEY', ''
).strip() or os.getenv('PUBLIC_SUPABASE_ANON_KEY', '').strip()

_supabase_client: Client | None = None

def get_supabase() -> Client:
    """Lazy-initialized Supabase client."""
    global _supabase_client
    if _supabase_client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError(
                'Supabase not configured. Set PUBLIC_SUPABASE_URL and '
                'SUPABASE_SERVICE_ROLE_KEY (or PUBLIC_SUPABASE_ANON_KEY) in .env.'
            )
        _supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase_client

# ── PayPal Client ──────────────────────────────────────────────────────

PAYPAL_CLIENT_ID = os.getenv('PAYPAL_CLIENT_ID', '')
PAYPAL_CLIENT_SECRET = os.getenv('PAYPAL_CLIENT_SECRET', '')
PAYPAL_MODE = os.getenv('PAYPAL_MODE', 'sandbox')  # 'sandbox' or 'live'

PAYPAL_API_BASE = os.getenv('PAYPAL_API_BASE', '')
PAYPAL_BASE = (
    PAYPAL_API_BASE if PAYPAL_API_BASE
    else 'https://api-m.sandbox.paypal.com' if PAYPAL_MODE == 'sandbox'
    else 'https://api-m.paypal.com'
)

_paypal_token_cache = {'token': None, 'expires_at': 0}


def get_paypal_token() -> str:
    """OAuth 2.0 → access_token, cached until near expiry."""
    now = time.time()
    if _paypal_token_cache['token'] and now < _paypal_token_cache['expires_at'] - 60:
        return _paypal_token_cache['token']
    resp = requests.post(
        f'{PAYPAL_BASE}/v1/oauth2/token',
        auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
        data={'grant_type': 'client_credentials'},
        headers={'Accept': 'application/json'}
    )
    resp.raise_for_status()
    data = resp.json()
    _paypal_token_cache['token'] = data['access_token']
    _paypal_token_cache['expires_at'] = now + data.get('expires_in', 32400)
    return data['access_token']


def paypal_headers():
    """Return auth + content-type headers for PayPal API."""
    return {
        'Authorization': f'Bearer {get_paypal_token()}',
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    }

# ── Subscription Helpers ───────────────────────────────────────────────

def get_active_subscription(user_id: str) -> dict | None:
    """Get the user's active subscription (any provider).
    Returns None if no subscription or if subscriptions table doesn't exist yet."""
    sb = get_supabase()
    try:
        res = sb.table('subscriptions').select('*') \
            .eq('user_id', user_id) \
            .eq('status', 'active') \
            .order('created_at', desc=True) \
            .limit(1) \
            .execute()
        return res.data[0] if res.data else None
    except Exception as e:
        logging.warning(f"get_active_subscription failed (user={user_id}): {e}")
        return None  # Graceful degradation


def sync_user_plan(user_id: str) -> str:
    """Sync users.plan based on whether any active subscription exists."""
    sb = get_supabase()
    try:
        active = sb.table('subscriptions').select('id') \
            .eq('user_id', user_id) \
            .eq('status', 'active') \
            .execute()
        plan = 'premium' if active.data else 'free'
        sb.table('users').update({'plan': plan}).eq('id', user_id).execute()
        return plan
    except Exception as e:
        logging.warning(f"sync_user_plan failed (user={user_id}): {e}")
        # Fallback: read current plan from users table
        res = sb.table('users').select('plan').eq('id', user_id).execute()
        return res.data[0].get('plan', 'free') if res.data else 'free'


def get_subscription_info(user_id: str) -> dict:
    """Return subscription summary for user info responses."""
    sub = get_active_subscription(user_id)
    if not sub:
        return {'plan': 'free', 'subscription': None}
    return {
        'plan': sub.get('plan_tier', 'premium'),
        'subscription': {
            'provider': sub.get('provider'),
            'status': sub.get('status'),
            'current_period_end': sub.get('current_period_end'),
            'cancel_at_period_end': sub.get('cancel_at_period_end', False)
        }
    }

# ── Auth Utilities ─────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """Hash password with bcrypt."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def verify_password(password: str, password_hash: str) -> bool:
    """Verify password against hash. Supports both bcrypt and legacy SHA-256."""
    if password_hash.startswith('$2b$') or password_hash.startswith('$2a$'):
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    # Legacy SHA-256 fallback
    return hashlib.sha256(password.encode()).hexdigest() == password_hash

def generate_token(user_id: str) -> str:
    payload = {
        'user_id': user_id,
        'exp': datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)
    }
    return jwt.encode(payload, app.config['SECRET_KEY'], algorithm='HS256')

def verify_token(token: str):
    try:
        payload = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
        return payload.get('user_id')
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization', '')
        token = auth_header.replace('Bearer ', '').strip()
        if not token:
            return jsonify({'error': 'Authorization header required'}), 401
        user_id = verify_token(token)
        if user_id is None:
            return jsonify({'error': 'Invalid or expired token'}), 401
        return f(user_id, *args, **kwargs)
    return decorated

def flex_auth(f):
    """
    Flexible auth: allows both authenticated (JWT) and anonymous (X-Anon-Id) users.
    Passes (user_id, anon_id, *args, **kwargs) to the endpoint.
    - user_id: str if authenticated, None if anonymous
    - anon_id: str from X-Anon-Id header, None if authenticated
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        # Try JWT auth first
        auth_header = request.headers.get('Authorization', '')
        token = auth_header.replace('Bearer ', '').strip()
        if token:
            user_id = verify_token(token)
            if user_id:
                return f(user_id, None, *args, **kwargs)
        # Anonymous — require X-Anon-Id header
        anon_id = request.headers.get('X-Anon-Id', '').strip()
        if not anon_id or len(anon_id) > 64:
            return jsonify({'error': 'Login required or provide X-Anon-Id header'}), 401
        if not re.match(r'^[a-zA-Z0-9\-]+$', anon_id):
            return jsonify({'error': 'Invalid X-Anon-Id format'}), 400
        return f(None, anon_id, *args, **kwargs)
    return decorated

# ── Rate Limiter (in-memory IP-based) ──────────────────────────────

_rate_limits: dict = defaultdict(list)
_rate_lock = Lock()

def rate_limit(max_requests: int = 10, window_seconds: int = 60):
    """Limit requests per IP within a time window."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            ip = request.remote_addr or 'unknown'
            now = time.time()
            with _rate_lock:
                timestamps = _rate_limits[ip]
                timestamps[:] = [t for t in timestamps if now - t < window_seconds]
                if len(timestamps) >= max_requests:
                    wait = int(window_seconds - (now - timestamps[0]))
                    return jsonify({
                        'error': f'Too many requests. Please wait {wait} seconds.'
                    }), 429
                timestamps.append(now)
            return f(*args, **kwargs)
        return decorated
    return decorator

# ── Rate Limiting ───────────────────────────────────────────────────────

def check_daily_usage(user_id: str):
    """
    Check remaining usage WITHOUT incrementing.
    Returns (allowed: bool, remaining: int or None for unlimited).
    """
    sb = get_supabase()
    res = sb.table('users').select('*').eq('id', user_id).execute()
    users = res.data or []
    if not users:
        return False, 0
    user = users[0]
    if user.get('plan') == 'premium':
        return True, None  # None = unlimited

    today = datetime.datetime.now().strftime('%Y-%m-%d')
    last_date = (user.get('last_usage_date') or '')
    daily_count = user.get('daily_usage_count', 0)

    if last_date != today:
        return True, 10  # fresh day, 10 remaining

    remaining = 10 - daily_count
    if remaining > 0:
        return True, remaining
    return False, 0


def record_daily_usage(user_id: str):
    """
    Record one usage for a logged-in user (only for free plan).
    Returns new remaining count (or None for premium).
    """
    sb = get_supabase()
    res = sb.table('users').select('plan,daily_usage_count,last_usage_date').eq('id', user_id).execute()
    if not res.data:
        return 0
    user = res.data[0]
    if user.get('plan') == 'premium':
        return None

    today = datetime.datetime.now().strftime('%Y-%m-%d')
    last_date = (user.get('last_usage_date') or '')

    if last_date != today:
        sb.table('users').update({
            'daily_usage_count': 1,
            'last_usage_date': today
        }).eq('id', user_id).execute()
        return 9

    new_count = user.get('daily_usage_count', 0) + 1
    sb.table('users').update({'daily_usage_count': new_count}).eq('id', user_id).execute()
    return 10 - new_count

# ── Anonymous Usage Tracking ──────────────────────────────────────────

ANON_DAILY_LIMIT = 10

def check_anon_usage(anon_id: str, ip: str):
    """
    Check anonymous daily usage WITHOUT incrementing.
    Returns (allowed: bool, remaining: int).
    """
    sb = get_supabase()
    today = datetime.datetime.now().strftime('%Y-%m-%d')
    try:
        res = sb.table('anonymous_usage').select('*').eq('anon_id', anon_id).eq('usage_date', today).execute()
        if res.data:
            record = res.data[0]
            remaining = ANON_DAILY_LIMIT - record['usage_count']
            if remaining > 0:
                return True, remaining
            return False, 0
        else:
            return True, ANON_DAILY_LIMIT  # no usage yet today
    except Exception as e:
        print(f'[anon_usage] Error checking usage for {anon_id}: {e}')
        return True, ANON_DAILY_LIMIT


def record_anon_usage(anon_id: str, ip: str):
    """
    Record one anonymous usage after a successful operation.
    Returns new remaining count.
    """
    sb = get_supabase()
    today = datetime.datetime.now().strftime('%Y-%m-%d')
    try:
        res = sb.table('anonymous_usage').select('*').eq('anon_id', anon_id).eq('usage_date', today).execute()
        if res.data:
            record = res.data[0]
            new_count = record['usage_count'] + 1
            sb.table('anonymous_usage').update({'usage_count': new_count}).eq('id', record['id']).execute()
            return ANON_DAILY_LIMIT - new_count
        else:
            sb.table('anonymous_usage').insert({
                'anon_id': anon_id,
                'ip_address': ip,
                'usage_date': today,
                'usage_count': 1
            }).execute()
            return ANON_DAILY_LIMIT - 1
    except Exception as e:
        print(f'[anon_usage] Error recording usage for {anon_id}: {e}')
        return ANON_DAILY_LIMIT - 1


def check_usage(user_id=None, anon_id=None, ip=None):
    """
    Unified usage check — authenticated or anonymous.
    Checks remaining WITHOUT incrementing.
    Returns (allowed: bool, remaining: int or None for unlimited).
    """
    if user_id:
        return check_daily_usage(user_id)
    if anon_id:
        return check_anon_usage(anon_id, ip or request.remote_addr or 'unknown')
    return False, 0


def record_usage(user_id=None, anon_id=None, ip=None):
    """
    Record one usage after a successful operation.
    Returns new remaining count (or None for premium/unlimited).
    """
    if user_id:
        return record_daily_usage(user_id)
    if anon_id:
        return record_anon_usage(anon_id, ip or request.remote_addr or 'unknown')
    return 0

# ── AI Client ──────────────────────────────────────────────────────────

def call_ai(messages: list, temperature: float = 0.5, max_tokens: int = 4096):
    if not OPENAI_API_KEY:
        return None, 'AI provider API key not configured — set OPENAI_API_KEY in your .env file'

    headers = {
        'Authorization': f'Bearer {OPENAI_API_KEY}',
        'Content-Type': 'application/json'
    }
    payload = {
        'model': OPENAI_MODEL,
        'messages': messages,
        'temperature': temperature,
        'max_tokens': max_tokens
    }

    try:
        resp = requests.post(
            f'{OPENAI_BASE_URL}/chat/completions',
            json=payload,
            headers=headers,
            timeout=180
        )
        data = resp.json()
        if 'choices' in data and len(data['choices']) > 0:
            return data['choices'][0]['message']['content'], None
        err = data.get('error', {})
        return None, err.get('message', f'API error (HTTP {resp.status_code})')
    except requests.exceptions.Timeout:
        return None, 'Request timed out after 180 seconds — try shorter text or use summarize first'
    except requests.exceptions.ConnectionError:
        return None, 'Cannot connect to AI API — check OPENAI_BASE_URL in .env'
    except Exception as e:
        return None, f'Unexpected error: {str(e)}'


def call_ai_stream(messages: list, temperature: float = 0.5, max_tokens: int = 1024):
    """Stream AI response as SSE chunks — yields JSON strings ready for SSE."""
    if not OPENAI_API_KEY:
        yield json.dumps({'error': 'AI provider API key not configured'})
        return

    headers = {
        'Authorization': f'Bearer {OPENAI_API_KEY}',
        'Content-Type': 'application/json'
    }
    payload = {
        'model': OPENAI_MODEL,
        'messages': messages,
        'temperature': temperature,
        'max_tokens': max_tokens,
        'stream': True,
        'thinking': {"type": "disabled"}
    }

    try:
        resp = requests.post(
            f'{OPENAI_BASE_URL}/chat/completions',
            json=payload,
            headers=headers,
            timeout=180,
            stream=True
        )

        if resp.status_code != 200:
            try:
                err = resp.json()
                yield json.dumps({'error': err.get('error', {}).get('message', f'API error (HTTP {resp.status_code})')})
            except Exception:
                yield json.dumps({'error': f'API error (HTTP {resp.status_code})'})
            return

        for line in resp.iter_lines():
            line = line.decode('utf-8').strip()
            if not line:
                continue
            if line == 'data: [DONE]':
                break
            if line.startswith('data: '):
                line = line[6:]
            try:
                chunk = json.loads(line)
                if 'choices' in chunk and len(chunk['choices']) > 0:
                    delta = chunk['choices'][0].get('delta', {})
                    content = delta.get('content', '')
                    reasoning = delta.get('reasoning_content', '')
                    if content:
                        yield json.dumps({'content': content})
                    elif reasoning:
                        # For thinking models, also stream the reasoning if no content yet
                        yield json.dumps({'content': '[Thinking] ' + reasoning})
            except (json.JSONDecodeError, KeyError):
                continue

    except requests.exceptions.Timeout:
        yield json.dumps({'error': 'Request timed out after 180 seconds'})
    except requests.exceptions.ConnectionError:
        yield json.dumps({'error': 'Cannot connect to AI API — check OPENAI_BASE_URL'})
    except Exception as e:
        yield json.dumps({'error': f'Unexpected error: {str(e)}'})

# ── Text Extractors ────────────────────────────────────────────────────

def extract_pdf_text(filepath: str) -> str:
    text_parts = []
    with open(filepath, 'rb') as f:
        reader = PyPDF2.PdfReader(f)
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
    return '\n\n'.join(text_parts).strip()

def extract_docx_text(filepath: str) -> str:
    doc = Document(filepath)
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    return '\n'.join(paragraphs).strip()

def extract_url_text(url: str) -> str:
    # SSRF protection: block private/internal IP addresses
    parsed = urlparse(url)
    hostname = parsed.hostname
    if hostname:
        try:
            resolved = socket.gethostbyname(hostname)
            ip = ipaddress.ip_address(resolved)
            private_ranges = [
                ipaddress.ip_network('10.0.0.0/8'),
                ipaddress.ip_network('172.16.0.0/12'),
                ipaddress.ip_network('192.168.0.0/16'),
                ipaddress.ip_network('127.0.0.0/8'),
                ipaddress.ip_network('169.254.0.0/16'),
                ipaddress.ip_network('0.0.0.0/8'),
                ipaddress.ip_network('fc00::/7'),
                ipaddress.ip_network('fe80::/10'),
                ipaddress.ip_network('::1/128'),
            ]
            if any(ip in net for net in private_ranges):
                raise ValueError('Access to private/internal networks is not allowed')
        except (socket.gaierror, ValueError) as e:
            if 'private' in str(e) or 'internal' in str(e):
                raise

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()

    # Fix encoding for Chinese/non-UTF8 sites (GBK/GB2312/etc)
    content_type = resp.headers.get('Content-Type', '')
    if 'charset' not in content_type.lower():
        resp.encoding = resp.apparent_encoding

    # ── PDF content ──
    if 'application/pdf' in content_type or url.lower().endswith('.pdf'):
        import io
        pdf_reader = PyPDF2.PdfReader(io.BytesIO(resp.content))
        pages = []
        for page in pdf_reader.pages:
            page_text = page.extract_text()
            if page_text:
                pages.append(page_text)
        text = '\n'.join(pages)
        return text.strip()[:30000]

    # ── HTML content ──
    soup = BeautifulSoup(resp.text, 'html.parser')
    for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside', 'iframe', 'noscript']):
        tag.decompose()
    text = soup.get_text(separator='\n')
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    return '\n'.join(lines)[:30000]

# ── Page Routes ───────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html',
                           paypal_client_id=PAYPAL_CLIENT_ID,
                           paypal_mode=PAYPAL_MODE,
                           paypal_plan_id=os.getenv('PAYPAL_PLAN_ID', ''))

@app.route('/login')
def login_page():
    return render_template('login.html')

@app.route('/register')
def register_page():
    return render_template('register.html')

@app.route('/privacy')
def privacy_page():
    return render_template('privacy.html')

@app.route('/ai-summarize')
def ai_summarize_page():
    return render_template('ai-summarize.html')

@app.route('/pdf-summarizer')
def pdf_summarizer_page():
    return render_template('pdf-summarizer.html')

@app.route('/blog/')
def blog_index():
    articles = [
        {
            'slug': 'how-to-summarize-pdf-with-ai',
            'title': 'How to Summarize a PDF with AI in 30 Seconds — Free Guide',
            'excerpt': 'Learn how to use AI to summarize PDF documents instantly. Step-by-step guide for students, researchers, and professionals.',
            'date': '2026-05-20',
            'category': 'Tutorial',
            'keywords': 'summarize PDF with AI, AI PDF summarizer free, summarize PDF online'
        },
        {
            'slug': 'best-youtube-video-summarizers-2026',
            'title': '5 Best Free YouTube Video Summarizers in 2026 (Tested & Ranked)',
            'excerpt': 'We tested the top free YouTube summarizer tools. See which one delivers the most accurate summaries for lectures, podcasts, and tutorials.',
            'date': '2026-05-22',
            'category': 'Comparison',
            'keywords': 'best YouTube video summarizer, free YouTube summary AI, YouTube to text summary'
        },
        {
            'slug': 'summarize-research-papers-fast',
            'title': 'How to Understand Research Papers Without Reading Every Page',
            'excerpt': 'AI tools can extract key points from academic papers in minutes. Here is how grad students and researchers save hours every week.',
            'date': '2026-05-24',
            'category': 'Productivity',
            'keywords': 'summarize research paper AI, read papers faster, academic paper summarizer'
        }
    ]
    return render_template('blog/index.html', articles=articles)

@app.route('/blog/<slug>')
def blog_post(slug):
    template_name = f'blog/{slug}.html'
    try:
        return render_template(template_name)
    except:
        return render_template('blog/index.html', articles=[
            {'slug': 'how-to-summarize-pdf-with-ai', 'title': 'How to Summarize a PDF with AI in 30 Seconds', 'excerpt': 'Learn how to use AI to summarize PDF documents instantly.', 'date': '2026-05-20', 'category': 'Tutorial', 'keywords': ''},
            {'slug': 'best-youtube-video-summarizers-2026', 'title': '5 Best Free YouTube Video Summarizers in 2026', 'excerpt': 'We tested the top free YouTube summarizer tools.', 'date': '2026-05-22', 'category': 'Comparison', 'keywords': ''},
            {'slug': 'summarize-research-papers-fast', 'title': 'How to Understand Research Papers Without Reading Every Page', 'excerpt': 'AI tools can extract key points from academic papers in minutes.', 'date': '2026-05-24', 'category': 'Productivity', 'keywords': ''}
        ]), 404

# ── Chinese (/zh/) Pages ──────────────────────────────────────────────

@app.route('/zh/')
def zh_index():
    return render_template('zh/index.html')

@app.route('/zh/pdf-summarizer')
def zh_pdf():
    return render_template('zh/pdf-summarizer.html')

@app.route('/zh/word-summarizer')
def zh_word():
    return render_template('zh/word-summarizer.html')

@app.route('/zh/url-summarizer')
def zh_url():
    return render_template('zh/url-summarizer.html')

# ── Auth Endpoints ─────────────────────────────────────────────────────

@app.route('/api/user/register', methods=['POST'])
@rate_limit(max_requests=10, window_seconds=60)
def register():
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    password = (data.get('password') or '').strip()

    if not email or not password:
        return jsonify({'error': 'Email and password are required'}), 400
    if len(password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        return jsonify({'error': 'Invalid email format'}), 400

    sb = get_supabase()

    existing = sb.table('users').select('id,email').eq('email', email).execute()
    if existing.data:
        return jsonify({'error': 'Email already registered'}), 409

    password_hash = hash_password(password)

    try:
        insert_res = sb.table('users').insert({
            'email': email,
            'password_hash': password_hash,
            'plan': 'free',
            'daily_usage_count': 0,
            'last_usage_date': None
        }).execute()
    except Exception as e:
        return jsonify({'error': f'Failed to create user: {str(e)}'}), 500

    if not insert_res.data:
        return jsonify({'error': 'Failed to create user'}), 500

    new_user = insert_res.data[0]
    token = generate_token(new_user['id'])

    sub_info = get_subscription_info(new_user['id'])
    return jsonify({
        'token': token,
        'user': {
            'id': new_user['id'],
            'email': new_user['email'],
            'plan': sub_info['plan'],
            'subscription': sub_info['subscription']
        }
    })


@app.route('/api/user/login', methods=['POST'])
@rate_limit(max_requests=10, window_seconds=60)
def login():
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    password = (data.get('password') or '').strip()

    if not email or not password:
        return jsonify({'error': 'Email and password are required'}), 400

    sb = get_supabase()

    res = sb.table('users').select('*').eq('email', email).execute()
    user_data = res.data

    if not user_data:
        return jsonify({'error': 'Invalid email or password'}), 401

    user = user_data[0]

    if not verify_password(password, user['password_hash']):
        return jsonify({'error': 'Invalid email or password'}), 401

    # Auto-migrate: upgrade SHA-256 hash to bcrypt on successful login
    if not user['password_hash'].startswith('$2b$') and not user['password_hash'].startswith('$2a$'):
        new_hash = hash_password(password)
        sb.table('users').update({'password_hash': new_hash}).eq('id', user['id']).execute()

    token = generate_token(user['id'])
    sub_info = get_subscription_info(user['id'])

    return jsonify({
        'token': token,
        'user': {
            'id': user['id'],
            'email': user['email'],
            'plan': sub_info['plan'],
            'subscription': sub_info['subscription']
        }
    })


@app.route('/api/user/usage', methods=['GET'])
@flex_auth
def get_usage(user_id, anon_id):
    if user_id:
        sb = get_supabase()
        res = sb.table('users').select(
            'plan, daily_usage_count, last_usage_date'
        ).eq('id', user_id).execute()

        if not res.data:
            return jsonify({'error': 'User not found'}), 404

        user = res.data[0]
        today = datetime.datetime.now().strftime('%Y-%m-%d')

        # Check subscriptions table for premium (not just users.plan)
        active_sub = get_active_subscription(user_id)
        if active_sub:
            return jsonify({
                'plan': 'premium',
                'daily_limit': None,
                'used_today': 0,
                'remaining': None,
                'subscription': {
                    'provider': active_sub['provider'],
                    'status': active_sub['status'],
                    'current_period_end': active_sub.get('current_period_end'),
                    'cancel_at_period_end': active_sub.get('cancel_at_period_end', False)
                }
            })

        if user.get('last_usage_date') != today:
            used, remaining = 0, 10
        else:
            used = user.get('daily_usage_count', 0)
            remaining = max(0, 10 - used)

        return jsonify({
            'plan': 'free',
            'daily_limit': 10,
            'used_today': used,
            'remaining': remaining
        })
    else:
        # Anonymous user
        sb = get_supabase()
        today = datetime.datetime.now().strftime('%Y-%m-%d')
        res = sb.table('anonymous_usage').select('usage_count').eq('anon_id', anon_id).eq('usage_date', today).execute()
        used = res.data[0]['usage_count'] if res.data else 0
        return jsonify({
            'plan': 'anonymous',
            'daily_limit': ANON_DAILY_LIMIT,
            'used_today': used,
            'remaining': max(0, ANON_DAILY_LIMIT - used)
        })


# ── Feedback ───────────────────────────────────────────────────────────

@app.route('/api/user/feedback', methods=['POST'])
def submit_feedback():
    data = request.get_json() or {}
    content = (data.get('content', '') or '').strip()
    if not content:
        return jsonify({'error': 'Feedback content is required'}), 400
    if len(content) > 2000:
        return jsonify({'error': 'Feedback too long (max 2000 characters)'}), 400

    # Optional: attach user_id if logged in
    user_id = None
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        try:
            token = auth_header.split(' ', 1)[1]
            payload = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
            user_id = payload.get('user_id')
        except Exception:
            pass

    sb = get_supabase()
    insert_data = {'content': content}
    if user_id:
        insert_data['user_id'] = user_id
    sb.table('feedback').insert(insert_data).execute()

    return jsonify({'success': True, 'message': 'Thank you for your feedback!'})


# ── PayPal Endpoints ───────────────────────────────────────────────────

@app.route('/api/paypal/create-subscription', methods=['POST'])
@require_auth
def create_paypal_subscription(user_id):
    """Create a PayPal subscription for the user."""
    if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
        return jsonify({'error': 'PayPal not configured on server'}), 500

    plan_id = os.getenv('PAYPAL_PLAN_ID', '')
    if not plan_id:
        return jsonify({'error': 'PayPal Plan ID not configured'}), 500

    sb = get_supabase()
    res = sb.table('users').select('email').eq('id', user_id).execute()
    if not res.data:
        return jsonify({'error': 'User not found'}), 404
    email = res.data[0]['email']

    origin = request.headers.get('Origin', 'http://localhost:5000')

    try:
        resp = requests.post(
            f'{PAYPAL_BASE}/v1/billing/subscriptions',
            headers=paypal_headers(),
            json={
                'plan_id': plan_id,
                'custom_id': user_id,
                'subscriber': {
                    'name': {'given_name': 'User'},
                    'email_address': email
                },
                'application_context': {
                    'brand_name': 'Summarify Pro',
                    'user_action': 'SUBSCRIBE_NOW',
                    'shipping_preference': 'NO_SHIPPING',
                    'return_url': f'{origin}/?checkout=success',
                    'cancel_url': f'{origin}/?checkout=cancelled'
                }
            }
        )
        resp.raise_for_status()
        data = resp.json()

        # Extract approval URL from HATEOAS links
        approve_link = next(
            (l['href'] for l in data.get('links', []) if l['rel'] == 'approve'),
            None
        )
        if not approve_link:
            return jsonify({'error': 'No approval URL from PayPal'}), 500

        return jsonify({
            'subscription_id': data['id'],
            'approve_url': approve_link
        })
    except requests.exceptions.HTTPError as e:
        err_detail = ''
        try:
            err_detail = e.response.json().get('message', str(e))
        except Exception:
            err_detail = str(e)
        return jsonify({'error': f'PayPal error: {err_detail}'}), 500
    except Exception as e:
        return jsonify({'error': f'Failed to create subscription: {str(e)}'}), 500


@app.route('/api/paypal/activate-subscription', methods=['POST'])
@require_auth
def activate_paypal_subscription(user_id):
    """Activate a PayPal subscription after user approval.
    Called by the frontend after the PayPal popup closes (onApprove).

    1. Fetch subscription from PayPal API
    2. Validate custom_id matches authenticated user
    3. Upsert into subscriptions table
    4. Sync users.plan
    """
    data = request.get_json(silent=True) or {}
    subscription_id = data.get('subscription_id', '').strip()
    if not subscription_id:
        return jsonify({'error': 'subscription_id is required'}), 400

    try:
        # 1. Fetch subscription details from PayPal
        resp = requests.get(
            f'{PAYPAL_BASE}/v1/billing/subscriptions/{subscription_id}',
            headers=paypal_headers()
        )
        resp.raise_for_status()
        sub = resp.json()

        status = sub.get('status', '')
        if status not in ('ACTIVE', 'APPROVAL_PENDING'):
            return jsonify({'error': f'Subscription not active (status: {status})'}), 400

        # 2. Validate custom_id belongs to this user
        custom_id = sub.get('custom_id', '')
        if custom_id and custom_id != user_id:
            return jsonify({'error': 'Subscription does not belong to this user'}), 403

        # 3. If APPROVAL_PENDING, activate it on PayPal side
        if status == 'APPROVAL_PENDING':
            resp2 = requests.post(
                f'{PAYPAL_BASE}/v1/billing/subscriptions/{subscription_id}/activate',
                headers=paypal_headers(),
                json={}
            )
            resp2.raise_for_status()
            # Refetch to get updated status & billing info
            resp = requests.get(
                f'{PAYPAL_BASE}/v1/billing/subscriptions/{subscription_id}',
                headers=paypal_headers()
            )
            resp.raise_for_status()
            sub = resp.json()

        # 4. Extract billing cycle info
        billing_info = sub.get('billing_info', {})
        next_billing = billing_info.get('next_billing_time', None)
        last_payment = billing_info.get('last_payment', {})
        period_end = next_billing or (
            last_payment.get('time', None) if last_payment else None
        )

        # 5. Upsert into subscriptions table (generic)
        sb = get_supabase()
        existing = sb.table('subscriptions').select('id') \
            .eq('user_id', user_id) \
            .eq('provider', 'paypal') \
            .eq('provider_subscription_id', subscription_id) \
            .execute()

        sub_record = {
            'user_id': user_id,
            'provider': 'paypal',
            'provider_subscription_id': subscription_id,
            'status': 'active',
            'plan_tier': 'premium',
            'current_period_end': period_end,
            'cancel_at_period_end': False,
            'provider_metadata': {
                'paypal_plan_id': sub.get('plan_id', ''),
                'subscriber_email': (sub.get('subscriber', {}) or {}).get('email_address', ''),
                'last_updated': datetime.datetime.utcnow().isoformat()
            }
        }

        if existing.data:
            sb.table('subscriptions').update(sub_record) \
                .eq('id', existing.data[0]['id']).execute()
        else:
            # Cancel any previous active PayPal subscription for this user
            sb.table('subscriptions').update({'status': 'cancelled', 'cancelled_at': 'now()'}) \
                .eq('user_id', user_id) \
                .eq('provider', 'paypal') \
                .eq('status', 'active') \
                .execute()
            sb.table('subscriptions').insert(sub_record).execute()

        # 6. Sync users.plan
        sync_user_plan(user_id)

        return jsonify({
            'success': True,
            'plan': 'premium',
            'subscription_id': subscription_id,
            'current_period_end': period_end
        })
    except requests.exceptions.HTTPError as e:
        err_detail = ''
        try:
            err_detail = e.response.json().get('message', str(e))
        except Exception:
            err_detail = str(e)
        return jsonify({'error': f'PayPal error: {err_detail}'}), 500
    except Exception as e:
        return jsonify({'error': f'Failed to activate subscription: {str(e)}'}), 500


@app.route('/api/paypal/cancel-subscription', methods=['POST'])
@require_auth
def cancel_paypal_subscription(user_id):
    """Cancel auto-renewal — user keeps premium until current period ends."""
    sb = get_supabase()
    try:
        res = sb.table('subscriptions').select('*') \
            .eq('user_id', user_id) \
            .eq('provider', 'paypal') \
            .eq('status', 'active') \
            .execute()
        if not res.data:
            return jsonify({'error': 'No active PayPal subscription found'}), 400

        sub = res.data[0]

        # Mark cancel-at-period-end — do NOT call PayPal cancel API
        # (PayPal will attempt renewal; if it fails or plan doesn't auto-renew,
        #  the webhook EXPIRED event will handle actual downgrade)
        sb.table('subscriptions').update({
            'cancel_at_period_end': True,
            'cancelled_at': datetime.datetime.utcnow().isoformat()
        }).eq('id', sub['id']).execute()

        # Refresh subscription info for response
        info = get_subscription_info(user_id)

        return jsonify({
            'success': True,
            'plan': info['plan'],
            'subscription': info['subscription'],
            'message': 'Auto-renewal cancelled. Your premium access continues until the current period ends.'
        })
    except requests.exceptions.HTTPError as e:
        err_detail = ''
        try:
            err_detail = e.response.json().get('message', str(e))
        except Exception:
            err_detail = str(e)
        return jsonify({'error': f'PayPal error: {err_detail}'}), 500
    except Exception as e:
        return jsonify({'error': f'Failed to cancel subscription: {str(e)}'}), 500


@app.route('/api/paypal/get-status', methods=['GET'])
def get_paypal_status():
    """Get PayPal configuration status for the frontend."""
    return jsonify({
        'configured': bool(PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET),
        'mode': PAYPAL_MODE,
        'has_plan_id': bool(os.getenv('PAYPAL_PLAN_ID', ''))
    })


@app.route('/api/paypal/webhook', methods=['POST'])
def paypal_webhook():
    """Handle PayPal subscription lifecycle events.

    ⚠️ REQUIRES PayPal BUSINESS account. Personal accounts cannot use webhooks.
    Webhook URL must be registered at: PayPal Developer Dashboard → Webhooks

    Events handled:
    - BILLING.SUBSCRIPTION.ACTIVATED   → subscription started
    - BILLING.SUBSCRIPTION.CANCELLED   → subscription cancelled
    - BILLING.SUBSCRIPTION.EXPIRED     → subscription expired
    - BILLING.SUBSCRIPTION.SUSPENDED   → payment failed, subscription suspended
    - BILLING.SUBSCRIPTION.PAYMENT.FAILED → single payment failure
    """
    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({'error': 'Invalid payload'}), 400

    event_type = payload.get('event_type', '')
    resource = payload.get('resource', {})
    subscription_id = resource.get('id', '')

    if not subscription_id or not event_type.startswith('BILLING.SUBSCRIPTION.'):
        return jsonify({'received': True}), 200  # Ack unknown events

    print(f'[Webhook] {event_type} — subscription {subscription_id}')

    try:
        sb = get_supabase()
        res = sb.table('subscriptions').select('*') \
            .eq('provider', 'paypal') \
            .eq('provider_subscription_id', subscription_id) \
            .execute()

        if not res.data:
            print(f'[Webhook] Unknown subscription: {subscription_id}')
            return jsonify({'received': True}), 200

        sub_record = res.data[0]
        user_id = sub_record['user_id']
        new_status = None

        if event_type == 'BILLING.SUBSCRIPTION.ACTIVATED':
            new_status = 'active'
        elif event_type == 'BILLING.SUBSCRIPTION.CANCELLED':
            new_status = 'cancelled'
        elif event_type == 'BILLING.SUBSCRIPTION.EXPIRED':
            new_status = 'expired'
        elif event_type == 'BILLING.SUBSCRIPTION.SUSPENDED':
            new_status = 'past_due'

        if new_status:
            updates = {'status': new_status}
            if new_status in ('cancelled', 'expired'):
                updates['cancelled_at'] = datetime.datetime.utcnow().isoformat()

            # Fetch latest period info from PayPal
            try:
                sub_resp = requests.get(
                    f'{PAYPAL_BASE}/v1/billing/subscriptions/{subscription_id}',
                    headers=paypal_headers()
                )
                if sub_resp.ok:
                    sub_data = sub_resp.json()
                    billing_info = sub_data.get('billing_info', {})
                    updates['current_period_end'] = billing_info.get('next_billing_time')
                    updates['provider_metadata'] = {
                        **(sub_record.get('provider_metadata') or {}),
                        'last_webhook_event': event_type,
                        'last_webhook_at': datetime.datetime.utcnow().isoformat()
                    }
            except Exception:
                pass

            sb.table('subscriptions').update(updates).eq('id', sub_record['id']).execute()
            sync_user_plan(user_id)
            print(f'[Webhook] Updated subscription {subscription_id} → {new_status}')

    except Exception as e:
        print(f'[Webhook] Error processing {event_type}: {e}')
        # Still return 200 to prevent PayPal retry storms

    return jsonify({'received': True}), 200


@app.route('/api/user/subscription', methods=['GET'])
@require_auth
def get_user_subscription(user_id):
    """Return current user's subscription details."""
    sb = get_supabase()
    try:
        res = sb.table('subscriptions').select('*') \
            .eq('user_id', user_id) \
            .order('created_at', desc=True) \
            .limit(1) \
            .execute()
    except Exception:
        return jsonify({'plan': 'free', 'subscription': None})

    if not res.data:
        return jsonify({
            'plan': 'free',
            'subscription': None
        })

    sub = res.data[0]
    return jsonify({
        'plan': sub.get('plan_tier', 'free') if sub.get('status') == 'active' else 'free',
        'subscription': {
            'provider': sub.get('provider'),
            'status': sub.get('status'),
            'plan_tier': sub.get('plan_tier'),
            'current_period_end': sub.get('current_period_end'),
            'cancel_at_period_end': sub.get('cancel_at_period_end', False),
            'created_at': sub.get('created_at')
        }
    })


# ── Parse Endpoints ────────────────────────────────────────────────────

@app.route('/api/parse/pdf', methods=['POST'])
@flex_auth
def parse_pdf(user_id, anon_id):
    allowed, remaining = check_usage(user_id, anon_id, request.remote_addr)
    if not allowed:
        limit = 10 if user_id else ANON_DAILY_LIMIT
        msg = f'Daily free limit reached ({limit}/{limit}).' + (' Upgrade to Premium for unlimited access.' if user_id else ' Sign up for 10 free uses per day!')
        return jsonify({'error': msg}), 429

    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'No file selected'}), 400
    if not file.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'Only PDF files are accepted'}), 400

    ext = os.path.splitext(file.filename)[1].lower() or '.pdf'
    filename = str(uuid.uuid4()) + ext
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    try:
        text = extract_pdf_text(filepath)
    except Exception as e:
        if os.path.exists(filepath):
            os.remove(filepath)
        return jsonify({'error': f'Failed to parse PDF: {str(e)}'}), 500
    finally:
        if os.path.exists(filepath):
            os.remove(filepath)

    if not text:
        return jsonify({'error': 'No extractable text found in this PDF'}), 400

    remaining = record_usage(user_id, anon_id, request.remote_addr)
    return jsonify({
        'text': text,
        'word_count': len(text.split()),
        'remaining': remaining
    })


@app.route('/api/parse/word', methods=['POST'])
@flex_auth
def parse_word(user_id, anon_id):
    allowed, remaining = check_usage(user_id, anon_id, request.remote_addr)
    if not allowed:
        limit = 10 if user_id else ANON_DAILY_LIMIT
        msg = f'Daily free limit reached ({limit}/{limit}).' + (' Upgrade to Premium for unlimited access.' if user_id else ' Sign up for 10 free uses per day!')
        return jsonify({'error': msg}), 429

    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'No file selected'}), 400
    if not file.filename.lower().endswith('.docx'):
        return jsonify({
            'error': 'Only .docx files are supported. If you have a .doc file, please open it in Word and Save As .docx, then upload again.'
        }), 400

    ext = os.path.splitext(file.filename)[1].lower() or '.docx'
    filename = str(uuid.uuid4()) + ext
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    try:
        text = extract_docx_text(filepath)
    except Exception as e:
        if os.path.exists(filepath):
            os.remove(filepath)
        err_msg = str(e)
        if 'Package not found' in err_msg or 'not a valid' in err_msg:
            err_msg = 'Invalid .docx file. If your file is .doc format, please open it in Word and Save As .docx.'
        return jsonify({'error': f'Failed to parse Word document: {err_msg}'}), 500
    finally:
        if os.path.exists(filepath):
            os.remove(filepath)

    if not text:
        return jsonify({'error': 'No extractable text found in this document'}), 400

    remaining = record_usage(user_id, anon_id, request.remote_addr)
    return jsonify({
        'text': text,
        'word_count': len(text.split()),
        'remaining': remaining
    })


@app.route('/api/parse/url', methods=['POST'])
@flex_auth
def parse_url(user_id, anon_id):
    allowed, remaining = check_usage(user_id, anon_id, request.remote_addr)
    if not allowed:
        limit = 10 if user_id else ANON_DAILY_LIMIT
        msg = f'Daily free limit reached ({limit}/{limit}).' + (' Upgrade to Premium for unlimited access.' if user_id else ' Sign up for 10 free uses per day!')
        return jsonify({'error': msg}), 429

    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()

    if not url:
        return jsonify({'error': 'URL is required'}), 400
    if not url.startswith(('http://', 'https://')):
        return jsonify({'error': 'Invalid URL — must start with http:// or https://'}), 400

    try:
        text = extract_url_text(url)
    except requests.exceptions.Timeout:
        return jsonify({'error': 'Request timed out — the website took too long to respond. Try again or use a different URL.'}), 500
    except requests.exceptions.ConnectionError:
        parsed = urlparse(url)
        hostname = parsed.hostname or ''
        if any(suffix in hostname for suffix in ['.gov.cn', '.gov.', 'gov.cn']):
            return jsonify({
                'error': 'Cannot reach this government website from the server. '
                         'Government sites often block overseas traffic. '
                         'Tip: download the PDF first, then use the "Upload PDF / Word" tab to process it.'
            }), 500
        return jsonify({'error': 'Could not connect to the URL — it may be temporarily unavailable or blocking our server. Try a different URL.'}), 500
    except ValueError as e:
        if 'private' in str(e).lower() or 'internal' in str(e).lower():
            return jsonify({'error': 'Access to internal/private networks is not allowed.'}), 400
        return jsonify({'error': f'Invalid URL: {str(e)}'}), 400
    except Exception as e:
        return jsonify({'error': f'Failed to fetch URL content: {str(e)}'}), 500

    if not text:
        return jsonify({'error': 'No readable content found at this URL'}), 400

    remaining = record_usage(user_id, anon_id, request.remote_addr)
    return jsonify({
        'text': text,
        'word_count': len(text.split()),
        'remaining': remaining
    })

# ── YouTube Transcript Endpoint ─────────────────────────────────────────

def _fetch_transcript_innertube(video_id: str):
    """
    Fetch YouTube captions via the innertube player API (POST endpoint).
    Bypasses HTML page scraping which triggers Google's bot detection (429).
    Returns (transcript_text: str, language_code: str).
    """
    import xml.etree.ElementTree as ET

    # Mobile user-agent is less likely to trigger bot detection
    ua = 'com.google.android.youtube/19.33.35 (Linux; U; Android 14; en_US)'
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': ua,
        'Origin': 'https://www.youtube.com',
    }

    # Try mobile clients first (less strict), then web
    clients = [
        {"client": {"clientName": "ANDROID", "clientVersion": "19.33.35", "hl": "en", "gl": "US"}},
        {"client": {"clientName": "IOS", "clientVersion": "19.33.35", "hl": "en", "gl": "US"}},
        {"client": {"clientName": "WEB", "clientVersion": "2.20250501.00.00", "hl": "en", "gl": "US"}},
    ]

    last_error = None
    for ctx in clients:
        try:
            payload = {"context": ctx, "videoId": video_id}
            resp = requests.post(
                'https://www.youtube.com/youtubei/v1/player',
                headers=headers,
                json=payload,
                timeout=15
            )
            if resp.status_code != 200:
                last_error = f'HTTP {resp.status_code} on player API'
                continue

            pr = resp.json()
            tracks = (
                pr.get('captions', {})
                .get('playerCaptionsTracklistRenderer', {})
                .get('captionTracks', [])
            )
            if tracks:
                break
            last_error = 'No caption tracks in response'
        except Exception as e:
            last_error = str(e)
            continue
    else:
        raise Exception(last_error or 'Could not fetch video data from YouTube')

    # Pick English track, fallback to first available
    track = next((t for t in tracks if t.get('languageCode') == 'en'), tracks[0])
    base_url = track.get('baseUrl')
    if not base_url:
        raise Exception('Could not get caption download URL')

    # Fetch & parse caption XML
    cap_resp = requests.get(base_url, headers={'User-Agent': ua}, timeout=15)
    cap_resp.raise_for_status()

    root = ET.fromstring(cap_resp.text)

    parts = []
    for elem in root.iter():
        tag = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
        if tag in ('p', 'text'):
            t = elem.text or ''
            t = re.sub(r'<[^>]+>', '', t)
            t = t.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&#39;', "'").replace('&quot;', '"').replace('&apos;', "'")
            if t.strip():
                parts.append(t.strip())

    if not parts:
        raise Exception('Caption text is empty')

    return ' '.join(parts), track.get('languageCode', 'unknown')


def extract_youtube_video_id(url):
    """Extract YouTube video ID from various URL formats."""
    patterns = [
        r'(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/shorts/|youtube\.com/live/)([A-Za-z0-9_-]{11})',
        r'youtube\.com/watch\?.*v=([A-Za-z0-9_-]{11})',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


@app.route('/api/youtube/transcript', methods=['POST'])
@flex_auth
def youtube_transcript(user_id, anon_id):
    allowed, remaining = check_usage(user_id, anon_id, request.remote_addr)
    if not allowed:
        limit = 10 if user_id else ANON_DAILY_LIMIT
        msg = f'Daily free limit reached ({limit}/{limit}).' + (' Upgrade to Premium for unlimited access.' if user_id else ' Sign up for 10 free uses per day!')
        return jsonify({'error': msg}), 429

    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()

    if not url:
        return jsonify({'error': 'YouTube URL is required'}), 400

    video_id = extract_youtube_video_id(url)
    if not video_id:
        return jsonify({'error': 'Invalid YouTube URL. Please provide a valid YouTube video link.'}), 400

    try:
        segments = None
        language = 'en'

        # Try innertube API first (bypasses bot detection on Railway)
        try:
            transcript_text, language = _fetch_transcript_innertube(video_id)
            if transcript_text and transcript_text.strip():
                segments = [{'text': transcript_text}]
        except Exception:
            pass

        # Fallback: youtube-transcript-api library (v1.2.4+ uses .fetch() not .get_transcript())
        if segments is None:
            try:
                ytt_api = YouTubeTranscriptApi()
                fetched = ytt_api.fetch(video_id, languages=['en'])
                segments = fetched.to_raw_data()
                language = fetched.language_code or 'en'
            except Exception:
                pass

        if segments is None:
            try:
                ytt_api = YouTubeTranscriptApi()
                fetched = ytt_api.fetch(video_id)
                segments = fetched.to_raw_data()
                language = fetched.language_code or 'auto'
            except Exception:
                pass

        if not segments:
            return jsonify({'error': 'Unable to fetch transcript for this video. Please try another video.'}), 400

        text = ' '.join(s.get('text', '') for s in segments if s.get('text'))

        if not text or not text.strip():
            return jsonify({'error': 'Unable to fetch transcript for this video. Please try another video.'}), 400

        remaining = record_usage(user_id, anon_id, request.remote_addr)

        return jsonify({
            'text': text.strip(),
            'word_count': len(text.split()),
            'remaining': remaining,
            'video_id': video_id,
            'language': language
        })

    except Exception:
        return jsonify({'error': 'Unable to fetch transcript for this video. Please try another video.'}), 400


# ── YouTube Summarizer Page ──────────────────────────────────────────────

@app.route('/youtube-summarizer')
def youtube_summarizer_page():
    return render_template('youtube.html')


# ── AI Generate Endpoint ───────────────────────────────────────────────

@app.route('/api/ai/generate', methods=['POST'])
@flex_auth
def ai_generate(user_id, anon_id):
    data = request.get_json(silent=True) or {}
    action = (data.get('action') or '').strip()
    content = (data.get('content') or '').strip()
    target_lang = (data.get('target_lang') or 'Chinese').strip()

    if not action or not content:
        return jsonify({'error': 'Both action and content are required'}), 400

    if len(content) > 50000:
        return jsonify({'error': 'Content too long (maximum 50,000 characters)'}), 400

    PROMPTS = {
        'summarize': (
            "You are a professional document analyst. Please provide a **concise, well-structured summary** "
            "of the following document. IMPORTANT: You MUST respond in the **same language** as the document. "
            "The summary MUST NOT exceed 250 characters (including spaces and punctuation). "
            "Be concise and professional.\n\nDocument:\n{text}"
        ),
        'keypoints': (
            "You are a professional document analyst. Please **extract the most important key points** from the "
            "following document. IMPORTANT: You MUST respond in the **same language** as the document. "
            "Present them as a **numbered list**. Each point should be specific, actionable, "
            "and clearly stated. Highlight any critical data, numbers, or deadlines mentioned.\n\nDocument:\n{text}"
        ),
        'translate': (
            "You are a professional translator. Please **accurately translate** the following text into "
            "**{lang}**. Maintain the original meaning, tone, and formatting as much as possible. "
            "If there are technical terms, translate them appropriately.\n\nText:\n{text}"
        )
    }

    if action not in PROMPTS:
        return jsonify({'error': 'Invalid action — use: summarize, keypoints, or translate'}), 400

    # For long content: truncate to avoid timeout
    if action == 'translate' and len(content) > 8000:
        content = content[:8000]
    elif action in ('summarize', 'keypoints') and len(content) > 5000:
        content = content[:5000]

    prompt = PROMPTS[action].replace('{text}', content).replace('{lang}', target_lang)

    def generate():
        yield f"data: {json.dumps({'action': action})}\n\n"
        token_limit = 8192 if action == 'translate' else 1024
        for chunk_json in call_ai_stream(
            messages=[{'role': 'user', 'content': prompt}],
            temperature=0.5,
            max_tokens=token_limit
        ):
            yield f"data: {chunk_json}\n\n"
        yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive'
        }
    )


# ── Error Handlers ────────────────────────────────────────────────────

@app.errorhandler(413)
def too_large(_e):
    return jsonify({'error': 'File too large — maximum size is 16 MB'}), 413

@app.errorhandler(404)
def not_found(_e):
    return jsonify({'error': 'Endpoint not found'}), 404

@app.errorhandler(405)
def method_not_allowed(_e):
    return jsonify({'error': 'Method not allowed'}), 405

@app.errorhandler(500)
def internal_error(_e):
    return jsonify({'error': 'Internal server error'}), 500

# ── Entry Point ───────────────────────────────────────────────────────

if __name__ == '__main__':
    print("=" * 60)
    print("  Summarify Pro — AI Document Summarizer")
    print(f"  AI Provider : {AI_PROVIDER} ({OPENAI_MODEL})")
    print(f"  AI Base URL : {OPENAI_BASE_URL}")
    print(f"  Supabase URL : {SUPABASE_URL}")
    print(f"  PayPal  : {'configured' if PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET else 'NOT configured'} ({PAYPAL_MODE})")
    print("  Server running at http://localhost:5000")
    print("=" * 60)
    app.run(debug=False, host='0.0.0.0', port=int(os.getenv('PORT', 8080)))