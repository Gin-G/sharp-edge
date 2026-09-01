import { redirect } from '@sveltejs/kit';

// /nfl is the tab; props are the board it opens on.
export const load = () => {
  throw redirect(307, '/nfl/props');
};
