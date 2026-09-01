<script lang="ts">
  import ScreenPicker from '$lib/components/ScreenPicker.svelte';

  const screens = [
    { href: '/nfl/props', label: 'Props', blurb: 'Yardage & receptions vs the posted line' },
    { href: '/nfl/touchdowns', label: 'Touchdowns', blurb: 'Anytime scorer: model vs the price' },
    { href: '/nfl/games', label: 'Games', blurb: 'Moneyline, spread and total' },
  ];
</script>

<div class="space-y-4">
  <ScreenPicker league="NFL" {screens} />
  <slot />
</div>
