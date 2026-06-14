/**
 * auth_sync.js  —  GenZet Auth + Cloud Sync Manager  v2.0
 * =========================================================
 * Replaces the old auth_sync script block from index.html.
 *
 * What changed from v1.x:
 *   REMOVED: syncPullFromCloud() — now CloudStorage.loadLibrary()
 *   REMOVED: syncPushToCloud()   — now CloudStorage.saveAnimation()
 *   REMOVED: syncDeleteFromCloud() — now CloudStorage.deleteAnimation()
 *   REMOVED: syncPushAllToCloud()  — now CloudStorage.batchSaveAnimations()
 *   ADDED:   Loads BOTH library AND engineering courses on login
 *   KEPT:    authInit(), authDoLogin(), authDoRegister(), authLogout()
 *            All auth gate UI functions (unchanged)
 *
 * Paste this entire file content as a <script> block in index.html,
 * AFTER cloud_storage.js and BEFORE the main app script.
 *
 * Dependencies (must load before this script):
 *   cloud_storage.js — defines window.CloudStorage
 */

// ── Configuration ──────────────────────────────────────────────────────────
// BACKEND_URL is already declared in cloud_storage.js — do not re-declare.

// localStorage keys
const TOKEN_KEY = 'genzet_jwt';
const USER_KEY  = 'genzet_user';

// Auth state — on window so ALL script blocks can read them
window.authUser  = null;   // { user_id, email, name }
window.authToken = null;   // Supabase JWT


// ══════════════════════════════════════════════════════════════════════
// SECTION 1: INITIALIZATION
// ══════════════════════════════════════════════════════════════════════

async function authInit() {
    // ── Landing-page bridge ────────────────────────────────────────
    // When user signs in on genzet-landing.html, it sets
    // genzet_authenticated + genzet_user in localStorage.
    const landingAuth = localStorage.getItem('genzet_authenticated');
    const landingUser = (() => {
        try { return JSON.parse(localStorage.getItem('genzet_user') || 'null'); } catch { return null; }
    })();
    if (landingAuth === 'true' && landingUser) {
        localStorage.removeItem('genzet_authenticated');
        window.authUser  = { user_id: null, email: landingUser.email || '', name: landingUser.name || landingUser.email?.split('@')[0] || 'Student' };
        window.authToken = null;
        authHideGate();
        authUpdateStatusBar();
        // No JWT → can't pull cloud data; app works with defaults
        return;
    }

    // ── Restore session from localStorage ──────────────────────────
    const storedToken = localStorage.getItem(TOKEN_KEY);
    const storedUser  = (() => {
        try { return JSON.parse(localStorage.getItem(USER_KEY) || 'null'); } catch { return null; }
    })();

    if (!storedToken) { authShowGate(); return; }

    // ── Verify token with backend ───────────────────────────────────
    CloudStorage.setSyncStatus('Verifying session…');
    try {
        const res = await fetch(`${BACKEND_URL}/auth/verify`, {
            headers: { 'Authorization': `Bearer ${storedToken}` }
        });

        if (res.ok) {
            const profile    = await res.json();
            window.authToken = storedToken;
            window.authUser  = { user_id: profile.user_id, email: profile.email, name: profile.name };
            localStorage.setItem(USER_KEY, JSON.stringify(window.authUser));
            authHideGate();
            authUpdateStatusBar();
            await _loadAllCloudData();
        } else {
            authClearSession();
            authShowGate();
        }
    } catch (err) {
        console.warn('[AUTH] Backend unreachable:', err.message);
        if (storedUser) {
            // Offline mode — use cached user, load nothing from cloud
            window.authToken = storedToken;
            window.authUser  = storedUser;
            authHideGate();
            authUpdateStatusBar();
            CloudStorage.setSyncStatus('Offline mode');
        } else {
            authShowGate();
        }
    }
}

/**
 * Load BOTH library AND engineering courses from cloud.
 * Called after every successful login / session restore.
 */
async function _loadAllCloudData() {
    CloudStorage.setSyncStatus('Loading your data…');

    // Run both fetches in parallel
    const [libRes, courseRes] = await Promise.allSettled([
        CloudStorage.loadLibrary(),
        CloudStorage.loadCourses(),
    ]);

    // Re-render UI after cloud data arrives
    if (typeof syncLibraryToFolders === 'function') syncLibraryToFolders();
    if (typeof showFolders          === 'function') showFolders();
    if (typeof renderSubjectsGrid   === 'function') renderSubjectsGrid();
    if (typeof updateActiveCount    === 'function') updateActiveCount();
    if (typeof updateStorageBadge   === 'function') updateStorageBadge();

    const libOk     = libRes.status === 'fulfilled' && libRes.value?.ok;
    const courseOk  = courseRes.status === 'fulfilled' && courseRes.value?.ok;

    if (libOk && courseOk) {
        CloudStorage.setSyncStatus('');
    } else if (!libOk && !courseOk) {
        CloudStorage.setSyncStatus('⚠ Offline');
        setTimeout(() => CloudStorage.setSyncStatus(''), 6000);
    } else {
        CloudStorage.setSyncStatus('');
    }
}


// ══════════════════════════════════════════════════════════════════════
// SECTION 2: AUTH GATE UI
// ══════════════════════════════════════════════════════════════════════

function authShowGate() {
    const gate = document.getElementById('authGate');
    if (gate) { gate.style.display = 'flex'; gate.style.animation = 'fadeIn .3s ease'; }
    const bar = document.getElementById('authStatusBar');
    if (bar) bar.style.display = 'none';
}

function authHideGate() {
    const gate = document.getElementById('authGate');
    if (gate) gate.style.display = 'none';
    const bar = document.getElementById('authStatusBar');
    if (bar) bar.style.display = 'flex';
}

function authSwitchTab(tab) {
    const isLogin = tab === 'login';
    document.getElementById('loginForm').style.display    = isLogin ? 'block' : 'none';
    document.getElementById('registerForm').style.display = isLogin ? 'none'  : 'block';
    authClearMsg();
}

function agTogglePw(inputId, btn) {
    const inp = document.getElementById(inputId);
    if (!inp) return;
    const show = inp.type === 'password';
    inp.type    = show ? 'text' : 'password';
    btn.textContent = show ? '🙈' : '👁';
}

function authShowMsg(msg, type = 'error') {
    const el = document.getElementById('authMsg');
    if (!el) return;
    el.textContent = msg;
    el.style.display    = 'block';
    el.style.background = type === 'error' ? 'var(--red-lt)'   : 'var(--green-lt)';
    el.style.color      = type === 'error' ? 'var(--red)'       : 'var(--green)';
    el.style.border     = `1.5px solid ${type === 'error' ? 'var(--red)' : 'var(--green)'}`;
}

function authClearMsg() {
    const el = document.getElementById('authMsg');
    if (el) el.style.display = 'none';
}

function authSetBtnLoading(btnId, loading, defaultText) {
    const btn = document.getElementById(btnId);
    if (!btn) return;
    btn.disabled       = loading;
    btn.textContent    = loading ? '…' : defaultText;
    btn.style.opacity  = loading ? '0.7' : '1';
    btn.style.cursor   = loading ? 'not-allowed' : 'pointer';
}


// ══════════════════════════════════════════════════════════════════════
// SECTION 3: REGISTER
// ══════════════════════════════════════════════════════════════════════

async function authDoRegister() {
    const name     = (document.getElementById('regName')?.value     || '').trim();
    const email    = (document.getElementById('regEmail')?.value    || '').trim();
    const password = (document.getElementById('regPassword')?.value || '').trim();

    if (!name)                return authShowMsg('Please enter your name.');
    if (!email)               return authShowMsg('Please enter your email.');
    if (!password)            return authShowMsg('Please create a password.');
    if (password.length < 6)  return authShowMsg('Password must be at least 6 characters.');

    authSetBtnLoading('registerBtn', true, 'Create Account →');
    authClearMsg();

    try {
        const res  = await fetch(`${BACKEND_URL}/auth/register`, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ name, email, password }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Registration failed.');

        authShowMsg(`Welcome, ${data.name}! Account created. Please sign in.`, 'success');
        setTimeout(() => { authSwitchTab('login'); authClearMsg(); }, 2000);

    } catch (err) {
        authShowMsg(err.message || 'Something went wrong. Please try again.');
    } finally {
        authSetBtnLoading('registerBtn', false, 'Create Account →');
    }
}


// ══════════════════════════════════════════════════════════════════════
// SECTION 4: LOGIN
// ══════════════════════════════════════════════════════════════════════

async function authDoLogin() {
    const email    = (document.getElementById('loginEmail')?.value    || '').trim();
    const password = (document.getElementById('loginPassword')?.value || '').trim();

    if (!email)    return authShowMsg('Please enter your email.');
    if (!password) return authShowMsg('Please enter your password.');

    const loginBtn = document.getElementById('loginBtn');
    if (loginBtn) {
        loginBtn.classList.remove('ag-login-pulse');
        void loginBtn.offsetWidth;
        loginBtn.classList.add('ag-login-pulse');
    }

    authSetBtnLoading('loginBtn', true, 'Sign In →');
    authClearMsg();

    try {
        const res  = await fetch(`${BACKEND_URL}/auth/login`, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ email, password }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Login failed.');

        // Store session
        window.authToken = data.token;
        window.authUser  = { user_id: data.user_id, email: data.email, name: data.name };
        localStorage.setItem(TOKEN_KEY, window.authToken);
        localStorage.setItem(USER_KEY,  JSON.stringify(window.authUser));

        // Success overlay
        const overlay     = document.getElementById('agLoginSuccessOverlay');
        const successText = overlay?.querySelector('.ag-success-text');
        if (successText) successText.textContent = `Welcome back, ${data.name}!`;
        if (overlay) overlay.classList.add('active');

        setTimeout(async () => {
            if (overlay) {
                overlay.style.transition = 'opacity .4s ease';
                overlay.style.opacity    = '0';
                setTimeout(() => {
                    overlay.classList.remove('active');
                    overlay.style.opacity    = '';
                    overlay.style.transition = '';
                }, 400);
            }
            authHideGate();
            authUpdateStatusBar();
            // ── PULL ALL CLOUD DATA (library + courses) ──────────
            await _loadAllCloudData();
        }, 1200);

    } catch (err) {
        authShowMsg(err.message || 'Something went wrong. Please try again.');
        if (loginBtn) loginBtn.classList.remove('ag-login-pulse');
    } finally {
        authSetBtnLoading('loginBtn', false, 'Sign In →');
    }
}


// ══════════════════════════════════════════════════════════════════════
// SECTION 5: LOGOUT
// ══════════════════════════════════════════════════════════════════════

function authLogout() {
    if (!confirm('Sign out of GenZet? Your library is safely synced to the cloud.')) return;

    authClearSession();

    // Reset in-memory state
    window.animindLibrary       = [];
    window.engineeringCourses   = window._getDefaultCourses ? window._getDefaultCourses() : [];
    window._cloudOffline        = false;

    // Re-render UI back to empty/default state
    if (typeof syncLibraryToFolders === 'function') syncLibraryToFolders();
    if (typeof showFolders          === 'function') showFolders();
    if (typeof renderLibrary        === 'function') renderLibrary();
    if (typeof renderSubjectsGrid   === 'function') renderSubjectsGrid();
    if (typeof updateActiveCount    === 'function') updateActiveCount();

    authShowGate();
    authSwitchTab('login');
}


// ══════════════════════════════════════════════════════════════════════
// SECTION 6: SESSION HELPERS
// ══════════════════════════════════════════════════════════════════════

function authClearSession() {
    window.authToken = null;
    window.authUser  = null;
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
}

function authUpdateStatusBar() {
    if (!window.authUser) return;
    const nameEl = document.getElementById('authUserName');
    if (nameEl) nameEl.textContent = window.authUser.name;
}

function authSetSyncStatus(msg) {
    CloudStorage.setSyncStatus(msg);
}


// ══════════════════════════════════════════════════════════════════════
// SECTION 7: LEGACY SYNC FUNCTION STUBS
// These are called by old code throughout index.html.
// They now delegate to CloudStorage instead of IDB.
// ══════════════════════════════════════════════════════════════════════

window.syncPushToCloud = async function (animation) {
    const res = await CloudStorage.saveAnimation(animation);
    if (res.ok)  CloudStorage.setSyncOk();
    else         CloudStorage.setSyncError();
    return res;
};

window.syncPullFromCloud = async function () {
    return CloudStorage.loadLibrary();
};

window.syncDeleteFromCloud = async function (animId) {
    return CloudStorage.deleteAnimation(animId);
};

window.syncPushAllToCloud = async function () {
    return CloudStorage.batchSaveAnimations(window.animindLibrary || []);
};


// ══════════════════════════════════════════════════════════════════════
// SECTION 8: BOOTSTRAP
// ══════════════════════════════════════════════════════════════════════

const _authStyle = document.createElement('style');
_authStyle.textContent = `@keyframes fadeIn { from { opacity:0; transform:translateY(8px); } to { opacity:1; transform:none; } }`;
document.head.appendChild(_authStyle);

document.addEventListener('DOMContentLoaded', () => {
    // Wait briefly so initDB() (now a no-op stub) and other DOMContentLoaded
    // handlers run first, then initialise auth.
    setTimeout(authInit, 300);
});