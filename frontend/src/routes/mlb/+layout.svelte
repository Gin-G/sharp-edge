<script lang="ts">
  import ScreenPicker from '$lib/components/ScreenPicker.svelte';

  const screens = [
    { href: '/mlb/batters', label: 'Batters', blurb: 'To record a hit — the daily parlay' },
    { href: '/mlb/homers', label: 'Home Runs', blurb: 'HR probability: power, barrels, parks' },
  ];
</script>

<div class="space-y-4">
  <ScreenPicker league="MLB" {screens} />
  <slot />
</div>
