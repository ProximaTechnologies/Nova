# myapp/cache_keys.py
def nova_version_key(key, key_prefix, version):
    return f"{key_prefix}:{key}" if key_prefix else key
