<script lang="ts">
  export let label: string;
  export let value: string;
  export let sub: string = '';
  export let trend: 'pos' | 'neg' | 'neutral' = 'neutral';
  export let loading = false;
</script>

<div class="card flex flex-col gap-1.5 min-w-0">
  <p class="text-xs font-medium text-slate-400 uppercase tracking-wider">{label}</p>

  {#if loading}
    <div class="h-7 w-24 bg-surface-600 rounded animate-pulse"></div>
  {:else}
    <p class="text-lg sm:text-2xl font-bold tabular-nums truncate
      {trend === 'pos' ? 'text-emerald-400' : trend === 'neg' ? 'text-red-400' : 'text-slate-100'}">
      {value}
    </p>
  {/if}

  {#if sub && !loading}
    <p class="text-xs text-slate-500">{sub}</p>
  {/if}
</div>
