import { redirect } from '@sveltejs/kit';

// Kept so bookmarks from before the MLB tab still land on the board.
export const load = () => {
  throw redirect(308, '/mlb/homers');
};
