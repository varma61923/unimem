"""Thin Apache-2.0 remote HTTP client for UniMem Gateways and Hub servers."""

from unimem_client.client import (
    LEARNING_OPERATIONS,
    LEARNING_SCHEMA,
    LearningClient,
    MemoryClient,
    RemoteMemoryClient,
    UniMemClientError,
)

__version__ = "1.0.0"
__all__ = [
    "LEARNING_OPERATIONS",
    "LEARNING_SCHEMA",
    "LearningClient",
    "MemoryClient",
    "RemoteMemoryClient",
    "UniMemClientError",
    "__version__",
]
