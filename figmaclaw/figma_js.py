"""Reusable JavaScript snippets for generated Figma Plugin API scripts.

Both helpers assume `targetPage` and `NAMESPACE` are bound in the enclosing
template scope. They read from / write to the audit-page idMap stored as
chunked SharedPluginData (Figma caps each entry at ~100KB so we shard the
JSON across `${prefix}.0`, `${prefix}.1`, … with the chunk count in
`countKey`).
"""

READ_SPD_CHUNKS_JS = r"""
function readSPDChunks(prefix, countKey) {
  const count = Number(targetPage.getSharedPluginData(NAMESPACE, countKey) || "0");
  let value = "";
  for (let i = 0; i < count; i++) {
    value += targetPage.getSharedPluginData(NAMESPACE, `${prefix}.${i}`) || "";
  }
  return value;
}
""".strip()


WRITE_SPD_CHUNKS_JS = r"""
function writeSPDChunks(prefix, countKey, value, size) {
  const oldCount = Number(targetPage.getSharedPluginData(NAMESPACE, countKey) || "0");
  const chunks = [];
  for (let i = 0; i < value.length; i += size) chunks.push(value.slice(i, i + size));
  targetPage.setSharedPluginData(NAMESPACE, countKey, String(chunks.length));
  for (let i = 0; i < chunks.length; i++) {
    targetPage.setSharedPluginData(NAMESPACE, `${prefix}.${i}`, chunks[i]);
  }
  for (let i = chunks.length; i < oldCount; i++) {
    targetPage.setSharedPluginData(NAMESPACE, `${prefix}.${i}`, "");
  }
  return chunks.length;
}
""".strip()
