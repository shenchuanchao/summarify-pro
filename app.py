"""
Summarify Pro — Backend API Server
AI Document Summarizer — International Edition
Powered by Zhipu AI GLM-4-Flash
"""

import os
import re
import sqlite3
import hashlib
import secrets
import datetime
from functools import wraps

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import jwt
import requests
import PyPDF2
from docx import Document
from bs4 import BeautifulSoup
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

load_dotenv()

# ── App Initialization ────────────────────────────────────────────────

app = Flask(__name__, static_folder='static', static_url_path='')
CORS(app, resources={r"/api/*": {"origins": "*"}})

app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', secrets.token_hex(32))
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')

ZHIPU_API_KEY = os.getenv('ZHIPU_API_KEY', '')
ZHIPU_API_URL = 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
DATABASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'summarify.db')

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# ── Database ────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            plan TEXT DEFAULT 'free',
            daily_usage_count INTEGER DEFAULT 0,
            last_usage_date TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# ── Auth Utilities ──────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def generate_token(user_id: int) -> str:
    payload = {
        'user_id': user_id,
        'exp': datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)
    }
    return jwt.encode(payload, app.config['SECRET_KEY'], algorithm='HS256')

def verify_token(token: str):
    try:
        payload = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
        return payload['user_id']
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization', '')
        token = auth_header.replace('Bearer ', '')
        if not token:
            return jsonify({'error': 'Authorization header required'}), 401
        user_id = verify_token(token)
        if user_id is None:
            return jsonify({'error': 'Invalid or expired token'}), 401
        return f(user_id, *args, **kwargs)
    return decorated

# ── Rate Limiting ───────────────────────────────────────────────────────

def check_daily_usage(user_id: int):
    """Returns (allowed: bool, remaining: int or None for unlimited)."""
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()

    if not user:
        conn.close()
        return False, 0

    if user['plan'] == 'premium':
        conn.close()
        return True, None  # None = unlimited

    today = datetime.datetime.now().strftime('%Y-%m-%d')
    last_date = (user['last_usage_date'] or '')

    if last_date != today:
        # New day — reset counter
        conn.execute(
            'UPDATE users SET daily_usage_count = 1, last_usage_date = ? WHERE id = ?',
            (today, user_id)
        )
        conn.commit()
        conn.close()
        return True, 2  # 2 remaining (just used 1)

    if user['daily_usage_count'] < 3:
        new_count = user['daily_usage_count'] + 1
        conn.execute('UPDATE users SET daily_usage_count = ? WHERE id = ?', (new_count, user_id))
        conn.commit()
        remaining = 3 - new_count
        conn.close()
        return True, remaining

    conn.close()
    return False, 0

# ── Zhipu AI Client ────────────────────────────────────────────────────

def call_zhipu_ai(messages: list, temperature: float = 0.5, max_tokens: int = 4096):
    if not ZHIPU_API_KEY:
        return None, "ZHIPU_API_KEY not configured — set it in your .env file"

    headers = {
        'Authorization': f'Bearer {ZHIPU_API_KEY}',
        'Content-Type': 'application/json'
    }
    payload = {
        'model': 'glm-4-flash',
        'messages': messages,
        'temperature': temperature,
        'max_tokens': max_tokens
    }

    try:
        resp = requests.post(ZHIPU_API_URL, json=payload, headers=headers, timeout=120)
        data = resp.json()
        if 'choices' in data and len(data['choices']) > 0:
            return data['choices'][0]['message']['content'], None
        err = data.get('error', {})
        return None, err.get('message', f'API error (HTTP {resp.status_code})')
    except requests.exceptions.Timeout:
        return None, 'Request timed out after 120 seconds'
    except requests.exceptions.ConnectionError:
        return None, 'Cannot connect to Zhipu AI API — check your network'
    except Exception as e:
        return None, str(e)

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

# ── Static Route ───────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')

# ── Auth Endpoints ─────────────────────────────────────────────────────

@app.route('/api/user/register', methods=['POST'])
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

    conn = get_db()
    existing = conn.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone()
    if existing:
        conn.close()
        return jsonify({'error': 'Email already registered'}), 409

    password_hash = hash_password(password)
    cursor = conn.execute(
        'INSERT INTO users (email, password_hash) VALUES (?, ?)',
        (email, password_hash)
    )
    user_id = cursor.lastrowid
    conn.commit()
    conn.close()

    token = generate_token(user_id)
    return jsonify({
        'token': token,
        'user': {'id': user_id, 'email': email, 'plan': 'free'}
    })


@app.route('/api/user/login', methods=['POST'])
def login():
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    password = (data.get('password') or '').strip()

    if not email or not password:
        return jsonify({'error': 'Email and password are required'}), 400

    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
    conn.close()

    if not user or user['password_hash'] != hash_password(password):
        return jsonify({'error': 'Invalid email or password'}), 401

    token = generate_token(user['id'])
    return jsonify({
        'token': token,
        'user': {
            'id': user['id'],
            'email': user['email'],
            'plan': user['plan']
        }
    })


@app.route('/api/user/usage', methods=['GET'])
@require_auth
def get_usage(user_id):
    conn = get_db()
    user = conn.execute(
        'SELECT plan, daily_usage_count, last_usage_date FROM users WHERE id = ?',
        (user_id,)
    ).fetchone()
    conn.close()

    today = datetime.datetime.now().strftime('%Y-%m-%d')

    if user['plan'] == 'premium':
        return jsonify({'plan': 'premium', 'daily_limit': None, 'used_today': 0, 'remaining': None})

    if user['last_usage_date'] != today:
        used, remaining = 0, 3
    else:
        used = user['daily_usage_count']
        remaining = max(0, 3 - used)

    return jsonify({
        'plan': 'free',
        'daily_limit': 3,
        'used_today': used,
        'remaining': remaining
    })

# ── Parse Endpoints ────────────────────────────────────────────────────

@app.route('/api/parse/pdf', methods=['POST'])
@require_auth
def parse_pdf(user_id):
    allowed, remaining = check_daily_usage(user_id)
    if not allowed:
        return jsonify({
            'error': 'Daily free limit reached (3/3). Upgrade to Premium for unlimited access.'
        }), 429

    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'No file selected'}), 400
    if not file.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'Only PDF files are accepted'}), 400

    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    try:
        text = extract_pdf_text(filepath)
    except Exception as e:
        if os.path.exists(filepath):
            os.remove(filepath)
        return jsonify({'error': f'Failed to parse PDF: {str(e)}'}), 500

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
            'error': 'Daily free limit reached (3/3). Upgrade to Premium for unlimited access.'
        }), 429

    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'No file selected'}), 400
    if not file.filename.lower().endswith(('.docx', '.doc')):
        return jsonify({'error': 'Only Word documents (.docx, .doc) are accepted'}), 400

    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    try:
        text = extract_docx_text(filepath)
    except Exception as e:
        if os.path.exists(filepath):
            os.remove(filepath)
        return jsonify({'error': f'Failed to parse Word document: {str(e)}'}), 500

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
            'error': 'Daily free limit reached (3/3). Upgrade to Premium for unlimited access.'
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
            "of the following document. Organize the summary with clear sections (e.g., Overview, Key Findings, "
            "Conclusion). Keep it professional and to the point.\n\nDocument:\n{text}"
        ),
        'keypoints': (
            "You are a professional document analyst. Please **extract the most important key points** from the "
            "following document. Present them as a **numbered list**. Each point should be specific, actionable, "
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

    prompt = PROMPTS[action].format(text=content, lang=target_lang)
    result, error = call_zhipu_ai(
        messages=[{'role': 'user', 'content': prompt}],
        temperature=0.5,
        max_tokens=4096
    )

    if error:
        return jsonify({'error': f'AI processing failed: {error}'}), 500

    return jsonify({'result': result, 'action': action})


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

# ── Entry Point ────────────────────────────────────────────────────────

if __name__ == '__main__':
    print("=" * 60)
    print("  Summarify Pro — AI Document Summarizer")
    print("  Server running at http://localhost:5000")
    print("=" * 60)
    app.run(debug=False, host='0.0.0.0', port=5000)