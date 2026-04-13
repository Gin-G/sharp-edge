<script lang="ts">
  import '../app.css';
  import Sidebar from '$lib/components/Sidebar.svelte';
</script>

<div class="flex h-screen overflow-hidden">
  <Sidebar />

  <main class="flex-1 overflow-y-auto bg-surface-900">
    <div class="max-w-7xl mx-auto px-6 py-6">
      <slot />
    </div>
  </main>
</div>
