#!/usr/bin/env python3
"""Chemistry Source Collector V6.3 OA-only compatibility entry.

Implementation now lives in the chemistry_collector package. Existing commands
and imports from collector remain compatible.
"""
from chemistry_collector import *  # noqa: F401,F403
from chemistry_collector import feasibility as _feasibility
from chemistry_collector.cli import main
from chemistry_collector.discovery import journal_article_iterator


# Keep the legacy monkey-patching seam used by external tests and callers.
def collect_papers(client, args, root):
    original = _feasibility.journal_article_iterator
    _feasibility.journal_article_iterator = journal_article_iterator
    try:
        return _feasibility.collect_papers(client, args, root)
    finally:
        _feasibility.journal_article_iterator = original


if __name__ == "__main__":
    main()
