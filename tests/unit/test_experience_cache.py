from stds.data.cache import AutoCache


def test_auto_cache_isolates_experience_scopes_without_breaking_legacy_keys():
    cache = AutoCache()
    cache.put("转身", "legacy")
    cache.put("转身", "experience-a", scope="exp:a")
    cache.put("转身", "experience-b", scope="exp:b")

    assert cache.get("转身") == "legacy"
    assert cache.get("转身", scope="exp:a") == "experience-a"
    assert cache.get("转身", scope="exp:b") == "experience-b"
    assert cache.get("转身", scope="exp:missing") is None
