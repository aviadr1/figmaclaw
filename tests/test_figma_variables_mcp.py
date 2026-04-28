"""Tests for Figma MCP variable-definition export parsing."""

from __future__ import annotations

import json

import pytest

from figmaclaw.figma_mcp import FigmaMcpError
from figmaclaw.figma_variables_mcp import (
    _get_local_variables_via_mcp_runner,
    local_variables_response_from_mcp_result,
)


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


@pytest.mark.asyncio
async def test_get_local_variables_via_mcp_runner_assembles_compact_chunks() -> None:
    calls: list[str] = []

    async def use_figma(_file_key: str, _code: str, description: str) -> dict:
        calls.append(description)
        if description == "Export local variable collection summary":
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "status": 200,
                                "error": False,
                                "meta": {
                                    "variable_count": 2,
                                    "collections": [
                                        [
                                            "VariableCollectionId:1:0",
                                            "Semantic",
                                            "coll-key",
                                            [["1:0", "Light"]],
                                            "1:0",
                                            False,
                                            False,
                                        ]
                                    ],
                                },
                            }
                        ),
                    }
                ]
            }
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "status": 200,
                            "error": False,
                            "meta": {
                                "variables": [
                                    [
                                        "VariableID:libabc/1:1",
                                        "color/fg/default",
                                        "var-key",
                                        "VariableCollectionId:1:0",
                                        "COLOR",
                                        {"1:0": {"r": 1, "g": 1, "b": 1, "a": 1}},
                                        False,
                                        "",
                                        False,
                                        ["ALL_FILLS"],
                                        {"WEB": "--color-fg-default"},
                                    ],
                                    [
                                        "VariableID:libabc/1:2",
                                        "radius/default",
                                        "radius-key",
                                        "VariableCollectionId:1:0",
                                        "FLOAT",
                                        {"1:0": 8},
                                        False,
                                        "",
                                        False,
                                        ["CORNER_RADIUS"],
                                        {},
                                    ],
                                ]
                            },
                        }
                    ),
                }
            ]
        }

    response = await _get_local_variables_via_mcp_runner(
        use_figma,
        file_key="file123",
        chunk_size=50,
    )

    assert calls == [
        "Export local variable collection summary",
        "Export local variable definitions 0-50",
    ]
    collection = response.meta.variableCollections["VariableCollectionId:1:0"]
    assert collection.variableIds == ["VariableID:libabc/1:1", "VariableID:libabc/1:2"]
    assert response.meta.variables["VariableID:libabc/1:2"].valuesByMode["1:0"] == 8
