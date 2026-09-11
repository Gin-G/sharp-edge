<script lang="ts">
  import { onMount } from 'svelte';
  import { getAuthStatus, login, submitMfaCode, syncBets, importCsv } from '$lib/api';
  import type { AuthStatus } from '$lib/types';

  // --- Auth state ---
  let authStatus: AuthStatus = { authenticated: false };
  let authLoading = true;

  // Login form
  let loginEmail = '';
  let loginPassword = '';
  let loginLoading = false;
  let loginMsg = '';
  let loginErr = '';

  // Sync — kicked off automatically once a login lands, and available on its
  // own for a session that is still good from an earlier visit.
  let syncLoading = false;
  let syncMsg = '';
  let syncErr = '';

  // CSV import
  let csvPath = '';
  let importLoading = false;
  let importMsg = '';
  let importErr = '';

  onMount(async () => {
    try {
      authStatus = await getAuthStatus();
    } catch {
      /* backend may not be running */
    } finally {
      authLoading = false;
    }
  });

  async function handleLogin() {
    if (!loginEmail || !loginPassword) return;
    loginLoading = true;
    loginMsg = '';
    loginErr = '';
    try {
      const res = await login(loginEmail, loginPassword);
      if (res.status === 'mfa_required') {
        mfaRequired = true;
        loginMsg = res.message ?? 'FanDuel emailed a verification code — enter it below.';
      } else {
        loginMsg = sessionSummary(res);
        authStatus = { authenticated: true, expired: false };
        loginPassword = '';
        handleSync();
      }
    } catch (e) {
      loginErr = e instanceof Error ? e.message : String(e);
    } finally {
      loginLoading = false;
    }
  }

  /** Describe a new session without overstating what we know.
   *
   * FanDuel doesn't always put an exp claim on the session token; when it
   * doesn't, the backend assumes an hour, so presenting that as fact would
   * be inventing a number. And because the password is never persisted, a
   * session with no refresh token dies on the next pod restart — worth
   * saying up front rather than discovering at the next sync. */
  function sessionSummary(
    res: { expires_in?: number; expiry_assumed?: boolean; can_refresh?: boolean },
    prefix = 'Authenticated',
  ): string {
    const mins = Math.floor((res.expires_in ?? 0) / 60);
    const life = res.expiry_assumed
      ? `assuming ~${mins} min (FanDuel sent no expiry)`
      : `valid for ${mins} min`;
    const renew = res.can_refresh
      ? 'renews itself in the background'
      : 'no refresh token — you will need to log in again after a restart';
    return `${prefix} — ${life}; ${renew}.`;
  }

  let mfaRequired = false;
  let mfaCode = '';
  let mfaLoading = false;

  async function handleMfa() {
    if (!mfaCode.trim()) return;
    mfaLoading = true;
    loginErr = '';
    try {
      const res = await submitMfaCode(mfaCode.trim());
      loginMsg = sessionSummary(res);
      authStatus = { authenticated: true, expired: false };
      mfaRequired = false;
      mfaCode = '';
      loginPassword = '';
      handleSync();
    } catch (e) {
      loginErr = e instanceof Error ? e.message : String(e);
    } finally {
      mfaLoading = false;
    }
  }

  async function handleSync() {
    syncLoading = true;
    syncMsg = '';
    syncErr = '';
    try {
      const res = await syncBets();
      syncMsg = `Synced ${res.bets_synced} bets from FanDuel`;
    } catch (e) {
      syncErr = e instanceof Error ? e.message : String(e);
    } finally {
      syncLoading = false;
    }
  }

  async function handleImport() {
    if (!csvPath.trim()) return;
    importLoading = true;
    importMsg = '';
    importErr = '';
    try {
      const res = await importCsv(csvPath.trim());
      importMsg = `Imported ${res.bets_imported} bets from CSV`;
    } catch (e) {
      importErr = e instanceof Error ? e.message : String(e);
    } finally {
      importLoading = false;
    }
  }
</script>

<svelte:head><title>Settings — Sharp Edge</title></svelte:head>

<div class="space-y-6 max-w-2xl">
  <div>
    <h1 class="text-xl font-bold text-white">Settings</h1>
    <p class="text-sm text-slate-400 mt-0.5">FanDuel authentication and data sync</p>
  </div>

  <!-- FanDuel login -->
  <div class="card space-y-4">
    <div class="flex items-center justify-between gap-3">
      <h2 class="text-sm font-semibold text-slate-200">FanDuel Login</h2>
      {#if authLoading}
        <span class="inline-flex items-center gap-1.5 text-xs text-slate-400">
          <span class="w-2 h-2 rounded-full bg-slate-600 animate-pulse"></span>
          Checking…
        </span>
      {:else if authStatus.authenticated && !authStatus.expired}
        <span class="inline-flex items-center gap-1.5 text-xs text-emerald-300 font-medium">
          <span class="w-2 h-2 rounded-full bg-emerald-400"></span>
          Authenticated
        </span>
      {:else if authStatus.authenticated && authStatus.expired}
        <span class="inline-flex items-center gap-1.5 text-xs text-amber-300 font-medium">
          <span class="w-2 h-2 rounded-full bg-amber-400"></span>
          Token expired — log in again
        </span>
      {:else}
        <span class="inline-flex items-center gap-1.5 text-xs text-red-300 font-medium">
          <span class="w-2 h-2 rounded-full bg-red-400"></span>
          Not authenticated
        </span>
      {/if}
    </div>

    <p class="text-xs text-slate-500">
      Credentials are sent directly to FanDuel's session API and not stored.
      A successful login pulls your settled bets straight away.
    </p>

    <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
      <div>
        <label class="label" for="email">Email</label>
        <input
          id="email"
          type="email"
          class="input"
          placeholder="you@example.com"
          bind:value={loginEmail}
          disabled={loginLoading}
        />
      </div>
      <div>
        <label class="label" for="password">Password</label>
        <input
          id="password"
          type="password"
          class="input"
          placeholder="••••••••"
          bind:value={loginPassword}
          disabled={loginLoading}
        />
      </div>
    </div>

    <div class="flex items-center gap-3 flex-wrap">
      <button class="btn-primary" on:click={handleLogin} disabled={loginLoading || !loginEmail || !loginPassword}>
        {#if loginLoading}
          <svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
          </svg>
          Logging in…
        {:else}
          Login
        {/if}
      </button>
      {#if authStatus.authenticated}
        <button class="btn-ghost text-sm" on:click={handleSync} disabled={syncLoading}>
          {syncLoading ? 'Syncing…' : 'Sync now'}
        </button>
      {/if}
      {#if loginMsg}<span class="text-sm text-emerald-400">{loginMsg}</span>{/if}
      {#if loginErr}<span class="text-sm text-red-400">{loginErr}</span>{/if}
    </div>

    {#if mfaRequired}
      <div class="pt-2 border-t border-border space-y-3">
        <p class="text-xs text-slate-500">
          FanDuel sent a verification code to your account email (new device).
          Once verified, future logins skip this step.
        </p>
        <div class="flex items-center gap-3">
          <input
            class="input font-mono w-40"
            placeholder="123456"
            bind:value={mfaCode}
            disabled={mfaLoading}
          />
          <button
            class="btn-primary"
            on:click={handleMfa}
            disabled={mfaLoading || !mfaCode.trim()}
          >
            {mfaLoading ? 'Verifying…' : 'Verify'}
          </button>
        </div>
      </div>
    {/if}

    {#if syncLoading || syncMsg || syncErr}
      <div class="flex items-center gap-2 pt-2 border-t border-border text-sm">
        {#if syncLoading}
          <svg class="w-4 h-4 animate-spin text-slate-400" fill="none" viewBox="0 0 24 24">
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
          </svg>
          <span class="text-slate-400">Syncing bets from FanDuel…</span>
        {:else if syncMsg}
          <span class="text-emerald-400">{syncMsg}</span>
        {:else}
          <span class="text-red-400">Sync failed — {syncErr}</span>
        {/if}
      </div>
    {/if}
  </div>

  <!-- CSV Import -->
  <div class="card space-y-4">
    <h2 class="text-sm font-semibold text-slate-200">Import from Pikkit CSV</h2>
    <p class="text-xs text-slate-500">
      Absolute path to a Pikkit CSV export on the server running the backend.
    </p>

    <div>
      <label class="label" for="csv-path">File Path</label>
      <input
        id="csv-path"
        type="text"
        class="input font-mono text-xs"
        placeholder="/home/user/Downloads/pikkit-export.csv"
        bind:value={csvPath}
        disabled={importLoading}
      />
    </div>

    <div class="flex items-center gap-3">
      <button
        class="btn-primary"
        on:click={handleImport}
        disabled={importLoading || !csvPath.trim()}
      >
        {importLoading ? 'Importing…' : 'Import CSV'}
      </button>
      {#if importMsg}<span class="text-sm text-emerald-400">{importMsg}</span>{/if}
      {#if importErr}<span class="text-sm text-red-400">{importErr}</span>{/if}
    </div>
  </div>
</div>
