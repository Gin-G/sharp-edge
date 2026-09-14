"""Assumed lineup slot, and the at-bats it projects."""

import pytest

from sharp_edge import lineup


class TestExpectedSlot:
    def test_the_mode_of_the_last_three_starts(self):
        assert lineup.expected_slot([2, 5, 2]) == 2

    def test_only_the_last_three_count(self):
        """Lineups drift with form, so a role the batter has left must not
        outvote the one he is in — 'mode of all' scored worse than 'last
        start' over 5,934 starter-games for exactly this reason."""
        assert lineup.expected_slot([9, 9, 9, 9, 1, 1, 1]) == 1

    def test_a_tie_breaks_toward_the_most_recent(self):
        assert lineup.expected_slot([5, 2, 5, 2]) == 2

    def test_no_history_is_none_rather_than_a_guess(self):
        """The caller should know the difference between 'hits seventh' and
        'we have no idea', so this does not quietly return DEFAULT_SLOT."""
        assert lineup.expected_slot([]) is None
        assert lineup.expected_slot([None, 0, 12]) is None

    def test_bench_games_are_absent_not_zero(self):
        assert lineup.expected_slot([3, 0, 3]) == 3


class TestProjectedAb:
    def test_leadoff_gets_more_swings_than_ninth(self):
        assert lineup.projected_ab(1) > lineup.projected_ab(9)
        assert lineup.projected_ab(1) - lineup.projected_ab(9) == pytest.approx(0.90, abs=0.05)

    def test_an_unknown_slot_falls_back_to_the_bottom_third(self):
        """A call-up with no established place is not a league-average hitter
        in the order, and assuming so would flatter him."""
        assert lineup.projected_ab(None) == lineup.projected_ab(lineup.DEFAULT_SLOT)

    def test_a_walker_loses_at_bats(self):
        """A walk spends a trip without offering a swing."""
        assert lineup.projected_ab(1, walk_rate=0.20) < lineup.projected_ab(1, walk_rate=0.05)

    def test_a_degenerate_walk_rate_cannot_zero_the_projection(self):
        """A three-game sample can read 100%; that must not project no swings."""
        assert lineup.projected_ab(1, walk_rate=1.0) >= 0.5 * lineup.SLOT_PA[1]

    def test_out_of_range_slots_are_clamped(self):
        assert lineup.projected_ab(0) == lineup.projected_ab(1)
        assert lineup.projected_ab(15) == lineup.projected_ab(9)


def test_slots_come_out_of_a_boxscore_summary():
    summaries = [
        {"away": {"slots": {101: 3}}, "home": {"slots": {202: 7}}},
        {"away": {"slots": {}}, "home": {"slots": {101: 4}}},   # changed teams/side
        {"home": {"slots": {101: 3}}},                          # away key absent
    ]
    assert lineup.slots_from_boxscores(summaries, 101) == [3, 4, 3]
    assert lineup.slots_from_boxscores(summaries, 999) == []
