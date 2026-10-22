"""Agent profile registry owned by the delegation plugin.

Profile discovery (builtin + workspace/user/package manifests) and prompt rendering live
here; child execution stays in ``delegation_plugin``. ``subagent_type`` resolves through
this registry, so the profiles ``agents``/``agents.show`` display are exactly the ones
``task``/``task_batch`` can execute.
"""
