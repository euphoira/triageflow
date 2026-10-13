"""Illustrative tenant cache. Contains a deliberate missing-tenant-key bug."""

_cache: dict = {}


def get_profile(tenant_id: str, user_id: str, loader):
    """User IDs are only unique within a tenant."""
    key = user_id
    if key not in _cache:
        _cache[key] = loader(tenant_id, user_id)
    return _cache[key]
