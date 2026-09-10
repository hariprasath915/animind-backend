/* ============================================================================
   cloud_sync_v3.js  —  GenZet / Animind  v3.0
   ============================================================================
   REPLACES:  genzet_auth_override.js  +  auth_sync.js  +  cloud_sync.js
              (remove ALL three old script tags — use this ONE file instead)

   ADD as the LAST <script> tag before </body> in index.html:
     <script src="cloud_sync_v3.js"></script>

   WHAT'S NEW vs v2 (genzet_auth_override.js / cloud_sync.js):
     ✅ Single GET /sync/all on login  — one round-trip loads everything
     ✅ POST /sync/items               — typed save (ai_creator/book_mode/question_anim/topic_content)
     ✅ POST /sync/subjects + /cos + /topics  — per-row normalized writes
     ✅ POST /sync/vault/entries       — per-row vault entries (no more full-blob PUT)
     ✅ Legacy /sync/animations + /sync/courses + /sync/vault kept as fallback
     ✅ window.* API surface IDENTICAL to v2 — zero changes needed in index.html
        (except replacing the script filename)

   LOCALSTORAGE POLICY:
     ONLY  haezet_jwt      — auth token (needed for pre-paint gate)
     ONLY  haezet_user     — cached name/email for offline display
     ONLY  genzet_theme    — dark/light preference  (UI state, fine)
     ONLY  genzet_searches — search autocomplete history  (UI state, fine)
     ALL   app data (animations, courses, vault) comes from cloud only.
   ============================================================================ */

(function () {
  'use strict';

  const BACKEND = (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1')
    ? 'http://127.0.0.1:8000'
    : '/api';
  const TOKEN_KEY = 'haezet_jwt';
  const USER_KEY  = 'haezet_user';

  // Expose on window immediately so inline handlers can call them
  window.authToken = window.authToken || null;
  window.authUser  = window.authUser  || null;

  // ── BUGFIX: distinguish "server rejected the request" from "the request
  // never made it there at all" ───────────────────────────────────────────
  // A plain `catch (err) { ... err.message ... }` shows the raw browser
  // string "Failed to fetch" for BOTH a dead backend and a CORS rejection —
  // that's why the UI and console gave no clue which one was happening.
  // fetch() only ever throws a TypeError for network/CORS-level failures
  // (a real HTTP error, even a 500, resolves normally and is handled by the
  // `!res.ok` branch instead) — so `err instanceof TypeError` reliably
  // identifies this case and lets us log something actionable.
  function _isNetworkFailure(err) {
    return err instanceof TypeError;
  }
  function _logNetworkFailure(label) {
    console.error(
      `[CLOUD] ${label} could not reach ${BACKEND} — this is a connectivity ` +
      `or CORS problem, not a login/password/data problem. Check: (1) is the ` +
      `backend actually up — open ${BACKEND}/docs directly, (2) does the ` +
      `backend's CORS config allow-list this exact origin (${window.location.origin}).`
    );
  }

  // ── Token-refresh lock: prevents a stampede when multiple 401s fire at once ──
  let _refreshInProgress = null;   // Promise | null

  /**
   * _refreshToken()
   * Calls GET /auth/refresh with the current (possibly expired) Bearer token.
   * The backend exchanges it with Supabase for a fresh JWT and returns
   * { token, user_id, email, name }.
   * Updates window.authToken + localStorage so every subsequent call uses
   * the new token.
   * Returns the new token string, or null on failure.
   */
  async function _refreshToken() {
    // Deduplicate: if a refresh is already running, piggyback on it
    if (_refreshInProgress) return _refreshInProgress;

    _refreshInProgress = (async () => {
      const oldToken = window.authToken;
      if (!oldToken) return null;
      try {
        const res = await fetch(`${BACKEND}/auth/refresh`, {
          method:  'GET',
          headers: { 'Authorization': `Bearer ${oldToken}` },
        });
        if (!res.ok) {
          console.warn('[AUTH] Token refresh failed —', res.status, '— forcing re-login');
          _clearSession();
          _showLanding();
          return null;
        }
        const data = await res.json().catch(() => ({}));
        if (!data.token) {
          console.warn('[AUTH] Refresh response missing token — forcing re-login');
          _clearSession();
          _showLanding();
          return null;
        }
        // Persist the fresh token
        window.authToken = data.token;
        localStorage.setItem(TOKEN_KEY, data.token);
        if (data.user_id || data.email) {
          window.authUser = {
            user_id: data.user_id || window.authUser?.user_id || null,
            email:   data.email   || window.authUser?.email   || '',
            name:    data.name    || window.authUser?.name    || '',
          };
          localStorage.setItem(USER_KEY, JSON.stringify(window.authUser));
        }
        console.log('[AUTH] ✅ Token refreshed silently');
        return data.token;
      } catch (err) {
        console.warn('[AUTH] Token refresh network error:', err.message);
        return null;
      } finally {
        _refreshInProgress = null;
      }
    })();

    return _refreshInProgress;
  }

  // ── Low-level fetch helper ───────────────────────────────────────────────
  // On a 401 response the helper automatically attempts ONE silent token
  // refresh and retries the original request.  If the refresh also fails
  // (expired refresh token, revoked session, etc.) the user is sent back to
  // the landing/login screen — no silent infinite loops.
  async function _api(method, path, body, _isRetry) {
    const token = window.authToken;
    if (!token) return { ok: false, error: 'Not authenticated' };
    const opts = {
      method,
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${token}`,
      },
    };
    if (body !== undefined) opts.body = JSON.stringify(body);
    try {
      const res  = await fetch(`${BACKEND}${path}`, opts);
      const data = await res.json().catch(() => ({}));

      // ── Silent token refresh on 401 ──────────────────────────────────────
      if (res.status === 401 && !_isRetry) {
        console.warn(`[SYNC] ${method} ${path} → 401 — attempting silent token refresh`);
        const newToken = await _refreshToken();
        if (newToken) {
          // Retry once with the fresh token
          return _api(method, path, body, true);
        }
        // Refresh failed — session gone, user must log in again
        return { ok: false, status: 401, error: 'Session expired. Please sign in again.', data };
      }

      if (!res.ok) {
        console.warn(`[SYNC] ${method} ${path} → HTTP ${res.status}`, data);
        return { ok: false, status: res.status, error: data.detail || `HTTP ${res.status}`, data };
      }
      return { ok: true, data };
    } catch (err) {
      console.warn(`[SYNC] ${method} ${path} network error:`, err.message);
      if (_isNetworkFailure(err)) _logNetworkFailure(`${method} ${path}`);
      window._cloudOffline = true;
      return { ok: false, error: err.message };
    }
  }

  // ── Remove old auth gate markup (idempotent) ────────────────────────────
  function _removeOldGate() {
    if (document.getElementById('gz-auth-override-css')) return;
    const s = document.createElement('style');
    s.id = 'gz-auth-override-css';
    s.textContent = `
      #authGate, .ag-wrap, .ag-left, .ag-right, .ag-card,
      .ag-login-success-overlay, .ag-left-bg, .ag-anim-scene,
      [id="agLoginSuccessOverlay"] {
        display: none !important; visibility: hidden !important;
        pointer-events: none !important; opacity: 0 !important;
      }
      #authStatusBar { display: none; }
    `;
    document.head.appendChild(s);
    ['authGate', 'agLoginSuccessOverlay'].forEach(id => document.getElementById(id)?.remove());
  }

  // ── UI routing ──────────────────────────────────────────────────────────
  function _showLanding() {
    const dash = document.getElementById('gz-dashboard');
    if (dash) { dash.style.display = 'none'; dash.style.opacity = '0'; dash.classList.remove('active'); }
    const land = document.getElementById('gz-landing');
    if (land) { land.style.display = 'block'; land.style.opacity = '1'; }
    document.getElementById('authModal')?.classList?.remove('open');
    setTimeout(() => { if (typeof openAuthModal === 'function') openAuthModal(); }, 250);
  }

  function _enterDashboard() {
    if (typeof showDashboard === 'function') {
      showDashboard();
    } else {
      const land = document.getElementById('gz-landing');
      if (land) { land.style.opacity = '0'; setTimeout(() => land.style.display = 'none', 420); }
      const dash = document.getElementById('gz-dashboard');
      if (dash) { dash.style.display = 'block'; setTimeout(() => { dash.style.opacity = '1'; dash.classList.add('active'); }, 30); }
    }
    _updateStatusBar();
  }

  function _updateStatusBar() {
    const user = window.authUser;
    if (!user) return;

    // Show the topbar user pill (avatar + name + Sign Out button)
    const pill = document.getElementById('topbarUserPill');
    if (pill) pill.style.display = 'flex';

    // Set user name
    const nameEl = document.getElementById('authUserName');
    if (nameEl) nameEl.textContent = user.name || 'Student';

    // Set avatar initial (first letter of name or email)
    const avatarEl = document.getElementById('authUserAvatar');
    if (avatarEl) {
      avatarEl.textContent = (user.name || user.email || 'U').charAt(0).toUpperCase();
    }

    // Legacy: old authStatusBar (hidden in most layouts, no-op if absent)
    const bar = document.getElementById('authStatusBar');
    if (bar) bar.style.display = 'flex';

    const chipEl = document.getElementById('authUserIdChip');
    if (chipEl) {
      const uid = user.user_id;
      if (uid) {
        chipEl.textContent   = 'ID: ' + uid.slice(0, 8) + '…';
        chipEl.style.display = 'inline-block';
        chipEl.title         = 'Your User ID: ' + uid + ' — click to copy';
      } else {
        chipEl.style.display = 'none';
      }
    }
  }

  function _setSyncStatus(msg) {
    const el = document.getElementById('authSyncStatus');
    if (el) el.textContent = msg;
  }

  // ── Session helpers ─────────────────────────────────────────────────────
  function _storeSession(data) {
    window.authToken = data.token;
    window.authUser  = {
      user_id: data.user_id || null,
      email:   data.email   || '',
      name:    data.name    || (data.email || '').split('@')[0] || 'Student',
    };
    localStorage.setItem(TOKEN_KEY, window.authToken);
    localStorage.setItem(USER_KEY, JSON.stringify(window.authUser));
    // Clean up all legacy bridge flags (both naming variants)
    localStorage.removeItem('haezet_local_session');
    localStorage.removeItem('haezet_authenticated');
    localStorage.removeItem('genzet_local_session');
    localStorage.removeItem('genzet_authenticated');
  }

  function _clearSession() {
    window.authToken = null;
    window.authUser  = null;
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
    // Clean up all legacy bridge flags (both naming variants)
    localStorage.removeItem('haezet_authenticated');
    localStorage.removeItem('haezet_local_session');
    localStorage.removeItem('genzet_authenticated');
    localStorage.removeItem('genzet_local_session');
    // Clear onboarding fast-path so the server check re-runs on next login
    // (server will return completed:true for returning users — this just
    // ensures new logins on shared devices always verify server-side).
    localStorage.removeItem('haezet_onboarding_done');
  }

  // ════════════════════════════════════════════════════════════════════════
  // FULL DATA PULL  —  GET /sync/all
  // Called once after login. Falls back to legacy 3-call approach if needed.
  // ════════════════════════════════════════════════════════════════════════
  async function _syncAll() {
    const r = await _api('GET', '/sync/all');
    if (r.ok) {
      _hydrateItems(r.data.items    || []);
      _hydrateCourses(r.data.subjects || []);
      _hydrateVault(r.data.vault    || []);
      // Fetch public Supabase config for Realtime (fire-and-forget, no auth needed)
      _fetchPublicConfig();
      return true;
    }

    // ── 5xx errors: the backend endpoint exists but the DB call failed.
    // The legacy fallback endpoints (/animations, /courses, /vault) query the
    // same DB and will fail for the same reason — calling them just floods the
    // network tab with more 500s and leaves the user with a blank screen.
    // Instead, hydrate with empty arrays so the UI renders its empty-state.
    if (r.status && r.status >= 500) {
      console.warn(
        `[SYNC] /sync/all returned ${r.status} — skipping legacy fallback ` +
        `(same DB, same failure). Rendering empty state. Check Supabase keys & migrations.`
      );
      _hydrateItems([]);
      _hydrateCourses([]);
      _hydrateVault([]);
      _fetchPublicConfig();
      return false;
    }

    // 404 / network failure / pre-deploy: endpoint truly doesn't exist yet —
    // fall back to the legacy 3-call approach.
    console.warn('[SYNC] /sync/all unavailable — using legacy 3-call fallback');
    await _legacyLoadAll();
    _fetchPublicConfig();
    return false;
  }

  // ── Fetch public Supabase config (URL + anon key) for Realtime ─────────
  // Called after login. No auth token required. Idempotent.
  async function _fetchPublicConfig() {
    if (window.__supabaseUrl && window.__supabaseAnonKey) return; // already set
    try {
      const res  = await fetch(`${BACKEND}/sync/config`);
      if (!res.ok) return;
      const data = await res.json();
      if (data.supabase_url)      window.__supabaseUrl      = data.supabase_url;
      if (data.supabase_anon_key) window.__supabaseAnonKey  = data.supabase_anon_key;
      console.log('[CLOUD] Supabase config loaded for Realtime');
    } catch (_) {
      // Non-critical — Realtime simply won't connect if config unavailable
    }
  }

  // ── Hydrate in-memory state from cloud data ─────────────────────────────

  function _hydrateItems(items) {
    /**
     * items from /sync/all use the new schema:
     *   { id, item_type, title, prompt, explanation, html_code, playlist, created_at }
     * We map them to the shape index.html expects:
     *   { id, title, prompt, explanation, animation_code, playlist, created_at }
     *
     * NOTE: do NOT early-return when items is empty — we must still call the
     * render functions so the library shows its empty-state instead of a blank panel.
     */
    const mapped = items.map(item => ({
      id:             item.id,
      title:          item.title      || 'Untitled',
      prompt:         item.prompt     || '',
      explanation:    item.explanation || '',
      animation_code: item.html_code  || '',   // ← field rename
      playlist:       item.playlist   || 'General',
      created_at:     item.created_at,
      item_type:      item.item_type,           // extra — safe to keep
      unit_id:        item.unit_id    || null,  // File-Mode unit link
    }));

    // Cloud REPLACES local — no merge needed on login (even if empty)
    if (typeof animindLibrary !== 'undefined') {
      animindLibrary = mapped;
    }
    // Always refresh UI — shows empty-state for new users rather than blank
    if (typeof syncLibraryToFolders === 'function') syncLibraryToFolders();
    if (typeof showFolders          === 'function') showFolders();
    if (typeof updateActiveCount    === 'function') updateActiveCount();
    if (typeof renderLibrary        === 'function') renderLibrary();
    if (items.length && typeof notify === 'function') {
      notify(`✅ ${items.length} saved items loaded from cloud.`);
    }
  }

  function _hydrateCourses(subjects) {
    /**
     * subjects from /sync/all is already in engineeringCourses shape:
     * [{ id, name, description, cos: [{ id, coNum, description, topics: [...] }], units: [...] }]
     * Topics now include: type, pptUrl, pptStoragePath, fileName
     *
     * NOTE: do NOT early-return when subjects is empty — we must still call render
     * functions so File Mode shows its empty-state rather than a blank white panel.
     */
    // Normalise each topic so the frontend always has the fields it expects
    subjects.forEach(s => {
      // Normalise topics inside COs
      (s.cos || []).forEach(co =>
        (co.topics || []).forEach(t => {
          // Ensure backward-compat fields exist
          if (!t.type) {
            t.type = t.animCode && t.animCode.trim().startsWith('<!DOCTYPE')
              ? 'html_upload' : 'animation';
          }
          // pptUrl comes from backend as pptUrl already, but guard both names
          t.pptUrl        = t.pptUrl        || t.ppt_public_url   || null;
          t.pptStoragePath= t.pptStoragePath|| t.ppt_storage_path || null;
          t.fileName      = t.fileName      || t.ppt_file_name    || null;
          t.explanation   = t.explanation   || (t.type === 'ppt_upload' ? 'Uploaded PPT file' : '');
        })
      );
      // Ensure units array exists (new File Mode hierarchy)
      if (!Array.isArray(s.units)) s.units = [];
      s.units.forEach(u => {
        if (!Array.isArray(u.lesson_ids)) u.lesson_ids = [];
      });
    });
    if (typeof engineeringCourses !== 'undefined') {
      engineeringCourses = subjects;
    }
    // Hydrate in-memory units store for the new File Mode
    if (typeof window._fileMode !== 'undefined') {
      window._fileMode.hydrateUnits(subjects);
    }
    if (typeof renderSubjectsGrid  === 'function') renderSubjectsGrid();
    if (typeof syncLibraryToFolders === 'function') syncLibraryToFolders();
    if (typeof updateActiveCount    === 'function') updateActiveCount();
    if (typeof updateStorageBadge   === 'function') updateStorageBadge();
  }


  function _hydrateVault(entries) {
    // NOTE: do NOT early-return when entries is empty — vault must still render
    // its empty-state so users see the panel, not a blank white area.
    if (typeof vaultVideos !== 'undefined') {
      // Map new schema fields → legacy shape that vaultRenderGrid() expects
      vaultVideos = entries.map(e => ({
        id:       e.id,
        name:     e.name,
        fileName: e.file_name   || e.fileName || e.name,
        size:     e.file_size   || e.size     || 0,
        url:      e.public_url  || e.url      || '',
        created_at: e.created_at,
      }));
      if (typeof vaultRenderGrid === 'function') vaultRenderGrid();
    }
    // Remove legacy localStorage copy
    try { localStorage.removeItem('genzet_vault'); } catch (_) {}
  }


  // ════════════════════════════════════════════════════════════════════════
  // SAVE GENERATED ITEM  —  POST /sync/items
  // ════════════════════════════════════════════════════════════════════════
  async function _saveItem(item, itemType) {
    /**
     * item must have: title, prompt, explanation, animation_code, playlist
     * itemType: 'ai_creator' | 'book_mode' | 'question_anim' | 'topic_content'
     * Falls back to legacy /sync/animations if the new endpoint fails.
     */
    _setSyncStatus('Saving…');
    const body = {
      item_type:   itemType || 'ai_creator',
      title:       (item.title       || 'Untitled').trim(),
      prompt:      item.prompt       || '',
      explanation: item.explanation  || '',
      html_code:   item.animation_code || '',
      playlist:    item.playlist     || 'General',
      is_saved:    true,
      created_at:  item.created_at   || new Date().toISOString(),
    };
    // Book mode extras
    if (item.source_pdf_name) body.source_pdf_name = item.source_pdf_name;
    if (item.source_topic)    body.source_topic    = item.source_topic;
    if (item.source_subtopic) body.source_subtopic = item.source_subtopic;

    const r = await _api('POST', '/sync/items', body);
    if (r.ok) {
      if (r.data?.id) item._cloud_id = r.data.id;  // store UUID for later deletes
      _setSyncStatus('☁ Saved');
      setTimeout(() => _setSyncStatus(''), 3000);
      return r;
    }

    // New endpoint failed — fall back to legacy
    console.warn('[SYNC] /sync/items failed, falling back to legacy:', r.error);
    await _legacySaveAnimation(item);
    return r;
  }

  // ── Delete item (tries new endpoint first, falls back to legacy) ─────────
  async function _deleteItem(id) {
    // id may be a UUID (_cloud_id) or a legacy timestamp string
    const r = await _api('DELETE', `/sync/items/${encodeURIComponent(id)}`);
    if (!r.ok) {
      // Fall back to legacy /sync/animations/{id}
      await _legacyDeleteAnimation(id);
    }
    _setSyncStatus('');
  }


  // ════════════════════════════════════════════════════════════════════════
  // SUBJECT / CO / TOPIC  —  normalized row operations
  // ════════════════════════════════════════════════════════════════════════

  async function _createSubject(name, description) {
    const r = await _api('POST', '/sync/subjects', { name, description: description || '' });
    if (r.ok) {
      if (typeof notify === 'function') notify(`✅ Subject "${name}" created`);
      return r.data?.subject || null;
    }
    console.warn('[SYNC] createSubject failed:', r.error);
    return null;
  }

  async function _updateSubject(subjectId, patch) {
    const r = await _api('PUT', `/sync/subjects/${subjectId}`, patch);
    if (!r.ok) console.warn('[SYNC] updateSubject failed:', r.error);
    return r.ok;
  }

  async function _deleteSubject(subjectId) {
    const r = await _api('DELETE', `/sync/subjects/${subjectId}`);
    if (!r.ok) console.warn('[SYNC] deleteSubject failed:', r.error);
    return r.ok;
  }

  async function _createCO(subjectId, coNum, description) {
    const r = await _api('POST', '/sync/cos', {
      subject_id: subjectId,
      co_num:     coNum,
      description: description || '',
    });
    if (r.ok) return r.data?.co || null;
    console.warn('[SYNC] createCO failed:', r.error);
    return null;
  }

  async function _updateCO(coId, patch) {
    const r = await _api('PUT', `/sync/cos/${coId}`, patch);
    if (!r.ok) console.warn('[SYNC] updateCO failed:', r.error);
    return r.ok;
  }

  async function _deleteCO(coId) {
    const r = await _api('DELETE', `/sync/cos/${coId}`);
    if (!r.ok) console.warn('[SYNC] deleteCO failed:', r.error);
    return r.ok;
  }

  async function _createTopic(coId, subjectId, name, htmlCode, prompt) {
    const r = await _api('POST', '/sync/topics', {
      co_id:      coId,
      subject_id: subjectId,
      name,
      prompt:     prompt   || '',
      html_code:  htmlCode || '',
    });
    if (r.ok) return r.data?.topic || null;
    console.warn('[SYNC] createTopic failed:', r.error);
    return null;
  }

  async function _updateTopic(topicId, patch) {
    const r = await _api('PUT', `/sync/topics/${topicId}`, patch);
    if (!r.ok) console.warn('[SYNC] updateTopic failed:', r.error);
    return r.ok;
  }

  async function _deleteTopic(topicId) {
    const r = await _api('DELETE', `/sync/topics/${topicId}`);
    if (!r.ok) console.warn('[SYNC] deleteTopic failed:', r.error);
    return r.ok;
  }


  // ════════════════════════════════════════════════════════════════════════
  // PPT UPLOAD  —  POST /sync/ppt/upload  (multipart)
  // ════════════════════════════════════════════════════════════════════════

  /**
   * _uploadPPT(coId, subjectId, name, arrayBuffer, fileName)
   *
   * Sends the .pptx binary as multipart/form-data to POST /sync/ppt/upload.
   * The backend uploads it to Supabase Storage (ppt-files bucket) and
   * inserts a course_topics row with topic_type='ppt_upload'.
   *
   * Returns: { success, topic } where topic.id is the DB UUID,
   *          topic.pptUrl is the Supabase Storage public URL.
   * Returns null on failure.
   */
  async function _uploadPPT(coId, subjectId, name, arrayBuffer, fileName) {
    const token = window.authToken;
    if (!token) return null;

    const fd = new FormData();
    fd.append('file',       new Blob([arrayBuffer], {
      type: 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
    }), fileName || 'upload.pptx');
    fd.append('co_id',      coId);
    fd.append('subject_id', subjectId);
    fd.append('name',       name);

    try {
      const res  = await fetch(`${BACKEND}/sync/ppt/upload`, {
        method:  'POST',
        headers: { 'Authorization': `Bearer ${token}` },
        // Note: do NOT set Content-Type header — browser sets it
        //       automatically with the correct multipart boundary.
        body:    fd,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        console.warn('[PPT] Upload failed:', res.status, data);
        window._cloudOffline = (res.status === 0);
        return null;
      }
      console.log('[PPT] ✅ Uploaded to cloud:', data.topic?.id);
      return data;  // { success: true, topic: { id, name, type, pptUrl, ... } }
    } catch (err) {
      console.warn('[PPT] Network error during upload:', err.message);
      window._cloudOffline = true;
      return null;
    }
  }


  // ════════════════════════════════════════════════════════════════════════
  // VAULT  —  per-entry operations
  // ════════════════════════════════════════════════════════════════════════

  async function _addVaultEntry(entry) {
    /**
     * entry: { name, fileName, size, url?, storagePath?, mimeType? }
     * Falls back to legacy PUT /sync/vault if new endpoint fails.
     */
    const r = await _api('POST', '/sync/vault/entries', {
      name:         entry.name,
      file_name:    entry.fileName   || entry.file_name || entry.name,
      file_size:    entry.size       || entry.file_size || 0,
      storage_path: entry.storagePath || entry.storage_path || '',
      public_url:   entry.url        || entry.public_url    || '',
      mime_type:    entry.mimeType   || entry.mime_type     || 'video/mp4',
    });
    if (!r.ok) {
      console.warn('[SYNC] /sync/vault/entries failed, falling back to legacy:', r.error);
      await _legacySaveVault();
    }
    return r;
  }

  async function _deleteVaultEntry(entryId) {
    const r = await _api('DELETE', `/sync/vault/entries/${encodeURIComponent(entryId)}`);
    if (!r.ok) {
      console.warn('[SYNC] /sync/vault/entries DELETE failed, using legacy:', r.error);
      await _legacySaveVault();
    }
    return r;
  }


  // ════════════════════════════════════════════════════════════════════════
  // LEGACY FALLBACK FUNCTIONS
  // Kept exactly as in cloud_sync.js v2 so nothing breaks during migration.
  // ════════════════════════════════════════════════════════════════════════

  async function _legacyLoadAll() {
    await Promise.all([
      _legacyLoadLibrary(),
      _legacyLoadCourses(),
      _legacyLoadVault(),
    ]);
    if (typeof syncLibraryToFolders === 'function') syncLibraryToFolders();
    if (typeof renderSubjectsGrid   === 'function') renderSubjectsGrid();
    if (typeof showFolders          === 'function') showFolders();
    if (typeof updateActiveCount    === 'function') updateActiveCount();
    if (typeof vaultRenderGrid      === 'function') vaultRenderGrid();
  }

  async function _legacyLoadLibrary() {
    const r = await _api('GET', '/sync/animations');
    if (!r.ok) return;
    const cloud = r.data?.animations || [];
    if (typeof animindLibrary !== 'undefined') animindLibrary = cloud;
  }

  async function _legacySaveAnimation(anim) {
    if (!anim || !anim.id) return;
    _setSyncStatus('Syncing…');
    const r = await _api('POST', '/sync/animations', {
      id:             anim.id,
      title:          anim.title          || 'Untitled',
      prompt:         anim.prompt         || '',
      explanation:    anim.explanation    || '',
      animation_code: anim.animation_code || '',
      playlist:       anim.playlist       || 'General',
      created_at:     anim.created_at     || new Date().toISOString(),
    });
    _setSyncStatus(r.ok ? '☁ Synced' : '⚠ Failed');
    setTimeout(() => _setSyncStatus(''), 3000);
  }

  async function _legacyBatchSave(anims) {
    if (!anims || !anims.length) return;
    _setSyncStatus(`Uploading ${anims.length}…`);
    const r = await _api('POST', '/sync/animations/batch', { animations: anims });
    _setSyncStatus(r.ok ? `☁ ${r.data?.synced || 0} backed up` : '⚠ Failed');
    setTimeout(() => _setSyncStatus(''), 4000);
  }

  async function _legacyDeleteAnimation(id) {
    if (!id) return;
    await _api('DELETE', `/sync/animations/${encodeURIComponent(id)}`);
  }

  // Debounce timer for legacy courses push
  let _coursesTimer = null;
  async function _legacySaveCourses() {
    if (!window.authToken || !window.authUser?.user_id) return;
    clearTimeout(_coursesTimer);
    return new Promise(resolve => {
      _coursesTimer = setTimeout(async () => {
        const courses = (typeof engineeringCourses !== 'undefined' && engineeringCourses)
          ? engineeringCourses.filter(Boolean).map(s => ({
              ...s,
              cos: Array.isArray(s.cos)
                ? s.cos.map(co => ({ ...co, topics: Array.isArray(co.topics) ? [...co.topics] : [] }))
                : [],
              syllabus: s.syllabus ? { ...s.syllabus, raw: '' } : null,
            }))
          : [];
        const r = await _api('PUT', '/sync/courses', { courses });
        if (!r.ok) {
          console.warn('[SYNC] Legacy courses push failed:', r.error);
          if (typeof notify === 'function') notify('⚠️ Courses sync failed — check connection', 'error');
        }
        resolve();
      }, 1200);
    });
  }

  async function _legacyLoadCourses() {
    const r = await _api('GET', '/sync/courses');
    if (!r.ok) return;
    const cloud = Array.isArray(r.data?.courses) ? r.data.courses : [];
    if (!cloud.length) return;

    // Strip old pre-seeded default subject IDs
    const LEGACY_IDS = new Set(['ep', 'ec', 'em', 'ht', 'mc']);
    const filtered = cloud.filter(s => !LEGACY_IDS.has(s.id));
    if (!filtered.length) return;

    if (typeof engineeringCourses !== 'undefined') {
      // Backfill animCode for topics that were saved before the Bug 2 fix
      const animById = {};
      if (typeof animindLibrary !== 'undefined' && Array.isArray(animindLibrary)) {
        for (const a of animindLibrary) animById[a.id] = a;
      }
      filtered.forEach(s => {
        (s.cos || []).forEach(co => {
          (co.topics || []).forEach(t => {
            if (!t.animCode && animById[t.id]) {
              t.animCode = animById[t.id].animation_code || null;
            }
          });
        });
      });
      engineeringCourses = filtered;
    }
  }

  async function _legacySaveVault() {
    const entries = typeof vaultVideos !== 'undefined' ? vaultVideos : [];
    await _api('PUT', '/sync/vault', { entries });
    try { localStorage.removeItem('genzet_vault'); } catch (_) {}
  }

  async function _legacyLoadVault() {
    const r = await _api('GET', '/sync/vault');
    if (!r.ok) return;
    const entries = r.data?.entries || [];
    if (typeof vaultVideos !== 'undefined') vaultVideos = entries;
    try { localStorage.removeItem('genzet_vault'); } catch (_) {}
  }


  // ════════════════════════════════════════════════════════════════════════
  // GOOGLE OAUTH  —  full-page redirect flow  (like Claude.ai / Gemini)
  // ════════════════════════════════════════════════════════════════════════

  // ── Call-deduplication lock: prevents triple-click / re-mount firing ──
  var _googleLoginInProgress = false;

  /**
   * _googleLogin()
   *
   * ── ROOT CAUSE (seen in Network tab) ──────────────────────────────────
   * 1. NS_ERROR_UNKNOWN (×3 calls): The backend /auth/google endpoint returns
   *    JSON {url, verifier}, NOT a 302 redirect.  Pointing window.location.href
   *    at it navigated the browser to a JSON document — the browser couldn't
   *    render it as a page and threw NS_ERROR_UNKNOWN.  Additionally the button
   *    click handler was firing 3×, causing 3 duplicate navigation attempts.
   *
   * ── FIX ───────────────────────────────────────────────────────────────
   * a) Add a _googleLoginInProgress lock so only ONE call runs at a time.
   * b) fetch() the JSON properly with mode:'cors' to get {url, verifier}.
   * c) Store the verifier, THEN redirect to the real Google OAuth URL.
   * d) If fetch() is still CORS-blocked, fall back to direct navigation
   *    (which works if the backend later changes to a 302-redirect approach).
   *
   * Flow:
   *   1. fetch() BACKEND/auth/google → {url, verifier}
   *   2. localStorage.setItem('oauth_verifier', verifier)
   *   3. window.location.href = url  (Google OAuth consent page)
   *   4. Google → Supabase → oauth_callback.html?code=...
   *   5. oauth_callback.html exchanges code → JWT → redirects to '/'
   *   6. _authInit() reads JWT → enters dashboard
   */
  async function _googleLogin() {
    // ── Deduplication guard ────────────────────────────────────────────
    if (_googleLoginInProgress) {
      console.warn('[AUTH] _googleLogin() called while already in progress — ignoring duplicate call.');
      return;
    }
    _googleLoginInProgress = true;

    var origin = window.location.origin;
    if (!origin || origin === 'null') origin = 'https://haezet.com';
    var callbackUrl = origin + '/oauth_callback.html';
    var apiUrl = BACKEND + '/auth/google?redirect_to=' + encodeURIComponent(callbackUrl);

    // ── Show loading state on the Google button ────────────────────────
    var googleBtn = document.querySelector('[onclick*="GoogleLogin"], [onclick*="googleLogin"], #googleSignInBtn, .google-btn');
    var originalBtnText = googleBtn ? googleBtn.textContent : null;
    if (googleBtn) googleBtn.textContent = 'Connecting to Google…';

    try {
      // Step 1: fetch the OAuth URL as JSON (the backend returns {url, verifier})
      var controller = new AbortController();
      var tid = setTimeout(function () { controller.abort(); }, 12000);

      var res = await fetch(apiUrl, {
        method: 'GET',
        mode:   'cors',      // explicit CORS — browser sends Origin header
        signal: controller.signal,
        // No Authorization header on this public endpoint
        // No Content-Type — avoids an unnecessary preflight
      });
      clearTimeout(tid);

      if (!res.ok) throw new Error('HTTP ' + res.status);

      var data = await res.json();

      if (data.url) {
        // Step 2: store PKCE verifier if returned
        if (data.verifier) localStorage.setItem('oauth_verifier', data.verifier);
        // Step 3: redirect to the REAL Google OAuth URL (not the backend URL)
        console.log('[AUTH] ✅ Got OAuth URL — redirecting to Google consent page');
        window.location.href = data.url;
      } else {
        throw new Error('Backend returned no OAuth URL in response');
      }

    } catch (err) {
      _googleLoginInProgress = false; // release lock on error
      if (googleBtn && originalBtnText) googleBtn.textContent = originalBtnText;

      if (err.name === 'AbortError') {
        console.warn('[AUTH] Google OAuth fetch timed out — backend may be starting up (Railway cold-start).');
        // Fallback: navigate directly; works if backend later issues a 302
        console.warn('[AUTH] Falling back to direct navigation to:', apiUrl);
        window.location.href = apiUrl;
        return;
      }

      if (_isNetworkFailure(err)) {
        // TypeError = CORS blocked — the backend's CORS allow-list must include this origin
        console.error(
          '[AUTH] Google OAuth CORS error.\n' +
          'Backend must add "' + window.location.origin + '" to CORS allow_origins for GET /auth/google.\n' +
          'Falling back to direct navigation (works if backend issues a 302 redirect instead of JSON).'
        );
        // Fallback: direct page navigation bypasses CORS entirely
        window.location.href = apiUrl;
        return;
      }

      console.error('[AUTH] Google OAuth failed:', err.message);
      if (typeof window.amShowErr === 'function') {
        window.amShowErr('Google sign-in unavailable — please try email/password.');
      }
    }
  }




  // ════════════════════════════════════════════════════════════════════════
  // AUTH BOOTSTRAP
  // ════════════════════════════════════════════════════════════════════════

  // Promise that resolves when _authInit completes (token set or not).
  // Anything that needs the auth token awaits this instead of polling.
  let _authReadyResolve;
  window.authReadyPromise = new Promise(resolve => { _authReadyResolve = resolve; });

  async function _authInit() {
    _removeOldGate();

    // Clean up legacy bridge flags (both naming variants)
    const legacyFlag = localStorage.getItem('haezet_authenticated') || localStorage.getItem('genzet_authenticated');
    localStorage.removeItem('haezet_authenticated');
    localStorage.removeItem('genzet_authenticated');
    if (legacyFlag === 'true' && !localStorage.getItem(TOKEN_KEY)) {
      _showLanding(); return;
    }

    const storedToken = localStorage.getItem(TOKEN_KEY);
    if (!storedToken) { _showLanding(); _authReadyResolve(null); return; }

    _setSyncStatus('Verifying session…');
    try {
      const res = await fetch(`${BACKEND}/auth/verify`, {
        headers: { 'Authorization': `Bearer ${storedToken}` },
      });

      if (!res.ok) {
        _clearSession();
        _showLanding();
        _authReadyResolve(null);   // signal: token rejected
        return;
      }

      const profile    = await res.json();
      window.authToken = storedToken;
      window.authUser  = {
        user_id: profile.user_id,
        email:   profile.email,
        name:    profile.name,
      };
      localStorage.setItem(USER_KEY, JSON.stringify(window.authUser));

      // ── Onboarding gate ─────────────────────────────────────────────────
      // Check if this user has completed the school + role onboarding step.
      // Use a localStorage fast-path first to avoid a network round-trip for
      // returning users; fall back to a server check for new/unknown state.
      const onboardingDone = localStorage.getItem('haezet_onboarding_done') === 'true';
      if (!onboardingDone) {
        try {
          const obRes = await fetch(`${BACKEND}/onboarding/status`, {
            headers: { 'Authorization': `Bearer ${storedToken}` },
          });
          if (obRes.ok) {
            const obData = await obRes.json();
            if (!obData.completed) {
              window.location.href = '/onboarding.html';
              return;
            }
            // Mark done so future page loads skip this check
            localStorage.setItem('haezet_onboarding_done', 'true');
          } else {
            // If the server returns a 500 error (like missing tables), don't skip onboarding!
            window.location.href = '/onboarding.html';
            return;
          }
        } catch (_obErr) {
          // Network error (offline) — allow entry into dashboard; user can re-onboard later
          console.warn('[AUTH] Onboarding status check failed (network) — entering dashboard.');
        }
      }
      // ── End onboarding gate ─────────────────────────────────────────────

      _enterDashboard();
      _setSyncStatus('Loading your data…');
      await _syncAll();
      _setSyncStatus('');
      _authReadyResolve(storedToken);  // signal: auth complete, token is ready
      // Pre-fetch all subject APIs so Library Mode loads instantly
      _loadLessons().catch(() => {});
      _loadMathsTopics().catch(() => {});
      _loadSocialTopics().catch(() => {});

    } catch (err) {
      console.warn('[AUTH] Backend unreachable:', err.message);
      if (_isNetworkFailure(err)) _logNetworkFailure('GET /auth/verify');
      let cached = null;
      try { cached = JSON.parse(localStorage.getItem(USER_KEY) || 'null'); } catch (_) {}
      if (cached) {
        window.authToken = storedToken;
        window.authUser  = cached;
        _enterDashboard();
        _setSyncStatus('⚠ Offline — reconnect to load your data');
        _authReadyResolve(storedToken);  // signal auth done (offline path)
        // Still attempt to load lessons (works if backend is reachable)
        _loadLessons().catch(() => {});
        _loadMathsTopics().catch(() => {});
        _loadSocialTopics().catch(() => {});
      } else {
        _showLanding();
        _authReadyResolve(null);         // signal auth done (no session)
      }
    }
  }

  function _logout() {
    if (!confirm('Sign out of GenZet? Your library is safely synced to the cloud.')) return;
    _clearSession();
    if (typeof animindLibrary     !== 'undefined') animindLibrary     = [];
    if (typeof engineeringCourses !== 'undefined') engineeringCourses = [];
    if (typeof vaultVideos        !== 'undefined') vaultVideos        = [];
    if (typeof showFolders        === 'function') showFolders();
    if (typeof renderSubjectsGrid === 'function') renderSubjectsGrid();
    if (typeof vaultRenderGrid    === 'function') vaultRenderGrid();
    _showLanding();
  }

  // Called from login/register modal on success
  async function _onAuthSuccess(data) {
    _storeSession(data);
    // After login, always send through the onboarding flow.
    // onboarding.html will skip itself instantly for returning users
    // (localStorage fast-path + server-side /onboarding/status check).
    window.location.href = '/onboarding.html';
  }


  // ════════════════════════════════════════════════════════════════════════
  // PATCH showDashboard
  // ════════════════════════════════════════════════════════════════════════
  const _origShowDashboard = window.showDashboard;
  window.showDashboard = function () {
    if (typeof _origShowDashboard === 'function') _origShowDashboard();
    const t = localStorage.getItem(TOKEN_KEY);
    let u = null;
    try { u = JSON.parse(localStorage.getItem(USER_KEY) || 'null'); } catch (_) {}
    if (t && u) { window.authToken = t; window.authUser = u; }
    _updateStatusBar();
  };


  // ════════════════════════════════════════════════════════════════════════
  // PUBLIC API  —  same surface as v2, extended with new normalized ops
  // ════════════════════════════════════════════════════════════════════════

  // ── Auth ─────────────────────────────────────────────────────────────────
  window.authInit            = _authInit;
  window.authStoreSession    = _storeSession;
  window.authOnLoginSuccess  = _onAuthSuccess;   // store + sync in one call
  window.authClearSession    = _clearSession;
  window.authLogout          = _logout;
  window.authUpdateStatusBar = _updateStatusBar;
  window.authSetSyncStatus   = _setSyncStatus;
  window.authShowGate        = _showLanding;     // back-compat alias
  window.authHideGate        = _enterDashboard;  // back-compat alias
  window.authGoogleLogin     = _googleLogin;     // Google OAuth redirect flow



  // ── Item sync (new + legacy fallback) ────────────────────────────────────
  // syncPushToCloud(item, itemType?) — itemType defaults to 'ai_creator'
  window.syncPushToCloud     = (item, type) => _saveItem(item, type || 'ai_creator');
  window.syncPushAllToCloud  = (anims)      => _legacyBatchSave(anims || window.animindLibrary || []);
  window.syncPullFromCloud   = ()           => _syncAll();
  window.syncDeleteFromCloud = (id)         => _deleteItem(id);

  // ── Normalized course ops ─────────────────────────────────────────────────
  window.syncCreateSubject   = _createSubject;   // (name, description?) → subject|null
  window.syncUpdateSubject   = _updateSubject;   // (id, patch) → bool
  window.syncDeleteSubject   = _deleteSubject;   // (id) → bool
  window.syncCreateCO        = _createCO;        // (subjectId, coNum, desc?) → co|null
  window.syncUpdateCO        = _updateCO;        // (id, patch) → bool
  window.syncDeleteCO        = _deleteCO;        // (id) → bool
  window.syncCreateTopic     = _createTopic;     // (coId, subjectId, name, html?, prompt?) → topic|null
  window.syncUpdateTopic     = _updateTopic;     // (id, patch) → bool
  window.syncDeleteTopic     = _deleteTopic;     // (id) → bool
  window.syncUploadPPT       = _uploadPPT;       // (coId, subjectId, name, arrayBuffer, fileName) → {success, topic}|null

  // ── Legacy course sync (called by existing index.html saveECourses()) ─────
  window.syncCoursesToCloud       = _legacySaveCourses;
  window.syncPullCoursesFromCloud = _legacyLoadCourses;

  // ── Vault (new per-entry + legacy blob fallback) ──────────────────────────
  window.syncAddVaultEntry        = _addVaultEntry;    // (entry) → result
  window.syncDeleteVaultEntry     = _deleteVaultEntry; // (id) → result
  window.syncPushVaultToCloud     = _legacySaveVault;  // legacy: push full array
  window.syncPullVaultFromCloud   = _legacyLoadVault;  // legacy: pull full array

  // ── IDB stubs (safe no-ops) ───────────────────────────────────────────────
  window.initDB         = async () => null;
  window.saveLibraryIDB = async () => {};
  window.loadLibraryIDB = async () => { await _legacyLoadLibrary(); return window.animindLibrary || []; };
  window.saveECourses   = async () => { await _legacySaveCourses(); };
  window.loadECourses   = async () => { await _legacyLoadCourses(); return window.engineeringCourses || []; };

  // ── Old auth gate stubs ───────────────────────────────────────────────────
  window.authDoLogin    = () => console.warn('[AUTH] Use landing modal amHandleLogin()');
  window.authDoRegister = () => console.warn('[AUTH] Use landing modal amHandleSignup()');
  window.authSwitchTab  = () => {};
  window.agTogglePw     = () => {};
  window.authShowMsg    = () => {};
  window.authClearMsg   = () => {};


  // ════════════════════════════════════════════════════════════════════════
  // BOOTSTRAP
  // ════════════════════════════════════════════════════════════════════════
  // ── BUGFIX (paired with the index.html fix) ─────────────────────────────
  // This used to register its OWN 'DOMContentLoaded' listener that called
  // _authInit() via setTimeout(..., 80) — completely independent of the
  // 'DOMContentLoaded' listener in index.html that ALSO calls _authInit()
  // (via window.authInit). Browsers fire every listener for the same event
  // in registration order without waiting for an earlier async listener to
  // finish, so both listeners ran on every page load: /auth/verify and
  // /sync/all were each firing twice.
  //
  // cloud_sync_v3.js's own header comment says index.html is meant to own
  // bootstrapping ("window.* API surface IDENTICAL to v2 — zero changes
  // needed in index.html"), and index.html's listener already does
  // `const initFn = window.authInit || authInit; await initFn();` on
  // DOMContentLoaded. So the fix here is to stop self-triggering entirely —
  // this file only *exposes* _authInit as window.authInit; index.html is
  // solely responsible for calling it, exactly once.
  //
  // _removeOldGate() is still run immediately below since it's a cheap,
  // idempotent DOM/CSS cleanup that doesn't touch the network and doesn't
  // need to wait for DOMContentLoaded.
  _removeOldGate();

  // ════════════════════════════════════════════════════════════════════════
  // LESSONS API  —  Admin Content Management + Kahoot-Style Library
  // ════════════════════════════════════════════════════════════════════════
  // Exposes window.lessonsAPI with all lesson CRUD + Realtime subscription.
  // Syncs directly from Supabase public.lessons on every load.
  // Each lesson row has: title, subject, thumbnail_url, theory_url,
  // animation_url, realworld_images, content_type, class_name.
  // ════════════════════════════════════════════════════════════════════════

  let _lessonsCache = [];        // in-memory cache for quick re-renders
  let _realtimeWS   = null;      // Supabase Realtime WebSocket handle

  /**
   * Fetch ALL lessons from Supabase public.lessons directly using the
   * user's own JWT.  This satisfies the RLS policy
   * (auth.role() = 'authenticated') and does NOT depend on Railway.
   *
   * Strategy:
   *   1. Direct Supabase REST call  (user JWT + anon key as apikey header)
   *   2. Fallback: backend proxy   GET /api/sync/lessons  (uses service-role)
   *
   * Fields synced from public.lessons:
   *   id, title, subject, class_name, content_type,
   *   thumbnail_url, theory_url, animation_url,
   *   realworld_images (JSON array of URLs), created_at
   */
  async function _loadLessons() {
    // Wait for auth to complete before checking the token.
    // authReadyPromise resolves when _authInit finishes (regardless of how
    // long the Railway backend takes to respond).
    if (window.authReadyPromise) {
      await window.authReadyPromise;
    }
    const token = window.authToken;
    if (!token) {
      console.warn('[LESSONS] No auth token — user is not logged in.');
      return null;
    }

    let lessons = null;

    // ── Ensure Supabase config is loaded (URL + anon key) ────────────────
    // _fetchPublicConfig() is fire-and-forget in _syncAll — may not have
    // resolved yet by the time _loadLessons is called, so fetch inline.
    if (!window.__supabaseUrl || !window.__supabaseAnonKey) {
      await _fetchPublicConfig();
    }

    // ── Strategy 1: Direct Supabase REST (user JWT) ───────────────────────
    const sbUrl  = window.__supabaseUrl;
    const sbAnon = window.__supabaseAnonKey;
    if (sbUrl && sbAnon) {
      try {
        const res = await fetch(
          `${sbUrl}/rest/v1/lessons?select=*&order=created_at.asc`,
          {
            headers: {
              'apikey':        sbAnon,
              'Authorization': `Bearer ${token}`,
              'Accept':        'application/json',
            },
          }
        );
        if (res.ok) {
          const rows = await res.json();
          console.log('[LESSONS] Synced', rows.length, 'lesson(s) directly from Supabase.');
          lessons = rows;
        } else {
          const errText = await res.text().catch(() => '');
          console.warn('[LESSONS] Direct Supabase call failed:', res.status, errText);
        }
      } catch (err) {
        console.warn('[LESSONS] Direct Supabase fetch error:', err.message);
      }
    }

    // ── Strategy 2: Backend proxy fallback ───────────────────────────────
    if (!lessons) {
      console.log('[LESSONS] Falling back to backend proxy GET /sync/lessons …');
      const r = await _api('GET', '/sync/lessons');
      if (r.ok) {
        lessons = r.data.lessons || [];
        console.log('[LESSONS] Synced', lessons.length, 'lesson(s) via backend proxy.');
      } else {
        console.warn('[LESSONS] Backend proxy also failed:', r.error, '| HTTP status:', r.status);
        return null;
      }
    }

    // ── Normalize and cache ───────────────────────────────────────────────
    _lessonsCache = lessons.map(l => ({
      ...l,
      realworld_images: Array.isArray(l.realworld_images) ? l.realworld_images : [],
    }));

    // Refresh subject tile counts (Science / Maths / Social Science)
    if (typeof window.renderLibraryCategories === 'function') {
      window.renderLibraryCategories();
    }
    // Refresh lessons grid if it is currently visible
    if (typeof window.renderLessonsGrid === 'function') {
      window.renderLessonsGrid(_lessonsCache);
    }
    return _lessonsCache;
  }



  /**
   * Upload a single file (thumbnail / animation / theory) to Supabase Storage
   * via the backend proxy. Returns { public_url, storage_path }.
   * @param {File}   file      — File object from <input type="file">
   * @param {string} fileType  — "thumbnail" | "animation" | "theory"
   */
  async function _uploadLessonFile(file, fileType) {
    const token = window.authToken;
    if (!token) return { ok: false, error: 'Not authenticated' };

    const formData = new FormData();
    formData.append('file', file);
    formData.append('file_type', fileType);

    try {
      const res  = await fetch(`${BACKEND}/sync/lessons/upload-file`, {
        method:  'POST',
        headers: { 'Authorization': `Bearer ${token}` },
        body:    formData,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        return { ok: false, error: data.detail || `HTTP ${res.status}` };
      }
      return { ok: true, ...data };
    } catch (err) {
      if (_isNetworkFailure(err)) _logNetworkFailure('POST /sync/lessons/upload-file');
      return { ok: false, error: err.message };
    }
  }

  /**
   * Create a lesson row in the DB (admin only).
   * @param {object} lesson — { title, class_name, subject, thumbnail_url, theory_url, animation_url, realworld_images[] }
   */
  async function _createLesson(lesson) {
    return _api('POST', '/sync/lessons', lesson);
  }

  /**
   * Delete a lesson (admin only).
   * @param {string} lessonId — UUID
   */
  async function _deleteLesson(lessonId) {
    return _api('DELETE', `/sync/lessons/${encodeURIComponent(lessonId)}`);
  }

  // ── Supabase Realtime subscription ──────────────────────────────────────
  // Uses the Supabase Realtime WebSocket protocol to receive INSERT/UPDATE/DELETE
  // events on the `lessons` table and instantly update all active sessions.
  // No extra library needed — plain WebSocket.
  function _subscribeRealtime() {
    const supabaseUrl = (() => {
      // Derive from BACKEND or use env-encoded URL from backend /health
      // We embed the URL via the window.__supabaseUrl hint set during auth
      return window.__supabaseUrl || null;
    })();

    if (!supabaseUrl || _realtimeWS) return;  // already connected or no URL

    const anonKey = window.__supabaseAnonKey || null;
    if (!anonKey) return;

    try {
      const wsUrl = supabaseUrl.replace('https://', 'wss://').replace('http://', 'ws://');
      const ws = new WebSocket(
        `${wsUrl}/realtime/v1/websocket?apikey=${anonKey}&vsn=1.0.0`
      );
      _realtimeWS = ws;

      ws.onopen = () => {
        // Join the lessons channel
        ws.send(JSON.stringify({
          topic:   'realtime:public:lessons',
          event:   'phx_join',
          payload: { user_token: window.authToken || anonKey },
          ref:     '1',
        }));
        console.log('[LESSONS] Realtime subscribed to lessons table');
      };

      ws.onmessage = (evt) => {
        try {
          const msg = JSON.parse(evt.data);
          const { event, payload } = msg;

          if (event === 'INSERT' || event === 'postgres_changes') {
            // New lesson added to Supabase — add to in-memory cache and re-render
            const newLesson = payload?.data?.record || payload?.record;
            if (newLesson) {
              const exists = _lessonsCache.some(l => l.id === newLesson.id);
              if (!exists) {
                _lessonsCache.push(newLesson);
                if (typeof window.renderLessonsGrid === 'function') {
                  window.renderLessonsGrid(_lessonsCache);
                }
                if (typeof window.renderLibraryCategories === 'function') {
                  window.renderLibraryCategories();
                }
                if (typeof notify === 'function') {
                  notify('📚 New lesson added to the library!', 'success');
                }
              }
            }
          } else if (event === 'DELETE') {
            const oldId = payload?.data?.old_record?.id || payload?.old_record?.id;
            if (oldId) {
              _lessonsCache = _lessonsCache.filter(l => l.id !== oldId);
              if (typeof window.renderLessonsGrid === 'function') {
                window.renderLessonsGrid(_lessonsCache);
              }
              if (typeof window.renderLibraryCategories === 'function') {
                window.renderLibraryCategories();
              }
            }
          }
        } catch (_) {}
      };

      ws.onerror = (e) => console.warn('[LESSONS] Realtime WS error:', e);
      ws.onclose = () => {
        _realtimeWS = null;
        console.log('[LESSONS] Realtime WS closed');
      };
    } catch (err) {
      console.warn('[LESSONS] Realtime setup failed:', err.message);
    }
  }

  // Expose the public API on window
  window.lessonsAPI = {
    load:              _loadLessons,
    uploadFile:        _uploadLessonFile,
    createLesson:      _createLesson,
    deleteLesson:      _deleteLesson,
    subscribeRealtime: _subscribeRealtime,
    getCache:          () => _lessonsCache,
    // Reset in-memory cache and force a fresh sync from Supabase
    clearCache: function () {
      _lessonsCache = [];
      console.log('[LESSONS] In-memory cache cleared — next load will re-sync from Supabase.');
    },
  };

  // ── FILE MODE — Subject Units & Unit Lessons sync helpers ────────────────
  // These are called by the new File Mode JS in index.html.

  /**
   * Create a Unit or Lesson container inside a Subject Folder.
   * @param {string} subjectId
   * @param {string} unitType  'unit' | 'lesson'
   * @param {number} unitNumber  e.g. 1, 2, 3
   * @returns {Promise<object|null>}  the created unit row, or null on failure
   */
  window.syncCreateUnit = async function syncCreateUnit(subjectId, unitType, unitNumber) {
    const name = (unitType === 'lesson' ? 'Lesson' : 'Unit') + ' - ' + unitNumber;
    const res = await _api('POST', '/sync/units', {
      subject_id:  subjectId,
      unit_type:   unitType,
      unit_number: unitNumber,
      name,
      sort_order:  unitNumber - 1,
    });
    if (res.ok && res.data && res.data.unit) {
      console.log('[SYNC] ✅ Unit created:', res.data.unit);
      return res.data.unit;
    }
    console.warn('[SYNC] syncCreateUnit failed:', res.error);
    return null;
  };

  /**
   * Delete a Unit/Lesson container (cascades unit_lessons).
   * @param {string} unitId
   * @returns {Promise<boolean>}
   */
  window.syncDeleteUnit = async function syncDeleteUnit(unitId) {
    const res = await _api('DELETE', '/sync/units/' + encodeURIComponent(unitId));
    if (res.ok) {
      console.log('[SYNC] 🗑 Unit deleted:', unitId);
      return true;
    }
    console.warn('[SYNC] syncDeleteUnit failed:', res.error);
    return false;
  };

  /**
   * Save (replace) all lesson IDs linked to a unit.
   * @param {string} unitId
   * @param {string[]} lessonIds
   * @returns {Promise<boolean>}
   */
  window.syncSaveUnitLessons = async function syncSaveUnitLessons(unitId, lessonIds) {
    const res = await _api('POST', '/sync/unit-lessons', { unit_id: unitId, lesson_ids: lessonIds });
    if (res.ok) {
      console.log('[SYNC] ✅ Unit lessons saved: unit=' + unitId + ' count=' + lessonIds.length);
      return true;
    }
    console.warn('[SYNC] syncSaveUnitLessons failed:', res.error);
    return false;
  };

  /**
   * Get all lesson IDs saved to a unit.
   * @param {string} unitId
   * @returns {Promise<string[]>}
   */
  window.syncGetUnitLessons = async function syncGetUnitLessons(unitId) {
    const res = await _api('GET', '/sync/unit-lessons/' + encodeURIComponent(unitId));
    if (res.ok && res.data) return res.data.lesson_ids || [];
    console.warn('[SYNC] syncGetUnitLessons failed:', res.error);
    return [];
  };

  console.log('[CLOUD] cloud_sync_v3.js loaded — window.authInit is ready; index.html triggers it on DOMContentLoaded');

  // ════════════════════════════════════════════════════════════════════════
  // MATHS TOPICS API  —  Library Mode: Mathematics subject
  // Exposes window.mathsTopicsAPI  (same interface as lessonsAPI)
  // ════════════════════════════════════════════════════════════════════════

  let _mathsTopicsCache = [];

  async function _loadMathsTopics() {
    const r = await _api('GET', '/sync/maths-topics');
    if (r.ok) {
      _mathsTopicsCache = r.data.topics || [];
      console.log('[MATHS_TOPICS] Loaded', _mathsTopicsCache.length, 'topic(s).');
      // Refresh the library category tiles so the Maths count updates
      if (typeof window.renderLibraryCategories === 'function') {
        window.renderLibraryCategories();
      }
      if (typeof window.renderMathsTopicsGrid === 'function') {
        window.renderMathsTopicsGrid(_mathsTopicsCache);
      }
      return _mathsTopicsCache;
    }
    console.warn('[MATHS_TOPICS] Failed to load:', r.error);
    return null;
  }

  async function _uploadMathsTopicFile(file, fileType) {
    const token = window.authToken;
    if (!token) return { ok: false, error: 'Not authenticated' };
    const formData = new FormData();
    formData.append('file', file);
    formData.append('file_type', fileType);
    try {
      const res  = await fetch(`${BACKEND}/sync/maths-topics/upload-file`, {
        method:  'POST',
        headers: { 'Authorization': `Bearer ${token}` },
        body:    formData,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) return { ok: false, error: data.detail || `HTTP ${res.status}` };
      return { ok: true, ...data };
    } catch (err) {
      return { ok: false, error: err.message };
    }
  }

  async function _createMathsTopic(topic) {
    return _api('POST', '/sync/maths-topics', topic);
  }

  async function _deleteMathsTopic(topicId) {
    return _api('DELETE', `/sync/maths-topics/${encodeURIComponent(topicId)}`);
  }

  window.mathsTopicsAPI = {
    load:        _loadMathsTopics,
    uploadFile:  _uploadMathsTopicFile,
    createTopic: _createMathsTopic,
    deleteTopic: _deleteMathsTopic,
    getCache:    () => _mathsTopicsCache,
  };

  // ════════════════════════════════════════════════════════════════════════
  // SOCIAL TOPICS API  —  Library Mode: Social Science subject
  // Exposes window.socialTopicsAPI  (same interface as lessonsAPI)
  // ════════════════════════════════════════════════════════════════════════

  let _socialTopicsCache = [];

  async function _loadSocialTopics() {
    const r = await _api('GET', '/sync/social-topics');
    if (r.ok) {
      _socialTopicsCache = r.data.topics || [];
      console.log('[SOCIAL_TOPICS] Loaded', _socialTopicsCache.length, 'topic(s).');
      // Refresh the library category tiles so the Social Science count updates
      if (typeof window.renderLibraryCategories === 'function') {
        window.renderLibraryCategories();
      }
      if (typeof window.renderSocialTopicsGrid === 'function') {
        window.renderSocialTopicsGrid(_socialTopicsCache);
      }
      return _socialTopicsCache;
    }
    console.warn('[SOCIAL_TOPICS] Failed to load:', r.error);
    return null;
  }

  async function _uploadSocialTopicFile(file, fileType) {
    const token = window.authToken;
    if (!token) return { ok: false, error: 'Not authenticated' };
    const formData = new FormData();
    formData.append('file', file);
    formData.append('file_type', fileType);
    try {
      const res  = await fetch(`${BACKEND}/sync/social-topics/upload-file`, {
        method:  'POST',
        headers: { 'Authorization': `Bearer ${token}` },
        body:    formData,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) return { ok: false, error: data.detail || `HTTP ${res.status}` };
      return { ok: true, ...data };
    } catch (err) {
      return { ok: false, error: err.message };
    }
  }

  async function _createSocialTopic(topic) {
    return _api('POST', '/sync/social-topics', topic);
  }

  async function _deleteSocialTopic(topicId) {
    return _api('DELETE', `/sync/social-topics/${encodeURIComponent(topicId)}`);
  }

  window.socialTopicsAPI = {
    load:        _loadSocialTopics,
    uploadFile:  _uploadSocialTopicFile,
    createTopic: _createSocialTopic,
    deleteTopic: _deleteSocialTopic,
    getCache:    () => _socialTopicsCache,
  };

  // ════════════════════════════════════════════════════════════════════════
  // ASSESSMENT API  —  Assessment Mode: Science / Maths / Social Science
  // Exposes window.assessmentAPI
  // ════════════════════════════════════════════════════════════════════════

  // In-memory cache: { science: [], maths: [], social: [] }
  const _assessmentCache = { science: [], maths: [], social: [] };

  async function _loadAssessments(subject) {
    const r = await _api('GET', `/sync/assessment/${encodeURIComponent(subject)}`);
    if (r.ok) {
      _assessmentCache[subject] = r.data.assessments || [];
      console.log(`[ASSESSMENT] Loaded ${_assessmentCache[subject].length} row(s) for ${subject}.`);
      return _assessmentCache[subject];
    }
    console.warn(`[ASSESSMENT] Failed to load ${subject}:`, r.error);
    return null;
  }

  async function _saveAssessment(subject, payload) {
    return _api('POST', `/sync/assessment/${encodeURIComponent(subject)}`, payload);
  }

  /**
   * Upload an assessment HTML file to the subject-specific Supabase bucket.
   * Returns { ok, public_url, storage_path, bucket } on success.
   * @param {string} subject  — 'science' | 'maths' | 'social'
   * @param {File}   file     — HTML File object
   */
  async function _uploadAssessmentFile(subject, file) {
    const token = window.authToken;
    if (!token) return { ok: false, error: 'Not authenticated' };
    const formData = new FormData();
    formData.append('file', file);
    try {
      const res  = await fetch(`${BACKEND}/sync/assessment/${encodeURIComponent(subject)}/upload-file`, {
        method:  'POST',
        headers: { 'Authorization': `Bearer ${token}` },
        body:    formData,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) return { ok: false, error: data.detail || `HTTP ${res.status}` };
      return { ok: true, ...data };
    } catch (err) {
      return { ok: false, error: err.message };
    }
  }

  window.assessmentAPI = {
    load:       _loadAssessments,
    save:       _saveAssessment,
    uploadFile: _uploadAssessmentFile,
    getCache:   (subject) => _assessmentCache[subject] || [],
  };

})();
