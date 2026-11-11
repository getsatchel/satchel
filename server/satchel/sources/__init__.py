"""Source adapters (design §8.3): import the metadata of tools teams already use (dbt, ...) as
ordinary context items. The adapter contract lives in the separate ``satchel-adapter-sdk``
distribution; this package is the server side: discovery, mapping onto items, sync."""

from satchel.sources.registry import AdapterEntry, discover, get_entry

__all__ = ["AdapterEntry", "discover", "get_entry"]
