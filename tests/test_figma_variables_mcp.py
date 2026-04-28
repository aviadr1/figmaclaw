"""Tests for Figma MCP variable-definition export parsing."""

from __future__ import annotations

import json

import pytest

from figmaclaw.figma_mcp import FigmaMcpError
from figmaclaw.figma_variables_mcp import local_variables_response_from_mcp_result


def _payload() -> dict:
    return {
        "status": 200,
        "error": False,
        "meta": {
            "variables": {
                "VariableID:libabc/1:1": {
                    "id": "VariableID:libabc/1:1",
                    "name": "color/fg/default",
                    "key": "var-key",
                    "variableCollectionId": "VariableCollectionId:1:0",
                    "resolvedType": "COLOR",
                    "valuesByMode": {
                        "1:0": {"r": 1, "g": 1, "b": 1, "a": 1},
                        "1:1": {"type": "VARIABLE_ALIAS", "id": "VariableID:libabc/2:2"},
                    },
                    "scopes": ["ALL_FILLS"],
                    "codeSyntax": {"WEB": "--color-fg-default"},
                }
            },
            "variableCollections": {
                "VariableCollectionId:1:0": {
                    "id": "VariableCollectionId:1:0",
                    "name": "Semantic",
                    "modes": [
                        {"modeId": "1:0", "name": "Light"},
                        {"modeId": "1:1", "name": "Dark"},
                    ],
                    "defaultModeId": "1:0",
                    "variableIds": ["VariableID:libabc/1:1"],
                }
            },
        },
    }


def test_local_variables_response_from_mcp_text_payload() -> None:
    result = {"content": [{"type": "text", "text": json.dumps(_payload())}]}

    response = local_variables_response_from_mcp_result(result, file_key="file123")

    variable = response.meta.variables["VariableID:libabc/1:1"]
    collection = response.meta.variableCollections["VariableCollectionId:1:0"]
    assert variable.name == "color/fg/default"
    assert variable.valuesByMode["1:1"]["type"] == "VARIABLE_ALIAS"
    assert variable.scopes == ["ALL_FILLS"]
    assert collection.name == "Semantic"
    assert collection.modes[1].name == "Dark"


def test_local_variables_response_from_mcp_structured_payload() -> None:
    result = {"structuredContent": _payload()}

    response = local_variables_response_from_mcp_result(result, file_key="file123")

    assert len(response.meta.variables) == 1


def test_local_variables_response_from_mcp_error_raises() -> None:
    result = {"isError": True, "content": [{"type": "text", "text": "runtime failed"}]}

    with pytest.raises(FigmaMcpError, match="runtime failed"):
        local_variables_response_from_mcp_result(result, file_key="file123")
