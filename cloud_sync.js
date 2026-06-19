/* ============================================================================
   cloud_sync.js — GenZet / Animind — Unified Auth + Cloud Sync (MVP)
   ============================================================================
   Replaces: auth_sync.js, genzet_auth_override.js, cloud_storage.js
   Include this ONE file as the last <script> before </body>.
   Remove the three old script tags — running them together caused duplicate
   init calls, conflicting state, and a course-save bug (wrong HTTP verb).

   Source of truth: Supabase, via the backend, keyed by user_id (auth.users.id).
     - animindLibrary      ← GET/POST/DELETE /sync/animations
     - engineeringCourses  ← GET/PUT         /sync/courses
     - vaultVideos         ← GET/PUT         /sync/vault

   No IndexedDB. No localStorage for app data. localStorage holds ONLY the
   session JWT + a cached display name/email, so a page refresh doesn't force
   a re-login — the data itself is always re-pulled from the cloud, never
   read from a local cache.

   On login / valid-session page load: pull all three from the cloud and
   REPLACE in-memory state (cloud wins — no merging with stale local arrays).
   On every mutation: push to the cloud immediately so other devices/tabs
   logged into the same account stay in sync.
   ============================================================================ */

(function () {
  'use strict';

  const BACKEND_URL = 'https://animind-backend-production.up.railway.app';
  const TOKEN_KEY = 'genzet_jwt';
  const USER_KEY  = 'genzet_user';

  window.authToken = window.authToken || null;
  window.authUser  = window.authUser  || null;

  // ── Low-level request helper ─────────────────────────────────────────────
  async function apiRequest(method, path, body) {
    const token = window.authToken;
    if (!token) return { ok: false, error: 'Not authenticated' };
    const opts = {
      method,
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    };
    if (body !== undefined) opts.body = JSON.stringify(body);
    try {
      const res = await fetch(`${BACKEND_URL}${path}`, opts);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        console.warn(`[SYNC] ${method} ${path} → HTTP ${res.status}`, data);
        return { ok: false, status: res.status, error: data.detail || `HTTP ${res.status}` };
      }
      return { ok: true, data };
    } catch (err) {
      console.warn(`[SYNC] ${method} ${path} network error:`, err.message);
      window._cloudOffline = true;
      return { ok: false, error: err.message };
    }
  }

  // ── Old auth-gate cleanup (idempotent, safe if that markup is gone) ──────
  function removeOldAuthGate() {
    if (document.getElementById('gz-auth-override-css')) return;
    const style = document.createElement('style');
    style.id = 'gz-auth-override-css';
    style.textContent = `
      #authGate, .ag-wrap, .ag-left, .ag-right, .ag-card,
      .ag-login-success-overlay, .ag-left-bg, .ag-anim-scene,
      [id="agLoginSuccessOverlay"] {
        display: none !important; visibility: hidden !important;
        pointer-events: none !important; opacity: 0 !important;
      }
      #authStatusBar { display: none; }
    `;
    document.head.appendChild(style);
    ['authGate', 'agLoginSuccessOverlay'].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.remove();
    });
  }

  // ── UI routing ────────────────────────────────────────────────────────────
  function showLanding() {
    const dash = document.getElementById('gz-dashboard');
    if (dash) { dash.style.display = 'none'; dash.style.opacity = '0'; dash.classList.remove('active'); }
    const landing = document.getElementById('gz-landing');
    if (landing) { landing.style.display = 'block'; landing.style.opacity = '1'; }
    setTimeout(() => { if (typeof openAuthModal === 'function') openAuthModal(); }, 250);
  }

  function enterDashboard() {
    if (typeof showDashboard === 'function') {
      showDashboard();
    } else {
      const landing = document.getElementById('gz-landing');
      if (landing) { landing.style.opacity = '0'; setTimeout(() => (landing.style.display = 'none'), 420); }
      const dash = document.getElementById('gz-dashboard');
      if (dash) { dash.style.display = 'block'; setTimeout(() => { dash.style.opacity = '1'; dash.classList.add('active'); }, 30); }
    }
    updateStatusBar();
  }

  function updateStatusBar() {
    const user = window.authUser;
    if (!user) return;
    const bar = document.getElementById('authStatusBar');
    if (bar) bar.style.display = 'flex';
    const nameEl = document.getElementById('authUserName');
    if (nameEl) nameEl.textContent = user.name || 'Teacher';
    const chipEl = document.getElementById('authUserIdChip');
    if (chipEl) {
      const uid = user.user_id;
      if (uid) {
        chipEl.textContent = 'ID: ' + uid.slice(0, 8) + '…';
        chipEl.style.display = 'inline-block';
        chipEl.title = 'Your User ID: ' + uid + ' — click to copy';
      } else {
        chipEl.style.display = 'none';
      }
    }
  }

  function setSyncStatus(msg) {
    const el = document.getElementById('authSyncStatus');
    if (el) el.textContent = msg;
  }

  // ── Session persistence — JWT + display profile only, never app data ─────
  function storeSession(data) {
    window.authToken = data.token;
    window.authUser  = { user_id: data.user_id, email: data.email, name: data.name };
    localStorage.setItem(TOKEN_KEY, window.authToken);
    localStorage.setItem(USER_KEY, JSON.stringify(window.authUser));
  }

  function clearSession() {
    window.authToken = null;
    window.authUser  = null;
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
    localStorage.removeItem('genzet_authenticated'); // legacy flag cleanup
  }

  // ══════════════════════════════════════════════════════════════════════
  // CloudStorage — single source of truth for ALL app data
  // ══════════════════════════════════════════════════════════════════════
  const CloudStorage = {

    // ── Animations / Library ────────────────────────────────────────────
    async loadLibrary() {
      const res = await apiRequest('GET', '/sync/animations');
      if (!res.ok) return { ok: false };
      window.animindLibrary = res.data.animations || []; // cloud REPLACES local state
      return { ok: true, data: window.animindLibrary };
    },

    async saveAnimation(anim) {
      if (!anim || !anim.id) return { ok: false, error: 'No animation' };
      const res = await apiRequest('POST', '/sync/animations', {
        id:             anim.id,
        title:          anim.title || 'Untitled',
        prompt:         anim.prompt || '',
        explanation:    anim.explanation || '',
        animation_code: anim.animation_code || '',
        playlist:       anim.playlist || 'General',
        created_at:     anim.created_at || new Date().toISOString(),
      });
      if (res.ok) {
        const lib = (window.animindLibrary = window.animindLibrary || []);
        const idx = lib.findIndex((a) => a.id === anim.id);
        if (idx >= 0) lib[idx] = { ...anim }; else lib.unshift({ ...anim });
      }
      return res;
    },

    async deleteAnimation(animId) {
      if (!animId) return { ok: false, error: 'No animId' };
      const res = await apiRequest('DELETE', `/sync/animations/${encodeURIComponent(animId)}`);
      if (res.ok) window.animindLibrary = (window.animindLibrary || []).filter((a) => a.id !== animId);
      return res;
    },

    async batchSaveAnimations(animations) {
      if (!animations || !animations.length) return { ok: true };
      return apiRequest('POST', '/sync/animations/batch', { animations });
    },

    // ── Engineering Courses ─────────────────────────────────────────────
    async loadCourses() {
      const res = await apiRequest('GET', '/sync/courses');
      if (!res.ok) return { ok: false };
      const courses = res.data.courses;
      window.engineeringCourses = (courses && courses.length)
        ? courses
        : (typeof window._getDefaultCourses === 'function' ? window._getDefaultCourses() : []);
      return { ok: true, data: window.engineeringCourses };
    },

    async saveCourses(courses) {
      if (!courses) return { ok: false, error: 'No courses' };
      // Strip heavy raw syllabus text before sending (keep payload small)
      const clean = courses.map((s) => ({
        ...s,
        cos: (s.cos || []).map((co) => ({ ...co, topics: [...(co.topics || [])] })),
        syllabus: s.syllabus ? { ...s.syllabus, raw: '' } : null,
      }));
      // Backend route is PUT — using POST here was the bug that silently
      // broke course saving.
      return apiRequest('PUT', '/sync/courses', { courses: clean });
    },

    // ── Video Vault ──────────────────────────────────────────────────────
    async loadVault() {
      const res = await apiRequest('GET', '/sync/vault');
      if (!res.ok) return { ok: false };
      window.vaultVideos = res.data.entries || [];
      return { ok: true, data: window.vaultVideos };
    },

    async saveVault(entries) {
      return apiRequest('PUT', '/sync/vault', { entries: entries || [] });
    },

    // ── Load everything at once (login / page load) ────────────────────
    async loadAll() {
      const [lib, courses, vault] = await Promise.all([
        this.loadLibrary(),
        this.loadCourses(),
        this.loadVault(),
      ]);
      if (typeof syncLibraryToFolders === 'function') syncLibraryToFolders();
      if (typeof renderSubjectsGrid  === 'function') renderSubjectsGrid();
      if (typeof showFolders         === 'function') showFolders();
      if (typeof updateActiveCount   === 'function') updateActiveCount();
      if (typeof vaultRenderGrid     === 'function') vaultRenderGrid();
      return { lib, courses, vault };
    },
  };
  window.CloudStorage = CloudStorage;

  // ── Debounced auto-save for courses/vault (avoid spamming the API) ──────
  let coursesTimer = null;
  function syncCoursesToCloud() {
    if (!window.authToken) return Promise.resolve();
    clearTimeout(coursesTimer);
    return new Promise((resolve) => {
      coursesTimer = setTimeout(async () => {
        await CloudStorage.saveCourses(window.engineeringCourses || []);
        resolve();
      }, 1200);
    });
  }

  let vaultTimer = null;
  function syncVaultToCloud() {
    if (!window.authToken) return Promise.resolve();
    clearTimeout(vaultTimer);
    return new Promise((resolve) => {
      vaultTimer = setTimeout(async () => {
        await CloudStorage.saveVault(window.vaultVideos || []);
        resolve();
      }, 800);
    });
  }

  // ══════════════════════════════════════════════════════════════════════
  // AUTH BOOTSTRAP
  // ══════════════════════════════════════════════════════════════════════
  async function authInit() {
    removeOldAuthGate();

    const legacyFlag = localStorage.getItem('genzet_authenticated');
    if (legacyFlag === 'true') localStorage.removeItem('genzet_authenticated');

    const storedToken = localStorage.getItem(TOKEN_KEY);
    if (!storedToken) { showLanding(); return; }

    setSyncStatus('Verifying session…');
    try {
      const res = await fetch(`${BACKEND_URL}/auth/verify`, {
        headers: { Authorization: `Bearer ${storedToken}` },
      });

      if (!res.ok) {
        clearSession();
        showLanding();
        return;
      }

      const profile = await res.json();
      window.authToken = storedToken;
      window.authUser  = { user_id: profile.user_id, email: profile.email, name: profile.name };
      localStorage.setItem(USER_KEY, JSON.stringify(window.authUser));

      enterDashboard();
      setSyncStatus('Syncing…');
      await CloudStorage.loadAll();
      setSyncStatus('');
    } catch (err) {
      console.warn('[AUTH] Backend unreachable:', err.message);
      // No local data cache to fall back on by design — data lives only in
      // the cloud. Let the user in with the cached profile so the UI isn't
      // dead, but library/courses/vault stay empty until reconnected.
      let cached = null;
      try { cached = JSON.parse(localStorage.getItem(USER_KEY) || 'null'); } catch (_) {}
      if (cached) {
        window.authToken = storedToken;
        window.authUser  = cached;
        enterDashboard();
        setSyncStatus('⚠ Offline — reconnect to load your data');
      } else {
        showLanding();
      }
    }
  }

  function logout() {
    if (!confirm('Sign out of GenZet? Your library is safely synced to the cloud.')) return;
    clearSession();
    window.animindLibrary     = [];
    window.engineeringCourses = [];
    window.vaultVideos        = [];
    if (typeof showFolders        === 'function') showFolders();
    if (typeof renderSubjectsGrid === 'function') renderSubjectsGrid();
    if (typeof vaultRenderGrid    === 'function') vaultRenderGrid();
    showLanding();
  }

  // Call this from the login/register modal right after a successful
  // POST /auth/login or /auth/register response.
  async function onAuthSuccess(data) {
    storeSession(data);
    enterDashboard();
    setSyncStatus('Syncing…');
    await CloudStorage.loadAll();
    setSyncStatus('');
  }

  // ══════════════════════════════════════════════════════════════════════
  // PUBLIC API — same names as before, so existing app code (saveToLibrary,
  // deleteCO, the login modal, etc.) keeps working without changes.
  // ══════════════════════════════════════════════════════════════════════
  window.authInit           = authInit;
  window.authStoreSession   = storeSession;
  window.authOnLoginSuccess = onAuthSuccess; // preferred: store + sync in one call
  window.authClearSession   = clearSession;
  window.authLogout         = logout;
  window.authUpdateStatusBar = updateStatusBar;
  window.authSetSyncStatus  = setSyncStatus;
  window.authShowGate       = showLanding;
  window.authHideGate       = enterDashboard;

  window.syncPushToCloud          = (anim)  => CloudStorage.saveAnimation(anim);
  window.syncPushAllToCloud       = (anims) => CloudStorage.batchSaveAnimations(anims || window.animindLibrary || []);
  window.syncPullFromCloud        = ()      => CloudStorage.loadAll();
  window.syncDeleteFromCloud      = (id)    => CloudStorage.deleteAnimation(id);
  window.syncCoursesToCloud       = syncCoursesToCloud;
  window.syncPullCoursesFromCloud = ()      => CloudStorage.loadCourses();
  window.syncPushVaultToCloud     = syncVaultToCloud;
  window.syncPullVaultFromCloud   = ()      => CloudStorage.loadVault();

  // Old IndexedDB-era call sites become safe no-ops / cloud redirects.
  window.initDB         = async () => null;
  window.saveLibraryIDB = async () => {};
  window.loadLibraryIDB = async () => { await CloudStorage.loadLibrary(); return window.animindLibrary || []; };
  window.saveECourses   = async () => { await CloudStorage.saveCourses(window.engineeringCourses || []); };
  window.loadECourses   = async () => { await CloudStorage.loadCourses(); return window.engineeringCourses || []; };

  // ── Bootstrap — exactly once ─────────────────────────────────────────────
  function boot() { setTimeout(authInit, 80); }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

  console.log('[CLOUD] cloud_sync.js loaded — cloud is the single source of truth');
})();
