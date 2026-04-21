# `apply_patch`

`apply_patch` is the right tool for **patch-level changes**:

- multi-file edits,
- file creation / deletion / rename,
- changes that are easiest to express as unified diff text.

It is not the default choice for everyday single-location replacements. If the change is small and local, `edit` or `multi_edit` is usually safer.

## When to use it

Good fit:

- one operation needs to touch several files,
- you need patch semantics rather than one local replacement,
- you need add / delete / rename behavior.

Bad fit:

- a small edit in one file,
- situations where you have not yet read enough file context,
- cases where patch construction is more complex than the change itself.

## Risk profile

`apply_patch` is powerful, but it asks more from the agent:

- the patch text must be structurally valid,
- all paths must stay inside the workspace,
- mistakes in local context understanding can make the patch fail harder than a simple `edit` would.

So this is a high-power tool, not the default first choice.

## Return value

On success, expect structured change summaries such as:

- affected paths,
- change status (add / delete / modify / rename),
- patch application outcome.

## When it is better than `edit` / `multi_edit`

- several files must change together,
- you want to preserve a patch-shaped change,
- rename / delete / add behavior is part of the request.

## When it is worse than `edit` / `multi_edit`

- the change is small and local,
- you want to minimize patch construction complexity,
- the agent is still converging on the exact change through file reads.
