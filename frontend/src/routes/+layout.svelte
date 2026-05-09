<script lang="ts">
  import '../app.css';
  import { page } from '$app/stores';
  import Sidebar from '$lib/components/Sidebar.svelte';

  let sidebarOpen = false;

  // Close drawer on route change (mobile only — desktop keeps sidebar visible).
  $: if ($page) sidebarOpen = false;
</script>

<div class="flex h-screen overflow-hidden">
  <!-- Mobile backdrop -->
  {#if sidebarOpen}
    <button
      class="fixed inset-0 bg-black/50 z-30 md:hidden"
      aria-label="Close menu"
      on:click={() => (sidebarOpen = false)}
    ></button>
  {/if}

  <!-- Sidebar: fixed off-canvas on mobile, in-flow on md+ -->
  <div
    class="fixed inset-y-0 left-0 z-40 transform transition-transform duration-200
           md:static md:transform-none
           {sidebarOpen ? 'translate-x-0' : '-translate-x-full md:translate-x-0'}"
  >
    <Sidebar onNavigate={() => (sidebarOpen = false)} />
  </div>

  <main class="flex-1 overflow-y-auto bg-surface-900">
    <!-- Mobile top bar -->
    <div class="md:hidden sticky top-0 z-20 bg-surface-800 border-b border-border px-4 py-3 flex items-center gap-3">
      <button
        class="text-slate-300 hover:text-white p-1 -m-1"
        aria-label="Open menu"
        on:click={() => (sidebarOpen = true)}
      >
        <svg class="w-6 h-6" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" d="M4 6h16M4 12h16M4 18h16"/>
        </svg>
      </button>
      <div class="flex items-center gap-2">
        <div class="w-6 h-6 bg-indigo-600 rounded-md flex items-center justify-center">
          <svg class="w-3.5 h-3.5 text-white" fill="none" viewBox="0 0 24 24">
            <path stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
              d="M2.25 18L9 11.25l4.306 4.307a11.95 11.95 0 015.814-5.519l2.74-1.22m0 0l-5.94-2.28m5.94 2.28l-2.28 5.941"/>
          </svg>
        </div>
        <span class="font-semibold text-white text-sm tracking-tight">Sharp Edge</span>
      </div>
    </div>

    <div class="max-w-7xl mx-auto px-4 sm:px-6 py-4 sm:py-6">
      <slot />
    </div>
  </main>
</div>
