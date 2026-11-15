"""doc_reader — the reading agent: read (doc_parser) → cut (doc_chunker) → find (doc_index).

One server (``python -m doc_reader serve``) puts the three desks behind one product screen and one
A2A agent card. The components stay importable on their own (``doc_parser``, ``doc_chunker``,
``doc_index``); this package is only the app layer over them.
"""

__version__ = "0.1.0"
