"""NFL props: projections from NFL-API, prices from FanDuel, priced here.

The MLB screens compute their own features from Statcast. Football does not
need that — the projection model already exists in the nfl-data-py repo and
runs weekly inside NFL-API — so this package is a client, a calibration layer
and a pricing model rather than a data pipeline.

  projections  NFL-API client; undoes the availability discount baked into the
               stored component projections
  odds         FanDuel's public NFL board — two-sided prop lines, alt ladders,
               anytime-TD prices, game markets
  model        projection -> probability -> edge, and the market rescaling that
               has to happen before any of that means anything
  screen       the week's board, cached and warmed in the background
"""

from . import model, names, odds, projections, screen  # noqa: F401
