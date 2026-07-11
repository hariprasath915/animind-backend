/**
 * cloud_storage.js  —  GenZet Cloud Storage Manager  v2.0
 * =========================================================
 * Replaces ALL IndexedDB / localStorage data storage with Supabase cloud.
 *
 * What changed from v1.x:
 *   REMOVED: initDB(), idbSet(), idbGet(), saveLibraryIDB(), loadLibraryIDB()
 *            saveECourses() via IDB, loadECourses() via IDB
 *   ADDED:   CloudStorage class (singleton) that wraps all CRUD via
 *            the backend API (/sync/animations, /sync/courses)
 *   KEPT:    Same public API surface so existing callers need minimal changes
 *
 * Usage (paste this <script> block into index.html BEFORE the main app script):
 *
 *   // After user logs in and window.authToken is set:
 *   await CloudStorage.loadLibrary();      // fills window.animindLibrary
 *   await CloudStorage.loadCourses();      // fills window.engineeringCourses
 *
 *   // After user saves an animation:
 *   await CloudStorage.saveAnimation(animObj);
 *
 *   // After any change to engineering courses:
 *   await CloudStorage.saveCourses(window.engineeringCourses);
 *
 *   // After user deletes an animation:
 *   await CloudStorage.deleteAnimation(animId);
 *
 * All methods are async and return { ok: true/false, data?, error? }.
 * They silently handle network errors and set window._cloudOffline = true.
 */

const BACKEND_URL = (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1') 
  ? 'http://127.0.0.1:8000' 
  : '/api';

// ── Tiny request helper ────────────────────────────────────────────────────
async function _cloudRequest(method, path, body = null) {
    const token = window.authToken;
    if (!token) {
        console.warn('[CLOUD] No authToken — skipping', method, path);
        return { ok: false, error: 'Not authenticated' };
    }

    const opts = {
        method,
        headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${token}`,
        },
    };
    if (body !== null) opts.body = JSON.stringify(body);

    try {
        const res = await fetch(`${BACKEND_URL}${path}`, opts);
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            console.warn(`[CLOUD] ${method} ${path} → HTTP ${res.status}`, data);
            return { ok: false, error: data.detail || `HTTP ${res.status}` };
        }
        return { ok: true, data };
    } catch (err) {
        console.warn(`[CLOUD] ${method} ${path} network error:`, err.message);
        window._cloudOffline = true;
        return { ok: false, error: err.message };
    }
}

// ── CloudStorage singleton ─────────────────────────────────────────────────
window.CloudStorage = {

    // ── ANIMATIONS LIBRARY ─────────────────────────────────────────────────

    /**
     * Pull all animations from cloud and populate window.animindLibrary.
     * Called on login / page load.
     */
    async loadLibrary() {
        const res = await _cloudRequest('GET', '/sync/animations');
        if (!res.ok) {
            console.warn('[CLOUD] loadLibrary failed:', res.error);
            return { ok: false };
        }
        const cloud = res.data.animations || [];
        window.animindLibrary = cloud;
        console.log(`[CLOUD] ✅ Library loaded: ${cloud.length} animations`);
        return { ok: true, data: cloud };
    },

    /**
     * Save one animation to cloud.
     * @param {Object} anim — { id, title, prompt, explanation, animation_code, playlist, created_at }
     */
    async saveAnimation(anim) {
        if (!anim || !anim.id) return { ok: false, error: 'No animation' };
        const res = await _cloudRequest('POST', '/sync/animations', {
            id:             anim.id,
            title:          anim.title || 'Untitled',
            prompt:         anim.prompt || '',
            explanation:    anim.explanation || '',
            animation_code: anim.animation_code || '',
            playlist:       anim.playlist || 'General',
            created_at:     anim.created_at || new Date().toISOString(),
        });
        if (res.ok) {
            console.log(`[CLOUD] ✅ Animation saved: ${anim.id}`);
            // Keep window.animindLibrary in sync
            const idx = (window.animindLibrary || []).findIndex(a => a.id === anim.id);
            if (idx >= 0) window.animindLibrary[idx] = { ...anim };
            else (window.animindLibrary = window.animindLibrary || []).unshift({ ...anim });
        }
        return res;
    },

    /**
     * Delete one animation from cloud.
     * @param {string} animId
     */
    async deleteAnimation(animId) {
        if (!animId) return { ok: false, error: 'No animId' };
        const res = await _cloudRequest('DELETE', `/sync/animations/${encodeURIComponent(animId)}`);
        if (res.ok) {
            window.animindLibrary = (window.animindLibrary || []).filter(a => a.id !== animId);
            console.log(`[CLOUD] 🗑 Animation deleted: ${animId}`);
        }
        return res;
    },

    /**
     * Push all local animations to cloud (used on first login / new device).
     * @param {Array} animations
     */
    async batchSaveAnimations(animations) {
        if (!animations || animations.length === 0) return { ok: true };
        const res = await _cloudRequest('POST', '/sync/animations/batch', {
            animations: animations.map(a => ({
                id:             a.id,
                title:          a.title || 'Untitled',
                prompt:         a.prompt || '',
                explanation:    a.explanation || '',
                animation_code: a.animation_code || '',
                playlist:       a.playlist || 'General',
                created_at:     a.created_at || new Date().toISOString(),
            })),
        });
        if (res.ok) console.log(`[CLOUD] ✅ Batch saved: ${animations.length} animations`);
        return res;
    },


    // ── ENGINEERING COURSES ────────────────────────────────────────────────

    /**
     * Pull engineering courses from cloud and populate window.engineeringCourses.
     * Called on login / page load.
     * If no courses exist in cloud, returns the default courses list.
     */
    async loadCourses() {
        const res = await _cloudRequest('GET', '/sync/courses');
        if (!res.ok) {
            console.warn('[CLOUD] loadCourses failed:', res.error);
            // Fall back to defaults so app still works offline
            if (!window.engineeringCourses || window.engineeringCourses.length === 0) {
                window.engineeringCourses = window._getDefaultCourses ? window._getDefaultCourses() : [];
            }
            return { ok: false };
        }
        const courses = res.data.courses || [];
        if (courses.length > 0) {
            window.engineeringCourses = courses;
            console.log(`[CLOUD] ✅ Courses loaded: ${courses.length} subjects`);
        } else {
            // First time — no courses in cloud yet; use defaults
            if (!window.engineeringCourses || window.engineeringCourses.length === 0) {
                window.engineeringCourses = window._getDefaultCourses ? window._getDefaultCourses() : [];
            }
            console.log('[CLOUD] No courses in cloud yet — using defaults');
        }
        return { ok: true, data: window.engineeringCourses };
    },

    /**
     * Push the full engineering courses state to cloud.
     * Call this after ANY change to subjects, COs, or topics.
     * @param {Array} courses — full engineeringCourses array
     */
    async saveCourses(courses) {
        if (!courses) return { ok: false, error: 'No courses' };
        const res = await _cloudRequest('POST', '/sync/courses', { courses });
        if (res.ok) {
            const n = courses.length;
            const t = courses.reduce((s, sub) =>
                s + sub.cos.reduce((cs, co) => cs + (co.topics || []).length, 0), 0);
            console.log(`[CLOUD] ✅ Courses saved: ${n} subjects, ${t} topics`);
        } else {
            console.warn('[CLOUD] saveCourses failed:', res.error);
        }
        return res;
    },


    // ── SYNC STATUS UI ─────────────────────────────────────────────────────

    /** Show a brief sync indicator in the topbar. */
    setSyncStatus(msg) {
        const el = document.getElementById('authSyncStatus');
        if (el) el.textContent = msg;
    },

    setSyncOk(msg = '☁ Synced') {
        this.setSyncStatus(msg);
        setTimeout(() => this.setSyncStatus(''), 3000);
    },

    setSyncError(msg = '⚠ Sync failed') {
        this.setSyncStatus(msg);
        setTimeout(() => this.setSyncStatus(''), 5000);
    },
};


// ── Legacy function stubs (keep for any missed call sites) ────────────────
// These redirect to the new CloudStorage API so nothing breaks.

window.saveLibraryIDB = async function () {
    // No-op: library is now saved to Supabase individually on each saveAnimation()
    // This stub exists only so old call sites don't crash.
    console.debug('[COMPAT] saveLibraryIDB() called — now a no-op (Supabase handles storage)');
};

window.loadLibraryIDB = async function () {
    // Pull from cloud instead of IDB
    await CloudStorage.loadLibrary();
    return window.animindLibrary || [];
};

window.saveECourses = async function () {
    // Redirect to cloud save
    await CloudStorage.saveCourses(window.engineeringCourses || []);
};

window.loadECourses = async function () {
    await CloudStorage.loadCourses();
    return window.engineeringCourses || [];
};

// initDB() stub — no longer needed (no IndexedDB)
window.initDB = async function () {
    console.debug('[COMPAT] initDB() called — IndexedDB removed, using Supabase cloud');
    return Promise.resolve(null);
};

console.log('[CLOUD] cloud_storage.js v2.0 loaded — IndexedDB replaced by Supabase');
