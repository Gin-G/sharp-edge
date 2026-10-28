<script lang="ts">
  import ScreenPicker from '$lib/components/ScreenPicker.svelte';

  const screens = [
    { href: '/nfl/props', label: 'Props', blurb: 'Yardage & receptions vs the posted line' },
    { href: '/nfl/touchdowns', label: 'Touchdowns', blurb: 'Anytime scorer picks: calibrated odds vs the price' },
    { href: '/nfl/games', label: 'Games', blurb: 'Simulated predictions and moneyline picks' },
    { href: '/nfl/record', label: 'Record', blurb: 'Every pick we made, graded: record, units, ROI' },
  ];
</script>

<div class="space-y-4">
  <ScreenPicker league="NFL" {screens} />
  <slot />
</div>
