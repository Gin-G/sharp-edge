import { redirect } from '@sveltejs/kit';

// /mlb is the tab; the batter board is what it opens on.
export const load = () => {
  throw redirect(307, '/mlb/batters');
};
