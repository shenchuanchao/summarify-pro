/**
 * Summarify Pro — Frontend Application Logic
 * Vanilla JavaScript — No framework dependencies
 */

// ── State ────────────────────────────────────────────────────────────────
const STATE = {
    token: localStorage.getItem('summarify_token') || null,
    user: JSON.parse(localStorage.getItem('summarify_user') || 'null'),
    parsedText: '',
    currentTab: 'pdf',
    pdfFile: null,
    wordFile: null,
    lastActionResult: ''
};

const API = (() => {
    const BASE = '';
    const headers = () => {
        const h = { 'Content-Type': 'application/json' };
        if (STATE.token) h['Authorization'] = `Bearer ${STATE.token}`;
        return h;
    };

    const handle = async (res) => {
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
        return data;
    };

    return {
        register: (email, password) =>
            fetch(`${BASE}/api/user/register`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email, password }) }).then(handle),
        login: (email, password) =>
            fetch(`${BASE}/api/user/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email, password }) }).then(handle),
        usage: () =>
            fetch(`${BASE}/api/user/usage`, { headers: headers() }).then(handle),
        parsePDF: (file) => {
            const fd = new FormData();
            fd.append('file', file);
            return fetch(`${BASE}/api/parse/pdf`, { method: 'POST', headers: { 'Authorization': `Bearer ${STATE.token}` }, body: fd }).then(handle);
        },
        parseWord: (file) => {
            const fd = new FormData();
            fd.append('file', file);
            return fetch(`${BASE}/api/parse/word`, { method: 'POST', headers: { 'Authorization': `Bearer ${STATE.token}` }, body: fd }).then(handle);
        },
        parseURL: (url) =>
            fetch(`${BASE}/api/parse/url`, { method: 'POST', headers: headers(), body: JSON.stringify({ url }) }).then(handle),
        aiGenerate: (action, content, targetLang) =>
            fetch(`${BASE}/api/ai/generate`, { method: 'POST', headers: headers(), body: JSON.stringify({ action, content, target_lang: targetLang || 'Chinese' }) }).then(handle),
        getStripeStatus: () => fetch(`${BASE}/api/stripe/get-status`).then(handle),
        createCheckout: () => fetch(`${BASE}/api/stripe/create-checkout-session`, { method: 'POST', headers: { 'Authorization': `Bearer ${STATE.token}` } }).then(handle),
        verifyCheckout: (sessionId) => fetch(`${BASE}/api/stripe/verify-session?session_id=${sessionId}`, { headers: { 'Authorization': `Bearer ${STATE.token}` } }).then(handle)
    };
})();

// ── Stripe ─────────────────────────────────────────────────────
function upgradeToPremium() {
    toast('Premium subscription is coming soon. Stay tuned!', 'info');
}

async function handleStripeReturn() {
    const params = new URLSearchParams(window.location.search);
    const checkout = params.get('checkout');
    if (checkout === 'success') {
        const sessionId = params.get('session_id');
        if (sessionId) {
            try {
                const data = await API.verifyCheckout(sessionId);
                if (data && data.success) {
                    if (STATE.user) STATE.user.plan = 'premium';
                    localStorage.setItem('summarify_user', JSON.stringify(STATE.user));
                    updateUI();
                    loadUsage();
                    toast('🎉 Welcome to Premium!', 'success');
                    window.history.replaceState({}, document.title, window.location.pathname);
                }
            } catch (e) {
                toast(e.message || 'Verification failed', 'error');
            }
        }
    } else if (checkout === 'cancelled') {
        toast('Payment cancelled. You can upgrade anytime!', 'info');
        window.history.replaceState({}, document.title, window.location.pathname);
    }
}

// ── Toast ───────────────────────────────────────────────────────────────
let _toastTimer;
function toast(msg, type = 'info') {
    const el = document.getElementById('toast');
    clearTimeout(_toastTimer);
    el.textContent = msg;
    el.className = `fixed bottom-6 right-6 z-[200] px-5 py-3 rounded-xl shadow-xl text-sm font-medium max-w-xs animate-fade-in ${
        type === 'error' ? 'bg-red-600 text-white' :
        type === 'success' ? 'bg-emerald-600 text-white' :
        'bg-gray-900 text-white'
    }`;
    _toastTimer = setTimeout(() => { el.classList.add('hidden'); }, 4000);
}

// ── Auth ────────────────────────────────────────────────────────────────
function updateUI() {
    const navAuth = document.getElementById('nav-auth');
    const navUser = document.getElementById('nav-user');
    const userEmail = document.getElementById('user-email');
    const usageBadge = document.getElementById('usage-badge');
    const planBadge = document.getElementById('plan-badge');
    const upgradeBtn = document.getElementById('upgrade-btn');

    if (STATE.token && STATE.user) {
        navAuth.classList.add('hidden');
        navUser.classList.remove('hidden');
        userEmail.textContent = STATE.user.email;

        if (STATE.user.plan === 'premium') {
            if (planBadge) planBadge.classList.remove('hidden');
            if (upgradeBtn) upgradeBtn.classList.add('hidden');
        } else {
            if (planBadge) planBadge.classList.add('hidden');
            if (upgradeBtn) upgradeBtn.classList.remove('hidden');
        }

        loadUsage();
    } else {
        navAuth.classList.remove('hidden');
        navUser.classList.add('hidden');
        usageBadge.textContent = '';
        if (upgradeBtn) upgradeBtn.classList.add('hidden');
        if (planBadge) planBadge.classList.add('hidden');
    }
}

async function loadUsage() {
    if (!STATE.token) return;
    try {
        const data = await API.usage();
        const badge = document.getElementById('usage-badge');
        if (data.plan === 'premium') {
            badge.classList.add('hidden');
        } else {
            badge.textContent = `${data.remaining} / ${data.daily_limit} free`;
            badge.className = data.remaining === 0
                ? 'text-xs font-medium bg-red-50 text-red-700 px-3 py-1.5 rounded-full border border-red-200'
                : 'text-xs font-medium bg-brand-50 text-brand-700 px-3 py-1.5 rounded-full border border-brand-200';
        }
    } catch (e) {
        // silently fail
    }
}

async function handleLogin(e) {
    e.preventDefault();
    const email = document.getElementById('login-email').value.trim();
    const password = document.getElementById('login-password').value.trim();
    const btn = e.target.querySelector('button[type=submit]');
    btn.disabled = true;
    btn.textContent = 'Logging in...';
    try {
        const data = await API.login(email, password);
        STATE.token = data.token;
        STATE.user = data.user;
        localStorage.setItem('summarify_token', data.token);
        localStorage.setItem('summarify_user', JSON.stringify(data.user));
        updateUI();
        hideModal('login');
        toast('Logged in successfully!', 'success');
    } catch (e) {
        toast(e.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Log in';
    }
}

async function handleRegister(e) {
    e.preventDefault();
    const email = document.getElementById('register-email').value.trim();
    const password = document.getElementById('register-password').value.trim();
    const btn = e.target.querySelector('button[type=submit]');
    btn.disabled = true;
    btn.textContent = 'Creating account...';
    try {
        const data = await API.register(email, password);
        STATE.token = data.token;
        STATE.user = data.user;
        localStorage.setItem('summarify_token', data.token);
        localStorage.setItem('summarify_user', JSON.stringify(data.user));
        updateUI();
        hideModal('register');
        toast('Account created! You have 3 free uses today.', 'success');
    } catch (e) {
        toast(e.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Create account';
    }
}

function logout() {
    STATE.token = null;
    STATE.user = null;
    STATE.parsedText = '';
    localStorage.removeItem('summarify_token');
    localStorage.removeItem('summarify_user');
    updateUI();
    document.getElementById('parsed-section').classList.add('hidden');
    document.getElementById('result-section').classList.add('hidden');
    toast('Signed out');
}

// ── Modals ──────────────────────────────────────────────────────────────
function showModal(type) {
    document.getElementById(`modal-${type}`).classList.remove('hidden');
}
function hideModal(type) {
    document.getElementById(`modal-${type}`).classList.add('hidden');
    // clear forms
    const form = document.getElementById(`${type}-form`);
    if (form) form.reset();
}
function switchModal(to) {
    hideModal('login');
    hideModal('register');
    showModal(to);
}

// ── Tab Switching ──────────────────────────────────────────────────────
function switchTab(tab) {
    STATE.currentTab = tab;
    document.querySelectorAll('.tab-btn').forEach(b => {
        if (b.dataset.tab === tab) {
            b.classList.add('active', 'bg-white', 'text-brand-700', 'shadow-sm');
            b.classList.remove('text-gray-500');
        } else {
            b.classList.remove('active', 'bg-white', 'text-brand-700', 'shadow-sm');
            b.classList.add('text-gray-500');
        }
    });
    document.querySelectorAll('.tab-content').forEach(c => c.classList.add('hidden'));
    document.getElementById(`tab-${tab}`).classList.remove('hidden');
    updateParseButton();
}

document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
});

// ── File Upload ────────────────────────────────────────────────────────
function setupDropzone(type) {
    const dz = document.getElementById(`${type}-dropzone`);
    const input = document.getElementById(`${type}-input`);
    const info = document.getElementById(`${type}-file-info`);
    const name = document.getElementById(`${type}-file-name`);

    dz.addEventListener('click', () => input.click());
    dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('drag-over'); });
    dz.addEventListener('dragleave', () => dz.classList.remove('drag-over'));
    dz.addEventListener('drop', e => {
        e.preventDefault();
        dz.classList.remove('drag-over');
        const file = e.dataTransfer.files[0];
        if (file) setFile(type, file);
    });
    input.addEventListener('change', () => {
        if (input.files[0]) setFile(type, input.files[0]);
    });
}

function setFile(type, file) {
    if (type === 'pdf') STATE.pdfFile = file;
    else STATE.wordFile = file;
    const info = document.getElementById(`${type}-file-info`);
    const name = document.getElementById(`${type}-file-name`);
    name.textContent = file.name;
    info.classList.remove('hidden');
    document.getElementById(`${type}-dropzone`).classList.add('hidden');
    updateParseButton();
}

function clearFile(type) {
    if (type === 'pdf') STATE.pdfFile = null;
    else STATE.wordFile = null;
    document.getElementById(`${type}-file-info`).classList.add('hidden');
    document.getElementById(`${type}-dropzone`).classList.remove('hidden');
    document.getElementById(`${type}-input`).value = '';
    updateParseButton();
}

function updateParseButton() {
    const actions = document.getElementById('parse-actions');
    const shouldShow = (STATE.currentTab === 'pdf' && STATE.pdfFile) ||
                       (STATE.currentTab === 'word' && STATE.wordFile) ||
                       STATE.currentTab === 'url';
    if (shouldShow) actions.classList.remove('hidden');
    else actions.classList.add('hidden');
}

// ── Parse ────────────────────────────────────────────────────────────────
async function parseURL() {
    if (!requireAuth()) return;
    const url = document.getElementById('url-input').value.trim();
    if (!url) { toast('Please enter a URL', 'error'); return; }
    if (!url.startsWith('http')) { toast('URL must start with http:// or https://', 'error'); return; }

    showParseLoading(true);
    try {
        const data = await API.parseURL(url);
        showParsedText(data);
        loadUsage();
        toast('URL content extracted!', 'success');
    } catch (e) {
        toast(e.message, 'error');
    } finally {
        showParseLoading(false);
    }
}

async function handleParse() {
    if (!requireAuth()) return;
    showParseLoading(true);
    try {
        let data;
        if (STATE.currentTab === 'pdf') {
            if (!STATE.pdfFile) { toast('Please select a PDF file', 'error'); return; }
            data = await API.parsePDF(STATE.pdfFile);
        } else if (STATE.currentTab === 'word') {
            if (!STATE.wordFile) { toast('Please select a Word file', 'error'); return; }
            data = await API.parseWord(STATE.wordFile);
        } else {
            return parseURL();
        }
        showParsedText(data);
        loadUsage();
        toast('Text extracted successfully!', 'success');
    } catch (e) {
        toast(e.message, 'error');
    } finally {
        showParseLoading(false);
    }
}

document.getElementById('parse-btn').addEventListener('click', handleParse);

function showParseLoading(show) {
    document.getElementById('parse-loading').classList.toggle('hidden', !show);
    const btn = document.getElementById('parse-btn');
    if (btn) btn.disabled = show;
}

function showParsedText(data) {
    STATE.parsedText = data.text;
    document.getElementById('parsed-content').textContent = data.text;
    document.getElementById('parsed-word-count').textContent = `${data.word_count || data.text.split(/\s+/).length} words`;
    document.getElementById('parsed-section').classList.remove('hidden');
    document.getElementById('result-section').classList.add('hidden');
}

// ── AI Actions ──────────────────────────────────────────────────────────
async function runAI(action) {
    if (!requireAuth()) return;
    if (!STATE.parsedText) { toast('Please extract text first', 'error'); return; }

    const targetLang = document.getElementById('target-lang').value;

    // Show loading
    document.getElementById('result-content').classList.add('hidden');
    document.getElementById('result-loading').classList.remove('hidden');
    document.getElementById('result-section').classList.remove('hidden');

    const titles = { summarize: 'AI Summary', keypoints: 'Key Points', translate: `Translation (${targetLang})` };
    document.getElementById('result-title').textContent = titles[action] || 'AI Result';

    // Disable all AI buttons
    document.querySelectorAll('.ai-btn').forEach(b => b.disabled = true);

    try {
        const data = await API.aiGenerate(action, STATE.parsedText, targetLang);
        STATE.lastActionResult = data.result;
        document.getElementById('result-content').textContent = data.result;
        document.getElementById('result-content').classList.remove('hidden');
        document.getElementById('result-loading').classList.add('hidden');
        document.getElementById('result-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (e) {
        toast(e.message, 'error');
        document.getElementById('result-section').classList.add('hidden');
    } finally {
        document.querySelectorAll('.ai-btn').forEach(b => b.disabled = false);
    }
}

// ── Result Actions ─────────────────────────────────────────────────────
async function copyResult() {
    const text = STATE.lastActionResult || document.getElementById('result-content').textContent;
    if (!text) { toast('Nothing to copy', 'error'); return; }
    try {
        await navigator.clipboard.writeText(text);
        toast('Copied to clipboard!', 'success');
    } catch {
        // fallback
        const ta = document.createElement('textarea');
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        toast('Copied to clipboard!', 'success');
    }
}

function downloadResult() {
    const text = STATE.lastActionResult || document.getElementById('result-content').textContent;
    if (!text) { toast('Nothing to download', 'error'); return; }
    const blob = new Blob([text], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `summarify-result-${Date.now()}.txt`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    toast('Downloaded!', 'success');
}

// ── Auth Check ─────────────────────────────────────────────────────────
function requireAuth() {
    if (!STATE.token) {
        toast('Please log in or create an account first', 'info');
        showModal('login');
        return false;
    }
    return true;
}

// URL input enter key handler
document.getElementById('url-input')?.addEventListener('keydown', e => {
    if (e.key === 'Enter') parseURL();
});

// ── Init ────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    handleStripeReturn();
    setupDropzone('pdf');
    setupDropzone('word');
    updateUI();
    updateParseButton();

    // Scroll shadow on navbar
    window.addEventListener('scroll', () => {
        document.getElementById('navbar').classList.toggle('scrolled', window.scrollY > 10);
    });

    // Close modals on Escape
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape') {
            hideModal('login');
            hideModal('register');
        }
    });
});

// ── Social Share ────────────────────────────────────────────────────────

const SHARE_TITLE = encodeURIComponent('Summarify Pro — AI Document Summarizer');
const SHARE_HASHTAGS = encodeURIComponent('AI,Summarizer,Productivity');

function shareOn(platform) {
    const url = encodeURIComponent(window.location.href);
    const title = SHARE_TITLE;
    const text = encodeURIComponent('Summarize documents, extract key points, and translate with AI — all in seconds. Try it now!');

    let shareUrl = '';
    switch (platform) {
        case 'twitter':
            shareUrl = `https://twitter.com/intent/tweet?url=${url}&text=${text}&hashtags=AI,Summarizer,Productivity`;
            break;
        case 'facebook':
            shareUrl = `https://www.facebook.com/sharer/sharer.php?u=${url}&quote=${text}`;
            break;
        case 'linkedin':
            shareUrl = `https://www.linkedin.com/sharing/share-offsite/?url=${url}`;
            break;
        case 'reddit':
            shareUrl = `https://www.reddit.com/submit?url=${url}&title=${title}`;
            break;
        case 'whatsapp':
            shareUrl = `https://wa.me/?text=${text}%20${url}`;
            break;
    }

    if (shareUrl) {
        window.open(shareUrl, '_blank', 'width=600,height=400');
    }
}

function copyShareLink() {
    const url = window.location.href;
    if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(url).then(() => {
            toast('Link copied to clipboard!', 'success');
        }).catch(() => {
            toast('Failed to copy link', 'error');
        });
    } else {
        // Fallback for HTTP / insecure contexts
        const ta = document.createElement('textarea');
        ta.value = url;
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.select();
        try {
            document.execCommand('copy');
            toast('Link copied to clipboard!', 'success');
        } catch (e) {
            toast('Failed to copy link', 'error');
        }
        document.body.removeChild(ta);
    }
}