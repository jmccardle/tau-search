"""presearch -- a federated-search broker over a JMFTS appliance.

Metasearch broadcasts a query to every backend and merges. Federated search
SELECTS which collections to ask first. This package builds and maintains the
resource description index that selection needs, from corpora that already
map topics to sources -- Curlie, DMOZ, Wikidata -- and extends it from live
discovery against Marginalia.

Storage lives in JMFTS: topics are a document tree, sources hang beneath them,
and facts about a source are triples with temporal validity, so a source that
moves or dies is superseded rather than overwritten.
"""

__version__ = "0.1.0"
