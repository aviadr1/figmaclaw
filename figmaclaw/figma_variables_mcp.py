"""Export Figma local variables through the Figma MCP plugin runtime.

This is the non-REST authoritative variables reader. Figma's REST
``/variables/local`` endpoint requires the ``file_variables:read`` scope, which
is not available to every deployment even when normal file reads succeed. The
MCP ``use_figma`` tool runs inside Figma's plugin runtime and can read the
same local variable definitions through ``figma.variables``.
"""

from __future__ import annotations

import json
from typing import Any

from figmaclaw.figma_api_models import FigmaAPIValidationError, LocalVariablesResponse, _validate
from figmaclaw.figma_mcp import FigmaMcpClient, FigmaMcpError

_EXPORT_LOCAL_VARIABLES_JS = r"""
(async () => {
  const variablesApi = figma.variables;
  if (!variablesApi) {
    throw new Error("figma.variables is not available in this Figma runtime");
  }

  const localVariables = variablesApi.getLocalVariablesAsync
    ? await variablesApi.getLocalVariablesAsync()
    : variablesApi.getLocalVariables();
  const localCollections = variablesApi.getLocalVariableCollectionsAsync
    ? await variablesApi.getLocalVariableCollectionsAsync()
    : variablesApi.getLocalVariableCollections();

  const cloneValue = (value) => {
    if (value === null || value === undefined || typeof value !== "object") {
      return value;
    }
    if (value.type === "VARIABLE_ALIAS") {
      return { type: "VARIABLE_ALIAS", id: value.id };
    }
    if ("r" in value && "g" in value && "b" in value) {
      return { r: value.r, g: value.g, b: value.b, a: value.a ?? 1 };
    }
    return JSON.parse(JSON.stringify(value));
  };

  const safeGet = (target, key, fallback) => {
    try {
      const value = target[key];
      return value === undefined || value === null ? fallback : value;
    } catch (_error) {
      return fallback;
    }
  };

  const variableCollections = {};
  for (const collection of localCollections) {
    variableCollections[collection.id] = {
      id: collection.id,
      name: safeGet(collection, "name", ""),
      key: safeGet(collection, "key", ""),
      modes: safeGet(collection, "modes", []).map((mode) => ({
        modeId: mode.modeId,
        name: safeGet(mode, "name", ""),
      })),
      defaultModeId: safeGet(collection, "defaultModeId", ""),
      remote: Boolean(safeGet(collection, "remote", false)),
      hiddenFromPublishing: Boolean(safeGet(collection, "hiddenFromPublishing", false)),
      variableIds: Array.from(safeGet(collection, "variableIds", [])),
    };
  }

  const variables = {};
  for (const variable of localVariables) {
    const valuesByMode = {};
    for (const [modeId, value] of Object.entries(variable.valuesByMode || {})) {
      valuesByMode[modeId] = cloneValue(value);
    }

    variables[variable.id] = {
      id: variable.id,
      name: safeGet(variable, "name", ""),
      key: safeGet(variable, "key", ""),
      variableCollectionId: safeGet(variable, "variableCollectionId", ""),
      resolvedType: safeGet(variable, "resolvedType", ""),
      valuesByMode,
      remote: Boolean(safeGet(variable, "remote", false)),
      description: safeGet(variable, "description", ""),
      hiddenFromPublishing: Boolean(safeGet(variable, "hiddenFromPublishing", false)),
      scopes: Array.from(safeGet(variable, "scopes", [])),
      codeSyntax: safeGet(variable, "codeSyntax", {}),
    };
  }

  return JSON.stringify({
    status: 200,
    error: false,
    meta: { variables, variableCollections },
  });
})()
"""


async def get_local_variables_via_mcp(
    file_key: str,
    *,
    client: FigmaMcpClient | None = None,
) -> LocalVariablesResponse:
    """Read local variable definitions from Figma through MCP ``use_figma``."""
    mcp = client or FigmaMcpClient.auto()
    result = await mcp.use_figma(
        file_key=file_key,
        code=_EXPORT_LOCAL_VARIABLES_JS,
        description="Export local variable definitions",
    )
    return local_variables_response_from_mcp_result(result, file_key=file_key)


def local_variables_response_from_mcp_result(
    result: dict[str, Any],
    *,
    file_key: str,
) -> LocalVariablesResponse:
    """Parse an MCP ``use_figma`` result into the REST-compatible model."""
    if result.get("isError"):
        raise FigmaMcpError(f"MCP variables export failed: {_mcp_text(result)}")

    payload = _extract_variables_payload(result)
    if payload is None:
        raise FigmaMcpError(
            "MCP variables export did not return a LocalVariablesResponse payload: "
            f"{_mcp_text(result)[:500]}"
        )

    try:
        return _validate(
            LocalVariablesResponse,
            payload,
            endpoint="MCP use_figma local variables export",
            context=f"file_key={file_key}",
        )
    except FigmaAPIValidationError:
        raise
    except Exception as exc:
        raise FigmaMcpError(f"MCP variables payload validation failed: {exc}") from exc


def _extract_variables_payload(result: dict[str, Any]) -> dict[str, Any] | None:
    for candidate in _candidate_payloads(result):
        parsed = _parse_candidate(candidate)
        if isinstance(parsed, dict) and isinstance(parsed.get("meta"), dict):
            meta = parsed["meta"]
            if "variables" in meta and "variableCollections" in meta:
                return parsed
    return None


def _candidate_payloads(result: dict[str, Any]) -> list[Any]:
    candidates: list[Any] = [result.get("structuredContent"), result.get("result")]

    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            candidates.extend([item.get("json"), item.get("data"), item.get("text")])

    candidates.extend([result.get("output"), result.get("text")])
    return [candidate for candidate in candidates if candidate is not None]


def _parse_candidate(candidate: Any) -> Any:
    if isinstance(candidate, dict | list):
        return candidate
    if not isinstance(candidate, str):
        return None

    text = candidate.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None


def _mcp_text(result: dict[str, Any]) -> str:
    content = result.get("content")
    if isinstance(content, list):
        parts = [item.get("text", "") for item in content if isinstance(item, dict)]
        text = "\n".join(part for part in parts if part)
        if text:
            return text
    return json.dumps(result, sort_keys=True)
