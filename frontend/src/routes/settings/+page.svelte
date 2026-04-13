<script lang="ts">
  import { onMount } from 'svelte';
  import { getAuthStatus, login, setManualToken, syncBets, importCsv } from '$lib/api';
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

  // Manual token
  let manualToken = '';
  let tokenLoading = false;
  let tokenMsg = '';
  let tokenErr = '';

  // Sync
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
      loginMsg = `Authenticated — token valid for ${Math.floor(res.expires_in / 60)} min`;
      authStatus = { authenticated: true, expired: false };
      loginPassword = '';
    } catch (e) {
      loginErr = e instanceof Error ? e.message : String(e);
    } finally {
      loginLoading = false;
    }
  }

  async function handleManualToken() {
    if (!manualToken.trim()) return;
    tokenLoading = true;
    tokenMsg = '';
    tokenErr = '';
    try {
      const res = await setManualToken(manualToken.trim());
      tokenMsg = `Token accepted — valid for ${Math.floor(res.expires_in / 60)} min`;
      authStatus = { authenticated: true, expired: false };
      manualToken = '';
    } catch (e) {
      tokenErr = e instanceof Error ? e.message : String(e);
    } finally {
      tokenLoading = false;
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

  <!-- Auth status banner -->
  <div class="card flex items-center gap-3">
    {#if authLoading}
      <div class="w-2.5 h-2.5 rounded-full bg-slate-600 animate-pulse"></div>
      <span class="text-sm text-slate-400">Checking auth status…</span>
    {:else if authStatus.authenticated && !authStatus.expired}
      <div class="w-2.5 h-2.5 rounded-full bg-emerald-400"></div>
      <span class="text-sm text-emerald-300 font-medium">Authenticated with FanDuel</span>
    {:else if authStatus.authenticated && authStatus.expired}
      <div class="w-2.5 h-2.5 rounded-full bg-amber-400"></div>
      <span class="text-sm text-amber-300 font-medium">Token expired — re-authenticate</span>
    {:else}
      <div class="w-2.5 h-2.5 rounded-full bg-red-400"></div>
      <span class="text-sm text-red-300 font-medium">Not authenticated</span>
    {/if}
  </div>

  <!-- FanDuel login -->
  <div class="card space-y-4">
    <h2 class="text-sm font-semibold text-slate-200">FanDuel Login</h2>
    <p class="text-xs text-slate-500">
      Credentials are sent directly to FanDuel's session API and not stored.
      Use a manually-captured token if login fails (FanDuel uses Cloudflare).
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

    <div class="flex items-center gap-3">
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
      {#if loginMsg}<span class="text-sm text-emerald-400">{loginMsg}</span>{/if}
      {#if loginErr}<span class="text-sm text-red-400">{loginErr}</span>{/if}
    </div>
  </div>

  <!-- Manual token -->
  <div class="card space-y-4">
    <h2 class="text-sm font-semibold text-slate-200">Manual Token</h2>
    <p class="text-xs text-slate-500">
      Open DevTools → Network → any <code class="bg-surface-600 px-1 rounded">api.fanduel.com</code> request →
      Request Headers → copy the <code class="bg-surface-600 px-1 rounded">x-authentication</code> value.
    </p>

    <div>
      <label class="label" for="token">JWT Token</label>
      <textarea
        id="token"
        class="input font-mono text-xs resize-none h-20"
        placeholder="eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9..."
        bind:value={manualToken}
        disabled={tokenLoading}
      ></textarea>
    </div>

    <div class="flex items-center gap-3">
      <button
        class="btn-primary"
        on:click={handleManualToken}
        disabled={tokenLoading || !manualToken.trim()}
      >
        {tokenLoading ? 'Saving…' : 'Set Token'}
      </button>
      {#if tokenMsg}<span class="text-sm text-emerald-400">{tokenMsg}</span>{/if}
      {#if tokenErr}<span class="text-sm text-red-400">{tokenErr}</span>{/if}
    </div>
  </div>

  <!-- Sync -->
  <div class="card space-y-4">
    <h2 class="text-sm font-semibold text-slate-200">Sync from FanDuel</h2>
    <p class="text-xs text-slate-500">
      Pulls all settled bets from FanDuel's API and upserts them into the local database.
      Requires an active auth token above.
    </p>

    <div class="flex items-center gap-3">
      <button
        class="btn-primary"
        on:click={handleSync}
        disabled={syncLoading || !authStatus.authenticated}
      >
        {#if syncLoading}
          <svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
          </svg>
          Syncing…
        {:else}
          <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182m0-4.991v4.99"/>
          </svg>
          Sync Bets
        {/if}
      </button>
      {#if syncMsg}<span class="text-sm text-emerald-400">{syncMsg}</span>{/if}
      {#if syncErr}<span class="text-sm text-red-400">{syncErr}</span>{/if}
    </div>
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
