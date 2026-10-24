"""Structured read views must expose omitted content and usable continuation."""

import json

import pytest

from voidcode.capabilities.files.read_formats import render_notebook


@pytest.mark.parametrize("cell", [None, 0])
def test_notebook_omitted_outputs_are_partial_and_recoverable(tmp_path, cell):
    path = tmp_path / "notebook.ipynb"
    path.write_text(
        json.dumps(
            {
                "cells": [
                    {
                        "cell_type": "code",
                        "source": "print('first')",
                        "outputs": [
                            {"output_type": "stream", "text": "first"},
                            {"output_type": "stream", "text": "second"},
                        ],
                    },
                    {"cell_type": "markdown", "source": "later cell"},
                ]
            }
        ),
        encoding="utf-8",
    )
    first = render_notebook(path, offset=1, limit=10, cell=cell, output_limit=1)
    assert first["partial"] is True
    assert first["truncated"] is True
    assert first["next_offset"] is None
    code = first["cells"][0]
    assert code["outputs_truncated"] is True
    assert code["next_output_offset"] == 2
    assert code["outputs"][0]["text"] == "first"
    assert first["next_output_offset"] == (2 if cell == 0 else None)

    second = render_notebook(path, offset=1, limit=10, cell=code["index"], output_offset=code["next_output_offset"], output_limit=1)
    assert second["cells"][0]["outputs"][0]["text"] == "second"
    assert second["partial"] is False
    assert second["next_output_offset"] is None
