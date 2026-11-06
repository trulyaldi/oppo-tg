"""Opportunity sources. Importing this package registers all implemented sources."""

from src.sources import (
    ashby,  # noqa: F401  (registers "ashby")
    greenhouse,  # noqa: F401  (registers "greenhouse")
    hn,  # noqa: F401  (registers "hn")
    lever,  # noqa: F401  (registers "lever")
    pagewatch,  # noqa: F401  (registers "pagewatch")
    rss,  # noqa: F401  (registers "rss")
)
