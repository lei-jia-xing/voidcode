from voidcode.agent import (
    ADVISOR_AGENT_MANIFEST,
    EXPLORE_AGENT_MANIFEST,
    LEADER_AGENT_MANIFEST,
    PRODUCT_AGENT_MANIFEST,
    RESEARCHER_AGENT_MANIFEST,
    WORKER_AGENT_MANIFEST,
    get_builtin_agent_manifest,
    list_builtin_agent_manifests,
)


def test_builtin_agent_registry_exposes_leader_manifest() -> None:
    leader = get_builtin_agent_manifest("leader")

    assert leader == LEADER_AGENT_MANIFEST
    assert leader is not None
    assert leader.name == "Leader"
    assert leader.mode == "primary"
    assert leader.prompt_profile == "leader"
    assert leader.execution_engine == "single_agent"
    assert "read_file" in leader.tool_allowlist
    assert "write_file" in leader.tool_allowlist
    assert "mcp/*" in leader.tool_allowlist


def test_builtin_agent_registry_lists_leader_manifest() -> None:
    manifests = list_builtin_agent_manifests()

    assert manifests == (
        LEADER_AGENT_MANIFEST,
        WORKER_AGENT_MANIFEST,
        ADVISOR_AGENT_MANIFEST,
        EXPLORE_AGENT_MANIFEST,
        RESEARCHER_AGENT_MANIFEST,
        PRODUCT_AGENT_MANIFEST,
    )


def test_builtin_agent_registry_exposes_future_role_skeletons() -> None:
    worker = get_builtin_agent_manifest("worker")
    advisor = get_builtin_agent_manifest("advisor")
    explore = get_builtin_agent_manifest("explore")
    researcher = get_builtin_agent_manifest("researcher")
    product = get_builtin_agent_manifest("product")

    assert worker == WORKER_AGENT_MANIFEST
    assert advisor == ADVISOR_AGENT_MANIFEST
    assert explore == EXPLORE_AGENT_MANIFEST
    assert researcher == RESEARCHER_AGENT_MANIFEST
    assert product == PRODUCT_AGENT_MANIFEST

    for manifest in (worker, advisor, explore, researcher, product):
        assert manifest is not None
        assert manifest.mode == "subagent"
        assert manifest.execution_engine is None
        assert manifest.tool_allowlist

    assert "edit" in worker.tool_allowlist
    assert "write_file" not in advisor.tool_allowlist
    assert "ast_grep_search" in explore.tool_allowlist
    assert researcher.tool_allowlist == ("web_search", "web_fetch", "code_search")
    assert product.tool_allowlist == ("read_file", "list", "glob", "grep")
