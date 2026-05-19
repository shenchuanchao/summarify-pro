"""
Summarify Pro — Backend API Server
AI Document Summarizer — International Edition
Powered by Zhipu AI GLM-4-Flash
Stripe-powered Premium subscription
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

# Stripe
import stripe

# YouTube Transcript
from youtube_transcript_api import YouTubeTranscriptApi

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

# ── App Initialization ────────────────────────────────────────────────

app = Flask(__name__, static_folder='static', static_url_path='')
CORS_ALLOWED_ORIGINS = os.getenv('CORS_ORIGINS', '').strip()
if CORS_ALLOWED_ORIGINS:
    _origins = [o.strip() for o in CORS_ALLOWED_ORIGINS.split(',') if o.strip()]
    CORS(app, resources={r"/api/*": {"origins": _origins}})
else:
    CORS(app, resources={r"/api/*": {"origins": "*"}})

app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', secrets.token_hex(32))
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB
app.config['UPLOAD_FOLDER'] = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'uploads'
)

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# ── AI Config (from .env) ─────────────────────────────────────────────

AI_PROVIDER    = os.getenv('AI_PROVIDER', 'zhipu')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')
OPENAI_BASE_URL = os.getenv('OPENAI_BASE_URL', 'https://open.bigmodel.cn/api/paas/v4')
OPENAI_MODEL   = os.getenv('OPENAI_MODEL', 'glm-4-flash')

# ── Supabase Client ───────────────────────────────────────────────────

SUPABASE_URL = os.getenv('PUBLIC_SUPABASE_URL', '')
SUPABASE_KEY = os.getenv(
    'SUPABASE_SERVICE_ROLE_KEY'
) or os.getenv('PUBLIC_SUPABASE_ANON_KEY', '')

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

# ── Stripe Client ─────────────────────────────────────────────────────

STRIPE_SECRET_KEY = os.getenv('STRIPE_SECRET_KEY', '')
STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET', '')
STRIPE_PRICE_ID = os.getenv('STRIPE_PRICE_ID', '')

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

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
    Returns (allowed: bool, remaining: int or None for unlimited).
    Uses Supabase to query and update the user's daily usage count.
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

    if last_date != today:
        sb.table('users').update({
            'daily_usage_count': 1,
            'last_usage_date': today
        }).eq('id', user_id).execute()
        return True, 9

    if user['daily_usage_count'] < 10:
        new_count = user['daily_usage_count'] + 1
        sb.table('users').update(
            {'daily_usage_count': new_count}
        ).eq('id', user_id).execute()
        remaining = 10 - new_count
        return True, remaining

    return False, 0

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
    soup = BeautifulSoup(resp.text, 'html.parser')
    for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside', 'iframe', 'noscript']):
        tag.decompose()
    text = soup.get_text(separator='\n')
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    return '\n'.join(lines)[:30000]

# ── Page Routes ───────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login')
def login_page():
    return render_template('login.html')

@app.route('/register')
def register_page():
    return render_template('register.html')

@app.route('/privacy')
def privacy_page():
    return render_template('privacy.html')

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

    return jsonify({
        'token': token,
        'user': {
            'id': new_user['id'],
            'email': new_user['email'],
            'plan': new_user.get('plan', 'free')
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

    return jsonify({
        'token': token,
        'user': {
            'id': user['id'],
            'email': user['email'],
            'plan': user.get('plan', 'free')
        }
    })


@app.route('/api/user/usage', methods=['GET'])
@require_auth
def get_usage(user_id):
    sb = get_supabase()
    res = sb.table('users').select(
        'plan, daily_usage_count, last_usage_date'
    ).eq('id', user_id).execute()

    if not res.data:
        return jsonify({'error': 'User not found'}), 404

    user = res.data[0]
    today = datetime.datetime.now().strftime('%Y-%m-%d')

    if user['plan'] == 'premium':
        return jsonify({
            'plan': 'premium',
            'daily_limit': None,
            'used_today': 0,
            'remaining': None
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


# ── Feedback ───────────────────────────────────────────────────────────

@app.route('/api/user/feedback', methods=['POST'])
@require_auth
def submit_feedback(user_id):
    data = request.get_json() or {}
    content = (data.get('content', '') or '').strip()
    if not content:
        return jsonify({'error': 'Feedback content is required'}), 400
    if len(content) > 2000:
        return jsonify({'error': 'Feedback too long (max 2000 characters)'}), 400

    sb = get_supabase()
    sb.table('feedback').insert({
        'user_id': user_id,
        'content': content
    }).execute()

    return jsonify({'success': True, 'message': 'Thank you for your feedback!'})


# ── Stripe Endpoints ───────────────────────────────────────────────────

@app.route('/api/stripe/create-checkout-session', methods=['POST'])
@require_auth
def create_checkout_session(user_id):
    """Create a Stripe Checkout session for the user to subscribe."""
    if not STRIPE_SECRET_KEY:
        return jsonify({'error': 'Stripe not configured on server'}), 500
    if not STRIPE_PRICE_ID:
        return jsonify({'error': 'Stripe price ID not configured'}), 500

    sb = get_supabase()
    res = sb.table('users').select('*').eq('id', user_id).execute()
    if not res.data:
        return jsonify({'error': 'User not found'}), 404

    user = res.data[0]
    email = user['email']

    # Get or create Stripe customer
    customer_id = None
    try:
        customer = stripe.Customer.create(email=email)
        customer_id = customer.id
    except Exception as e:
        return jsonify({'error': f'Failed to create Stripe customer: {str(e)}'}), 500

    # Determine success/cancel URLs
    origin = request.headers.get('Origin', 'http://localhost:5000')
    success_url = f'{origin}/?checkout=success&session_id={{CHECKOUT_SESSION_ID}}'
    cancel_url = f'{origin}/?checkout=cancelled'

    try:
        session = stripe.checkout.Session.create(
            customer=customer_id,
            payment_method_types=['card'],
            line_items=[{
                'price': STRIPE_PRICE_ID,
                'quantity': 1
            }],
            mode='payment',  # One-time payment for lifetime access
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={
                'user_id': user_id
            }
        )
        return jsonify({
            'checkout_url': session.url,
            'session_id': session.id
        })
    except Exception as e:
        return jsonify({'error': f'Failed to create checkout session: {str(e)}'}), 500


@app.route('/api/stripe/verify-session', methods=['GET'])
@require_auth
def verify_session(user_id):
    """Verify a Stripe Checkout session and activate premium."""
    session_id = request.args.get('session_id', '').strip()
    if not session_id:
        return jsonify({'error': 'session_id is required'}), 400

    if not STRIPE_SECRET_KEY:
        return jsonify({'error': 'Stripe not configured'}), 500

    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception as e:
        return jsonify({'error': f'Failed to verify session: {str(e)}'}), 500

    if session.payment_status != 'paid':
        return jsonify({'error': 'Payment not completed', 'status': session.payment_status}), 400

    # Verify this session belongs to this user
    if session.metadata.get('user_id') != user_id:
        return jsonify({'error': 'Session mismatch'}), 403

    # Activate premium
    sb = get_supabase()
    sb.table('users').update({
        'plan': 'premium'
    }).eq('id', user_id).execute()

    return jsonify({
        'success': True,
        'plan': 'premium'
    })


@app.route('/api/stripe/webhook', methods=['POST'])
def stripe_webhook():
    """Handle Stripe webhook events."""
    if not STRIPE_WEBHOOK_SECRET:
        return jsonify({'error': 'Webhook not configured'}), 500

    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature', '')
    event = None

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        return jsonify({'error': 'Invalid payload'}), 400
    except stripe.error.SignatureVerificationError:
        return jsonify({'error': 'Invalid signature'}), 400

    event_type = event['type']

    if event_type == 'checkout.session.completed':
        session = event['data']['object']
        user_id = session.metadata.get('user_id')
        if user_id and session.payment_status == 'paid':
            sb = get_supabase()
            sb.table('users').update({
                'plan': 'premium'
            }).eq('id', user_id).execute()

    elif event_type == 'payment_intent.payment_failed':
        # Handle failed payment
        pass

    return jsonify({'received': True})


@app.route('/api/stripe/get-status', methods=['GET'])
def get_stripe_status():
    """Get Stripe configuration status for the frontend."""
    return jsonify({
        'configured': bool(STRIPE_SECRET_KEY and STRIPE_PRICE_ID),
        'has_webhook': bool(STRIPE_WEBHOOK_SECRET)
    })


# ── Parse Endpoints ────────────────────────────────────────────────────

@app.route('/api/parse/pdf', methods=['POST'])
@require_auth
def parse_pdf(user_id):
    allowed, remaining = check_daily_usage(user_id)
    if not allowed:
        return jsonify({
            'error': 'Daily free limit reached (10/10). Upgrade to Premium for unlimited access.'
        }), 429

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

    return jsonify({
        'text': text,
        'word_count': len(text.split()),
        'remaining': remaining
    })


@app.route('/api/parse/word', methods=['POST'])
@require_auth
def parse_word(user_id):
    allowed, remaining = check_daily_usage(user_id)
    if not allowed:
        return jsonify({
            'error': 'Daily free limit reached (10/10). Upgrade to Premium for unlimited access.'
        }), 429

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

    return jsonify({
        'text': text,
        'word_count': len(text.split()),
        'remaining': remaining
    })


@app.route('/api/parse/url', methods=['POST'])
@require_auth
def parse_url(user_id):
    allowed, remaining = check_daily_usage(user_id)
    if not allowed:
        return jsonify({
            'error': 'Daily free limit reached (10/10). Upgrade to Premium for unlimited access.'
        }), 429

    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()

    if not url:
        return jsonify({'error': 'URL is required'}), 400
    if not url.startswith(('http://', 'https://')):
        return jsonify({'error': 'Invalid URL — must start with http:// or https://'}), 400

    try:
        text = extract_url_text(url)
    except requests.exceptions.Timeout:
        return jsonify({'error': 'Request timed out — the website took too long to respond'}), 500
    except requests.exceptions.ConnectionError:
        return jsonify({'error': 'Could not connect to the URL — check if it is valid'}), 500
    except Exception as e:
        return jsonify({'error': f'Failed to fetch URL content: {str(e)}'}), 500

    if not text:
        return jsonify({'error': 'No readable content found at this URL'}), 400

    return jsonify({
        'text': text,
        'word_count': len(text.split()),
        'remaining': remaining
    })

# ── YouTube Transcript Endpoint ─────────────────────────────────────────

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
@require_auth
def youtube_transcript(user_id):
    allowed, remaining = check_daily_usage(user_id)
    if not allowed:
        return jsonify({
            'error': 'Daily free limit reached (10/10). Upgrade to Premium for unlimited access.'
        }), 429

    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()

    if not url:
        return jsonify({'error': 'YouTube URL is required'}), 400

    video_id = extract_youtube_video_id(url)
    if not video_id:
        return jsonify({'error': 'Invalid YouTube URL. Please provide a valid YouTube video link.'}), 400

    try:
        # Try English transcript first, fall back to any available
        segments = None
        language = 'en'
        try:
            segments = YouTubeTranscriptApi.get_transcript(video_id, languages=['en'])
        except Exception:
            pass

        if segments is None:
            try:
                segments = YouTubeTranscriptApi.get_transcript(video_id)
                language = 'auto'
            except Exception as e2:
                return jsonify({'error': f'No transcript available for this video: {str(e2)}'}), 400

        if not segments:
            return jsonify({'error': 'Transcript is empty for this video.'}), 400

        text = ' '.join(s.get('text', '') for s in segments if s.get('text'))

        if not text or not text.strip():
            return jsonify({'error': 'Transcript text is empty.'}), 400

        return jsonify({
            'text': text.strip(),
            'word_count': len(text.split()),
            'remaining': remaining,
            'video_id': video_id,
            'language': language
        })

    except Exception as e:
        error_msg = str(e)
        if 'Video unavailable' in error_msg or 'private' in error_msg.lower():
            return jsonify({'error': 'This video is unavailable (private, deleted, or not found).'}), 400
        if 'disabled' in error_msg.lower():
            return jsonify({'error': 'Transcripts are disabled for this video.'}), 400
        return jsonify({'error': f'Failed to fetch transcript: {error_msg}'}), 500


# ── YouTube Summarizer Page ──────────────────────────────────────────────

@app.route('/youtube-summarizer')
def youtube_summarizer_page():
    return render_template('youtube.html')


# ── AI Generate Endpoint ───────────────────────────────────────────────

@app.route('/api/ai/generate', methods=['POST'])
@require_auth
def ai_generate(user_id):
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
            "Organize the summary with clear sections (e.g., Overview, Key Findings, "
            "Conclusion). Keep it professional and to the point.\n\nDocument:\n{text}"
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
    print(f"  Stripe : {'configured' if STRIPE_SECRET_KEY else 'NOT configured'}")
    print("  Server running at http://localhost:5000")
    print("=" * 60)
    app.run(debug=False, host='0.0.0.0', port=int(os.getenv('PORT', 8080)))