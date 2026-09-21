"""Corpus providers.

Two kinds, and the difference is load-bearing:

  BULK      has a downloadable corpus; yields topics() and sources() and can
            populate a registry from nothing. Curlie, DMOZ, Wikidata.

  DISCOVERY has no corpus to download, only a query interface; yields
            discover(topic) -> sources. Marginalia, and eventually tau-search's
            own web backends. These EXTEND a registry that already has a
            taxonomy to hang results on.

Discovery providers are how the registry self-populates from its own fallback
tier: the broker misses, the open web answers, the answer becomes a source.
"""

from .curlie import CurlieProvider
from .dmoz import DmozProvider
from .marginalia import MarginaliaProvider
from .wikidata import WikidataProvider

BULK = {
    "curlie": CurlieProvider,
    "dmoz": DmozProvider,
    "wikidata": WikidataProvider,
}

DISCOVERY = {
    "marginalia": MarginaliaProvider,
}

ALL = {**BULK, **DISCOVERY}


def get(name):
    try:
        return ALL[name]
    except KeyError:
        raise SystemExit(
            f"unknown provider {name!r}; have: {', '.join(sorted(ALL))}")
