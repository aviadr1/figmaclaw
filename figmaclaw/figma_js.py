"""Reusable JavaScript snippets for generated Figma Plugin API scripts."""

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
