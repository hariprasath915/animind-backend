// ═══════════════════════════════════════════════════════════════════════
// AUTH + CLOUD SYNC MANAGER — GenZet v4.1 (FIXED)
// OLD AUTH GATE REMOVED — Uses landing page dark modal ONLY
// ═══════════════════════════════════════════════════════════════════════

const BACKEND_URL = 'https://animind-backend-2.onrender.com';
const SUPABASE_URL      = 'https://fkincmzpteuibbegghti.supabase.co';
const SUPABASE_ANON_KEY = 'sb_publishable_9IRPmhzJbwX25VhmhY42zw_UB2i0h2b';
const sb = { auth: { signOut: () => Promise.resolve() } };

const TOKEN_KEY = 'genzet_jwt';
const USER_KEY  = 'genzet_user';

window.authUser  = null;
window.authToken = null;

// ══════════════════════════════════════════════════════════════════════
// INIT — checks JWT, either enters dashboard or shows landing + modal
// ══════════════════════════════════════════════════════════════════════
async function authInit() {
  // ── FIX: Legacy bridge (old sessions that set genzet_authenticated but no JWT) ──
  const landingAuth = localStorage.getItem('genzet_authenticated');
  const landingUser = (() => {
    try { return JSON.parse(localStorage.getItem('genzet_user') || 'null'); } catch { return null; }
  })();
  const hasJwt = !!localStorage.getItem(TOKEN_KEY);

  if (landingAuth === 'true' && landingUser && !hasJwt) {
    // Very old sessions — show landing with modal so user logs in properly
    localStorage.removeItem('genzet_authenticated');
    authShowLanding();
    return;
  }
  if (landingAuth === 'true' && hasJwt) {
    localStorage.removeItem('genzet_authenticated');
  }

  const storedToken = localStorage.getItem(TOKEN_KEY);
  if (!storedToken) {
    // No token — show landing page with auth modal
    authShowLanding();
    return;
  }

  // Verify token with backend
  authSetSyncStatus('Verifying session…');
  try {
    const res = await fetch(`${BACKEND_URL}/auth/verify`, {
      headers: { 'Authorization': `Bearer ${storedToken}` }
    });

    if (res.ok) {
      const profile = await res.json();
      window.authToken = storedToken;
      window.authUser  = { user_id: profile.user_id, email: profile.email, name: profile.name };
      localStorage.setItem(USER_KEY, JSON.stringify(window.authUser));

      // Valid token → go straight to dashboard
      authEnterDashboard();
      authSetSyncStatus('Syncing library…');
      await syncPullFromCloud();
      authSetSyncStatus('');

    } else {
      // Expired / invalid token
      authClearSession();
      authShowLanding();
    }
  } catch (err) {
    console.warn('[AUTH] Cannot reach backend:', err.message);
    const storedUser = (() => {
      try { return JSON.parse(localStorage.getItem(USER_KEY) || 'null'); } catch { return null; }
    })();
    if (storedUser) {
      // Offline mode — use cached data
      window.authToken = storedToken;
      window.authUser  = storedUser;
      authEnterDashboard();
      authUpdateStatusBar();
      authSetSyncStatus('Offline mode');
    } else {
      authShowLanding();
    }
  }
}

// ══════════════════════════════════════════════════════════════════════
// ROUTING HELPERS
// ══════════════════════════════════════════════════════════════════════

/** Show the landing page (and open the dark modal after a short delay) */
function authShowLanding() {
  // Hide dashboard completely
  const dash = document.getElementById('gz-dashboard');
  if (dash) { dash.style.display = 'none'; dash.style.opacity = '0'; dash.classList.remove('active'); }

  // Show landing page
  const landing = document.getElementById('gz-landing');
  if (landing) { landing.style.display = 'block'; landing.style.opacity = '1'; }

  // After landing renders, open the dark auth modal automatically
  setTimeout(() => {
    if (typeof openAuthModal === 'function') openAuthModal();
  }, 300);
}

/** Hide landing, show dashboard, update status bar */
function authEnterDashboard() {
  // The showDashboard() function on the landing page handles the transition
  if (typeof showDashboard === 'function') {
    showDashboard();
  } else {
    // Fallback: manual transition
    const landing = document.getElementById('gz-landing');
    if (landing) { landing.style.opacity = '0'; setTimeout(() => { landing.style.display = 'none'; }, 420); }
    const dash = document.getElementById('gz-dashboard');
    if (dash) { dash.style.display = 'block'; setTimeout(() => { dash.style.opacity = '1'; dash.classList.add('active'); }, 30); }
  }
  authUpdateStatusBar();
}

// Keep authShowGate as alias for backward compatibility
// but redirect to landing instead of old auth gate
function authShowGate() { authShowLanding(); }
function authHideGate() { authEnterDashboard(); }

// ── These functions are REMOVED (old auth gate form IDs no longer exist) ──
// authDoLogin, authDoRegister, authSwitchTab, agTogglePw, authShowMsg,
// authClearMsg, authSetBtnLoading — all handled by the landing modal now.

// ══════════════════════════════════════════════════════════════════════
// LOGOUT
// ══════════════════════════════════════════════════════════════════════
function authLogout() {
  if (!confirm('Sign out of GenZet? Your library is safely synced to the cloud.')) return;
  authClearSession();
  sb.auth.signOut().catch(e => console.warn('[AUTH] Supabase signOut error:', e.message));
  // Reset in-memory state — cloud data is already persisted in Supabase
  if (typeof animindLibrary !== 'undefined') animindLibrary = [];
  if (typeof engineeringCourses !== 'undefined') engineeringCourses = [];
  if (typeof showFolders === 'function') showFolders();
  if (typeof renderSubjectsGrid === 'function') renderSubjectsGrid();
  // Route back to landing page with modal
  authShowLanding();
}

// ══════════════════════════════════════════════════════════════════════
// SESSION PERSISTENCE
// ══════════════════════════════════════════════════════════════════════
function authStoreSession(data) {
  window.authToken = data.token;
  window.authUser  = { user_id: data.user_id, email: data.email, name: data.name };
  localStorage.setItem(TOKEN_KEY, window.authToken);
  localStorage.setItem(USER_KEY, JSON.stringify(window.authUser));
}

function authClearSession() {
  window.authToken = null;
  window.authUser  = null;
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
  localStorage.removeItem('genzet_authenticated');
}

function authUpdateStatusBar() {
  if (!window.authUser) return;
  const nameEl = document.getElementById('authUserName');
  if (nameEl) nameEl.textContent = window.authUser.name;

  const bar = document.getElementById('authStatusBar');
  if (bar) bar.style.display = 'flex';

  const chipEl = document.getElementById('authUserIdChip');
  if (chipEl) {
    const uid = window.authUser.user_id;
    if (uid) {
      chipEl.textContent = 'ID: ' + uid.slice(0, 8) + '…';
      chipEl.style.display = 'inline-block';
      chipEl.title = 'Your User ID: ' + uid + ' — click to copy';
    } else {
      chipEl.style.display = 'none';
    }
  }
}

function authSetSyncStatus(msg) {
  const el = document.getElementById('authSyncStatus');
  if (el) el.textContent = msg;
}

// ══════════════════════════════════════════════════════════════════════
// CLOUD SYNC — PULL
// ══════════════════════════════════════════════════════════════════════
async function syncPullFromCloud() {
  const authToken = window.authToken;
  if (!authToken) return;
  try {
    const res = await fetch(`${BACKEND_URL}/sync/animations`, {
      headers: { 'Authorization': `Bearer ${authToken}` }
    });
    if (!res.ok) { console.warn('[SYNC] Pull failed:', res.status); return; }
    const data = await res.json();
    const cloudAnims = data.animations || [];
    if (cloudAnims.length === 0) { console.log('[SYNC] Cloud library empty.'); return; }

    const localAnims = typeof animindLibrary !== 'undefined' ? [...animindLibrary] : [];
    const localById  = Object.fromEntries(localAnims.map(a => [a.id, a]));
    for (const ca of cloudAnims) localById[ca.id] = ca;
    const merged = Object.values(localById).sort((a, b) =>
      new Date(b.created_at || 0) - new Date(a.created_at || 0)
    );
    // Update global in-memory state (Supabase is the persistent store)
    if (typeof animindLibrary !== 'undefined') animindLibrary = merged;
    if (typeof syncLibraryToFolders === 'function') syncLibraryToFolders();
    if (typeof showFolders === 'function') showFolders();
    if (typeof updateActiveCount === 'function') updateActiveCount();
    console.log(`[SYNC] ✅ Pulled ${cloudAnims.length} animations.`);
    if (typeof notify === 'function') notify(`✅ ${cloudAnims.length} animations restored from cloud.`);
  } catch (err) {
    console.warn('[SYNC] Pull error:', err.message);
  }
  await syncPullCoursesFromCloud();
}

// ══════════════════════════════════════════════════════════════════════
// CLOUD SYNC — PUSH ONE
// ══════════════════════════════════════════════════════════════════════
async function syncPushToCloud(animation) {
  const authToken = window.authToken;
  if (!authToken || !animation) return;
  if (!window.authUser || !window.authUser.user_id) return;
  authSetSyncStatus('Syncing…');
  try {
    const res = await fetch(`${BACKEND_URL}/sync/animations`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${authToken}` },
      body: JSON.stringify({
        id: animation.id,
        title: animation.title || 'Untitled',
        prompt: animation.prompt || '',
        explanation: animation.explanation || '',
        animation_code: animation.animation_code || '',
        playlist: animation.playlist || 'General',
        created_at: animation.created_at || new Date().toISOString(),
      }),
    });
    if (res.ok) {
      authSetSyncStatus('☁ Synced');
      setTimeout(() => authSetSyncStatus(''), 3000);
    } else {
      authSetSyncStatus('⚠ Sync failed');
      setTimeout(() => authSetSyncStatus(''), 5000);
    }
  } catch (err) {
    console.warn('[SYNC] Push error:', err.message);
    authSetSyncStatus('⚠ Offline');
    setTimeout(() => authSetSyncStatus(''), 5000);
  }
}

// ══════════════════════════════════════════════════════════════════════
// CLOUD SYNC — PUSH ALL
// ══════════════════════════════════════════════════════════════════════
async function syncPushAllToCloud() {
  const authToken = window.authToken;
  if (!authToken) return;
  const anims = typeof animindLibrary !== 'undefined' ? animindLibrary : [];
  if (anims.length === 0) return;
  authSetSyncStatus(`Uploading ${anims.length} animations…`);
  try {
    const res = await fetch(`${BACKEND_URL}/sync/animations/batch`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${authToken}` },
      body: JSON.stringify({ animations: anims }),
    });
    if (res.ok) {
      const d = await res.json();
      authSetSyncStatus(`☁ ${d.synced} backed up`);
      setTimeout(() => authSetSyncStatus(''), 4000);
    } else {
      authSetSyncStatus('⚠ Batch sync failed');
      setTimeout(() => authSetSyncStatus(''), 5000);
    }
  } catch (err) {
    authSetSyncStatus('⚠ Offline');
    setTimeout(() => authSetSyncStatus(''), 5000);
  }
}

// ══════════════════════════════════════════════════════════════════════
// CLOUD SYNC — DELETE
// ══════════════════════════════════════════════════════════════════════
async function syncDeleteFromCloud(animId) {
  const authToken = window.authToken;
  if (!authToken || !animId) return;
  try {
    await fetch(`${BACKEND_URL}/sync/animations/${encodeURIComponent(animId)}`, {
      method: 'DELETE',
      headers: { 'Authorization': `Bearer ${authToken}` },
    });
  } catch (err) { console.warn('[SYNC] Delete error:', err.message); }
}

// ══════════════════════════════════════════════════════════════════════
// COURSES SYNC
// ══════════════════════════════════════════════════════════════════════
let _coursesSyncTimer = null;
async function syncCoursesToCloud() {
  const authToken = window.authToken;
  if (!authToken || !window.authUser?.user_id) return;
  if (_coursesSyncTimer) clearTimeout(_coursesSyncTimer);
  return new Promise(resolve => {
    _coursesSyncTimer = setTimeout(async () => {
      try {
        const courses = (typeof engineeringCourses !== 'undefined' ? engineeringCourses : [])
          .map(s => ({ ...s, cos: s.cos.map(co => ({ ...co, topics: co.topics.map(t => ({ ...t })) })), syllabus: s.syllabus ? { ...s.syllabus, raw: '' } : null }));
        const res = await fetch(`${BACKEND_URL}/sync/courses`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${authToken}` },
          body: JSON.stringify({ courses }),
        });
        if (res.ok) console.log('[SYNC] ✅ Courses synced.');
      } catch (err) { console.warn('[SYNC] Courses push error:', err.message); }
      resolve();
    }, 1500);
  });
}

async function syncPullCoursesFromCloud() {
  const authToken = window.authToken;
  if (!authToken) return;
  try {
    const res = await fetch(`${BACKEND_URL}/sync/courses`, {
      headers: { 'Authorization': `Bearer ${authToken}` }
    });
    if (!res.ok) return;
    const data = await res.json();
    const cloudCourses = data.courses;
    if (!cloudCourses || !Array.isArray(cloudCourses) || cloudCourses.length === 0) return;

    // Strip the old pre-seeded default course IDs that were auto-pushed
    // before users created their own subjects. These are now permanently removed.
    const LEGACY_DEFAULT_IDS = new Set(['ep', 'ec', 'em', 'ht', 'mc']);
    const filteredCloud = cloudCourses.filter(s => !LEGACY_DEFAULT_IDS.has(s.id));
    if (filteredCloud.length === 0) return;

    const localById = Object.fromEntries(
      (typeof engineeringCourses !== 'undefined' ? engineeringCourses : [])
        .filter(s => !LEGACY_DEFAULT_IDS.has(s.id))
        .map(s => [s.id, s])
    );
    for (const s of filteredCloud) localById[s.id] = s;
    // Update global in-memory state (Supabase is the persistent store)
    if (typeof engineeringCourses !== 'undefined') engineeringCourses = Object.values(localById);
    if (typeof renderSubjectsGrid === 'function') renderSubjectsGrid();
    if (typeof syncLibraryToFolders === 'function') syncLibraryToFolders();
    if (typeof showFolders === 'function') showFolders();
    if (typeof updateActiveCount === 'function') updateActiveCount();
  } catch (err) { console.warn('[SYNC] Courses pull error:', err.message); }
}

// ══════════════════════════════════════════════════════════════════════
// EXPOSE ON WINDOW
// ══════════════════════════════════════════════════════════════════════
window.syncPushToCloud          = syncPushToCloud;
window.syncPushAllToCloud       = syncPushAllToCloud;
window.syncPullFromCloud        = syncPullFromCloud;
window.syncDeleteFromCloud      = syncDeleteFromCloud;
window.syncCoursesToCloud       = syncCoursesToCloud;
window.syncPullCoursesFromCloud = syncPullCoursesFromCloud;

// ══════════════════════════════════════════════════════════════════════
// BOOTSTRAP
// ══════════════════════════════════════════════════════════════════════
document.addEventListener('DOMContentLoaded', () => {
  // Run after existing DOMContentLoaded handlers (IDB init, etc.)
  setTimeout(authInit, 80);
});
