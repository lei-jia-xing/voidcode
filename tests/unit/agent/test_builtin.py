from voidcode.agent import LEADER_AGENT_MANIFEST, get_builtin_agent_manifest, list_builtin_agent_manifests


def test_builtin_agent_registry_exposes_leader_manifest() -> None:
    leader = get_builtin_agent_manifest("leader")

    assert leader == LEADER_AGENT_MANIFEST
    assert leader is not None
    assert leader.name == "Leader"
    assert leader.mode == "primary"
    assert leader.prompt_profile == "leader"
    assert leader.execution_engine == "single_agent"


def test_builtin_agent_registry_lists_leader_manifest() -> None:
    manifests = list_builtin_agent_manifests()

    assert manifests == (LEADER_AGENT_MANIFEST,)
