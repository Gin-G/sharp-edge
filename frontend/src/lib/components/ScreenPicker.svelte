<script lang="ts">
  /** League header plus a dropdown that switches between that league's
   *  screens.
   *
   *  Each screen is still its own route, so a board stays deep-linkable and
   *  the module cache in `$lib/cache` keeps working — the dropdown navigates,
   *  it doesn't swap a component in place. A `<select>` would be less code but
   *  can't carry a subtitle per option, and picking a market is the one
   *  choice on these pages worth spelling out.
   */
  import { page } from '$app/stores';

  export let league: string;
  export let screens: { href: string; label: string; blurb: string }[];

  let open = false;

  $: current = screens.find((s) => $page.url.pathname.startsWith(s.href)) ?? screens[0];

  function close() {
    open = false;
  }
</script>

<svelte:window on:keydown={(e) => e.key === 'Escape' && close()} />

<div class="flex items-center gap-3">
  <h1 class="text-xl font-bold text-white">{league}</h1>

  <div class="relative">
    <button
      class="flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-medium
             bg-surface-700 text-slate-200 hover:bg-surface-600 border border-border"
      aria-haspopup="listbox"
      aria-expanded={open}
      on:click={() => (open = !open)}
    >
      {current.label}
      <svg class="w-4 h-4 text-slate-400" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" d="M19.5 8.25l-7.5 7.5-7.5-7.5" />
      </svg>
    </button>

    {#if open}
      <!-- Click-away catcher. Sits under the menu but over the page so the
           first click outside dismisses rather than activating something. -->
      <button class="fixed inset-0 z-10 cursor-default" aria-label="Close menu" on:click={close}></button>

      <ul
        class="absolute left-0 mt-1.5 z-20 w-64 rounded-lg border border-border
               bg-surface-800 shadow-xl shadow-black/40 py-1"
        role="listbox"
      >
        {#each screens as s}
          {@const active = s.href === current.href}
          <li>
            <a
              href={s.href}
              on:click={close}
              class="block px-3 py-2 text-sm hover:bg-surface-600
                     {active ? 'text-indigo-300' : 'text-slate-300'}"
              role="option"
              aria-selected={active}
            >
              <span class="font-medium">{s.label}</span>
              <span class="block text-xs text-slate-500 mt-0.5">{s.blurb}</span>
            </a>
          </li>
        {/each}
      </ul>
    {/if}
  </div>
</div>
