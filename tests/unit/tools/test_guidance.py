from voidcode.tools.contracts import ToolDefinition
from voidcode.tools.guidance import definition_with_guidance


def test_definition_with_guidance_preserves_path_argument_keys() -> None:
    definition = ToolDefinition(
        name="read_file",
        description="Read a file",
        input_schema={"filePath": {"type": "string"}},
        path_argument_keys=("filePath",),
    )

    decorated = definition_with_guidance(definition)

    assert decorated.path_argument_keys == ("filePath",)
