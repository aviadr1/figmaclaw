"""Export Figma local variables through the Figma MCP plugin runtime.

This is the non-REST authoritative variables reader. Figma's REST
``/variables/local`` endpoint requires the ``file_variables:read`` scope, which
is not available to every deployment even when normal file reads succeed. The
MCP ``use_figma`` tool runs inside Figma's plugin runtime and can read the
same local variable definitions through ``figma.variables``.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from figmaclaw.figma_api_models import FigmaAPIValidationError, LocalVariablesResponse, _validate
from figmaclaw.figma_mcp import FigmaMcpClient, FigmaMcpError

_MCP_VARIABLE_CHUNK_SIZE = 50

_MCP_VARIABLES_COMMON_JS = r"""
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

localVariables.sort((a, b) => a.id.localeCompare(b.id));

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
"""

_EXPORT_LOCAL_VARIABLES_JS = (
    "(async () => {\n"
    + _MCP_VARIABLES_COMMON_JS
    + r"""

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
)

_EXPORT_LOCAL_VARIABLES_SUMMARY_JS = (
    "(async () => {\n"
    + _MCP_VARIABLES_COMMON_JS
    + r"""
  return JSON.stringify({
    status: 200,
    error: false,
    meta: {
      variable_count: localVariables.length,
      collections: localCollections.map((collection) => [
        collection.id,
        safeGet(collection, "name", ""),
        safeGet(collection, "key", ""),
        safeGet(collection, "modes", []).map((mode) => [
          mode.modeId,
          safeGet(mode, "name", ""),
        ]),
        safeGet(collection, "defaultModeId", ""),
        Boolean(safeGet(collection, "remote", false)),
        Boolean(safeGet(collection, "hiddenFromPublishing", false)),
      ]),
    },
  });
})()
"""
)


def _export_local_variables_chunk_js(offset: int, limit: int) -> str:
    code = (
        "(async () => {\n"
        + _MCP_VARIABLES_COMMON_JS
        + r"""
  return JSON.stringify({
    status: 200,
    error: false,
    meta: {
      offset: __OFFSET__,
      limit: __LIMIT__,
      variables: localVariables.slice(__OFFSET__, __END__).map((variable) => {
        const valuesByMode = {};
        for (const [modeId, value] of Object.entries(variable.valuesByMode || {})) {
          valuesByMode[modeId] = cloneValue(value);
        }
        return [
          variable.id,
          safeGet(variable, "name", ""),
          safeGet(variable, "key", ""),
          safeGet(variable, "variableCollectionId", ""),
          safeGet(variable, "resolvedType", ""),
          valuesByMode,
          Boolean(safeGet(variable, "remote", false)),
          safeGet(variable, "description", ""),
          Boolean(safeGet(variable, "hiddenFromPublishing", false)),
          Array.from(safeGet(variable, "scopes", [])),
          safeGet(variable, "codeSyntax", {}),
        ];
      }),
    },
  });
})()
"""
    )
    return (
        code.replace("__OFFSET__", str(offset))
        .replace("__LIMIT__", str(limit))
        .replace("__END__", str(offset + limit))
    )


async def get_local_variables_via_mcp(
    file_key: str,
    *,
    client: FigmaMcpClient | None = None,
    chunk_size: int = _MCP_VARIABLE_CHUNK_SIZE,
) -> LocalVariablesResponse:
    """Read local variable definitions from Figma through MCP ``use_figma``."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be > 0")

    mcp = client or FigmaMcpClient.auto()
    async with mcp.session(timeout=120.0) as sess:
        return await _get_local_variables_via_mcp_runner(
            sess.use_figma,
            file_key=file_key,
            chunk_size=chunk_size,
        )


async def _get_local_variables_via_mcp_runner(
    use_figma: Callable[[str, str, str], Awaitable[dict[str, Any]]],
    *,
    file_key: str,
    chunk_size: int,
) -> LocalVariablesResponse:
    summary_result = await use_figma(
        file_key,
        _EXPORT_LOCAL_VARIABLES_SUMMARY_JS,
        "Export local variable collection summary",
    )
    summary = _json_payload_from_mcp_result(summary_result, file_key=file_key)
    meta = summary.get("meta", {})
    total = int(meta.get("variable_count", 0))

    variables: dict[str, Any] = {}
    for offset in range(0, total, chunk_size):
        chunk_result = await use_figma(
            file_key,
            _export_local_variables_chunk_js(offset, chunk_size),
            f"Export local variable definitions {offset}-{offset + chunk_size}",
        )
        chunk = _json_payload_from_mcp_result(chunk_result, file_key=file_key)
        for row in chunk.get("meta", {}).get("variables", []):
            variable = _expand_variable_row(row)
            variables[variable["id"]] = variable

    collections = _expand_collections(meta.get("collections", []), variables)
    return _validate_local_variables_payload(
        {
            "status": 200,
            "error": False,
            "meta": {
                "variables": variables,
                "variableCollections": collections,
            },
        },
        file_key=file_key,
    )


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

    return _validate_local_variables_payload(payload, file_key=file_key)


def _validate_local_variables_payload(
    payload: dict[str, Any],
    *,
    file_key: str,
) -> LocalVariablesResponse:
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


def _json_payload_from_mcp_result(result: dict[str, Any], *, file_key: str) -> dict[str, Any]:
    if result.get("isError"):
        raise FigmaMcpError(f"MCP variables export failed: {_mcp_text(result)}")

    for candidate in _candidate_payloads(result):
        parsed = _parse_candidate(candidate)
        if isinstance(parsed, dict) and isinstance(parsed.get("meta"), dict):
            return parsed

    raise FigmaMcpError(
        f"MCP variables export did not return a JSON payload: {_mcp_text(result)[:500]}"
    )


def _expand_variable_row(row: Any) -> dict[str, Any]:
    if not isinstance(row, list) or len(row) != 11:
        raise FigmaMcpError(f"MCP variable row has unexpected shape: {row!r}")
    return {
        "id": row[0],
        "name": row[1],
        "key": row[2],
        "variableCollectionId": row[3],
        "resolvedType": row[4],
        "valuesByMode": row[5],
        "remote": row[6],
        "description": row[7],
        "hiddenFromPublishing": row[8],
        "scopes": row[9],
        "codeSyntax": row[10],
    }


def _expand_collections(rows: Any, variables: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(rows, list):
        raise FigmaMcpError(f"MCP collection summary has unexpected shape: {rows!r}")

    collections: dict[str, Any] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) != 7:
            raise FigmaMcpError(f"MCP collection row has unexpected shape: {row!r}")
        coll_id = row[0]
        collections[coll_id] = {
            "id": coll_id,
            "name": row[1],
            "key": row[2],
            "modes": [
                {"modeId": mode[0], "name": mode[1]}
                for mode in row[3]
                if isinstance(mode, list) and len(mode) == 2
            ],
            "defaultModeId": row[4],
            "remote": row[5],
            "hiddenFromPublishing": row[6],
            "variableIds": [],
        }

    for variable in variables.values():
        coll_id = variable.get("variableCollectionId")
        if coll_id in collections:
            collections[coll_id]["variableIds"].append(variable["id"])

    return collections


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
