"""Agent profile registry owned by the subagents plugin.

Profile discovery (builtin + workspace/user/package manifests) and prompt rendering live
here; child execution stays in ``subagents_plugin``. ``subagent_type`` resolves through
this registry, so the discovered profiles are exactly the ones ``task``/``task_batch`` can execute.
"""
