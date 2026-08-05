from stds.data.cache import AutoCache
from stds.data.cache import decision_cache_scope
from stds.domain.models import Source, StdsElement, StdsResult
from stds.review.flywheel import on_review_confirmed


def test_auto_cache_isolates_experience_scopes_without_breaking_legacy_keys():
    cache = AutoCache()
    cache.put("转身", "legacy")
    cache.put("转身", "experience-a", scope="exp:a")
    cache.put("转身", "experience-b", scope="exp:b")

    assert cache.get("转身") == "legacy"
    assert cache.get("转身", scope="exp:a") == "experience-a"
    assert cache.get("转身", scope="exp:b") == "experience-b"
    assert cache.get("转身", scope="exp:missing") is None


def test_review_and_resolver_share_digest_and_common_scope():
    cache = AutoCache()
    element = StdsElement(1, "转身", "L", "S", freq=2, norm_key="转身")
    result = StdsResult(
        element=element,
        chartcode="202 010",
        decision="T,90,NB",
        time_s=1.44,
        cv="V",
        freq=2,
        source=Source.FORMULA,
        confidence=1.0,
        needs_review=False,
    )

    class Deps:
        experience_scope = "upload:abc"
        use_common_chart = True
        use_semantic_experience = True
        history_index = None

    deps = Deps()
    deps.cache = cache
    on_review_confirmed(element, result, deps)

    scope = decision_cache_scope(deps)
    template = cache.get(element.norm_key, scope=scope)
    assert scope == "upload:abc|common=1|semantic=1"
    assert template.time_s == 0.72
    assert template.freq == 1.0
    assert cache.get(element.norm_key) is None


def test_cache_scope_isolated_when_semantic_experience_switch_changes():
    class Deps:
        experience_scope = "upload:abc"
        use_common_chart = False
        use_semantic_experience = True

    deps = Deps()
    semantic_scope = decision_cache_scope(deps)
    deps.use_semantic_experience = False
    lexical_scope = decision_cache_scope(deps)

    assert semantic_scope == "upload:abc|common=0|semantic=1"
    assert lexical_scope == "upload:abc|common=0|semantic=0"
    assert semantic_scope != lexical_scope
