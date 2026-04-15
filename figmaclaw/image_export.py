"""Shared helpers for Figma image export URL retrieval."""

from __future__ import annotations

from figmaclaw.figma_client import FigmaClient

DEFAULT_IMAGE_BATCH_SIZE = 50


async def get_image_urls_batched(
    client: FigmaClient,
    file_key: str,
    node_ids: list[str],
    *,
    batch_size: int = DEFAULT_IMAGE_BATCH_SIZE,
    scale: float | None = None,
    format: str | None = None,
    fill_none_on_batch_error: bool = False,
) -> dict[str, str | None]:
    """Fetch image export URLs in fixed-size batches and merge results."""
    all_urls: dict[str, str | None] = {}
    for i in range(0, len(node_ids), batch_size):
        batch = node_ids[i : i + batch_size]
        try:
            if scale is not None and format is not None:
                urls = await client.get_image_urls(file_key, batch, scale=scale, format=format)
            elif scale is not None:
                urls = await client.get_image_urls(file_key, batch, scale=scale)
            elif format is not None:
                urls = await client.get_image_urls(file_key, batch, format=format)
            else:
                urls = await client.get_image_urls(file_key, batch)
        except Exception:
            if not fill_none_on_batch_error:
                raise
            urls = {nid: None for nid in batch}
        all_urls.update(urls)
    return all_urls
