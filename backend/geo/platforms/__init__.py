from geo.platforms import adapters as _adapters  # noqa: F401  (registers the built-in platforms)
from geo.platforms.base import REGISTRY, Extracted, PlatformAdapter, adapter, adapter_for_record

__all__ = ["REGISTRY", "Extracted", "PlatformAdapter", "adapter", "adapter_for_record"]
