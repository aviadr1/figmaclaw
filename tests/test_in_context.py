"""Tests for figmaclaw.in_context — composite context frame generation."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import pytest

from figmaclaw.figma_frontmatter import SectionNode
from figmaclaw.in_context import (
    _RASTER_FALLBACK_STEPS,
    SVG_SIZE_LIMIT,
    SectionData,
    fetch_section_data,
    make_context_calls,
)


def _make_section(
    node_id: str = "11:1",
    name: str = "Header",
    x: int = 0,
    y: int = 0,
    w: int = 393,
    h: int = 116,
) -> SectionNode:
    return SectionNode(node_id=node_id, name=name, x=x, y=y, w=w, h=h)


class TestFetchSectionData:
    """INVARIANT: fetch_section_data returns SVG when it fits, PNG otherwise."""

    @pytest.mark.asyncio
    async def test_returns_svg_when_under_limit(self) -> None:
        """INVARIANT: SVG data is used when compressed size ≤ SVG_SIZE_LIMIT."""
        small_svg = "<svg><rect/></svg>"
        assert len(small_svg) < SVG_SIZE_LIMIT

        mock_client = AsyncMock()
        mock_client.get_image_urls.return_value = {"11:1": "https://cdn/img.svg"}
        mock_client.download_url.return_value = small_svg.encode()

        section = _make_section()
        result = await fetch_section_data(mock_client, "file123", section)

        assert result.kind == "svg"
        assert result.data == small_svg
        assert result.section is section

    @pytest.mark.asyncio
    async def test_falls_back_to_png_when_svg_too_large(self) -> None:
        """INVARIANT: PNG fallback is used when SVG exceeds SVG_SIZE_LIMIT."""
        large_svg = "x" * (SVG_SIZE_LIMIT + 1)
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100  # fake PNG bytes

        mock_client = AsyncMock()
        mock_client.get_image_urls.side_effect = [
            {"11:1": "https://cdn/img.svg"},  # SVG call
            {"11:1": "https://cdn/img.png"},  # first PNG scale call
        ]
        mock_client.download_url.side_effect = [
            large_svg.encode(),  # SVG download
            png_bytes,  # PNG download (first scale fits)
        ]

        section = _make_section()
        result = await fetch_section_data(mock_client, "file123", section)

        assert result.kind == "png"
        assert result.data == base64.b64encode(png_bytes).decode("ascii")

    @pytest.mark.asyncio
    async def test_uses_first_fitting_raster_step(self) -> None:
        """INVARIANT: Uses the first raster step (format+scale) whose base64 fits."""
        large_svg = "x" * (SVG_SIZE_LIMIT + 1)
        # first raster step produces data too large, second fits
        large_img = b"\x89PNG" + b"\xff" * SVG_SIZE_LIMIT  # base64 will exceed limit
        small_img = b"\xff\xd8\xff" + b"\x00" * 50  # fake JPG bytes

        step0_fmt, step0_scale = _RASTER_FALLBACK_STEPS[0]
        step1_fmt, step1_scale = _RASTER_FALLBACK_STEPS[1]

        mock_client = AsyncMock()
        mock_client.get_image_urls.side_effect = [
            {"11:1": "https://cdn/img.svg"},
            {"11:1": f"https://cdn/img-step0.{step0_fmt}"},
            {"11:1": f"https://cdn/img-step1.{step1_fmt}"},
        ]
        mock_client.download_url.side_effect = [
            large_svg.encode(),
            large_img,
            small_img,
        ]

        section = _make_section()
        result = await fetch_section_data(mock_client, "file123", section)

        assert result.kind == step1_fmt
        assert result.data == base64.b64encode(small_img).decode("ascii")
        assert mock_client.get_image_urls.call_count == 3
        second_raster_call = mock_client.get_image_urls.call_args_list[2]
        assert second_raster_call.kwargs.get("scale") == step1_scale
        assert second_raster_call.kwargs.get("format") == step1_fmt

    @pytest.mark.asyncio
    async def test_falls_back_to_raster_when_svg_url_missing(self) -> None:
        """INVARIANT: Raster fallback is used when Figma returns no SVG URL."""
        img_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
        first_fmt = _RASTER_FALLBACK_STEPS[0][0]

        mock_client = AsyncMock()
        mock_client.get_image_urls.side_effect = [
            {"11:1": None},  # SVG → no URL
            {"11:1": "https://cdn/img.png"},  # first raster step
        ]
        mock_client.download_url.return_value = img_bytes

        section = _make_section()
        result = await fetch_section_data(mock_client, "file123", section)

        assert result.kind == first_fmt

    @pytest.mark.asyncio
    async def test_raises_when_all_raster_steps_too_large(self) -> None:
        """INVARIANT: ValueError is raised when no raster step fits within SVG_SIZE_LIMIT."""
        large_svg = "x" * (SVG_SIZE_LIMIT + 1)
        oversized_img = b"\x89PNG" + b"\xff" * SVG_SIZE_LIMIT  # base64 > SVG_SIZE_LIMIT

        mock_client = AsyncMock()
        mock_client.get_image_urls.side_effect = [
            {"11:1": "https://cdn/img.svg"},
        ] + [{"11:1": f"https://cdn/img-{fmt}-{scale}"} for fmt, scale in _RASTER_FALLBACK_STEPS]
        mock_client.download_url.side_effect = [
            large_svg.encode(),
        ] + [oversized_img] * len(_RASTER_FALLBACK_STEPS)

        section = _make_section()
        with pytest.raises(ValueError, match="No raster format"):
            await fetch_section_data(mock_client, "file123", section)

    @pytest.mark.asyncio
    async def test_raises_when_all_raster_urls_missing(self) -> None:
        """INVARIANT: ValueError is raised when Figma returns no URL for any raster step."""
        mock_client = AsyncMock()
        mock_client.get_image_urls.side_effect = [
            {"11:1": None},  # SVG → no URL
        ] + [{"11:1": None}] * len(_RASTER_FALLBACK_STEPS)

        section = _make_section()
        with pytest.raises(ValueError, match="No raster format"):
            await fetch_section_data(mock_client, "file123", section)


class TestMakeContextCalls:
    """INVARIANT: make_context_calls produces the correct call sequence."""

    def _calls(self, section_data_list: list[SectionData] | None = None) -> list[dict]:
        return make_context_calls(
            target_file_key="DRAFT_FILE",
            target_page_id="18:7",
            container_name="ctx-test",
            frame_w=393,
            frame_h=300,
            comp_x=80,
            comp_y=100,
            comp_w=200,
            label="Test Label",
            section_data_list=section_data_list or [],
        )

    def test_call_count_is_sections_plus_two(self) -> None:
        """INVARIANT: total calls = 1 (container) + N (sections) + 1 (caption)."""
        sections = [
            SectionData(section=_make_section("11:1", "A"), kind="png", data="abc"),
            SectionData(section=_make_section("11:2", "B"), kind="svg", data="<svg/>"),
        ]
        calls = self._calls(sections)
        assert len(calls) == 4  # container + 2 sections + caption

    def test_first_call_creates_container(self) -> None:
        """INVARIANT: First call creates the container frame with correct dimensions."""
        calls = self._calls()
        first = calls[0]
        assert first["file_key"] == "DRAFT_FILE"
        assert "createContextContainer" in first["code"]
        assert "393" in first["code"]
        assert "300" in first["code"]
        # x = comp_x + comp_w + 60 = 80 + 200 + 60 = 340
        assert "340" in first["code"]
        # y = comp_y = 100
        assert "100" in first["code"]

    def test_last_call_adds_caption(self) -> None:
        """INVARIANT: Last call adds a caption with the specified label text."""
        calls = self._calls()
        last = calls[-1]
        assert "addContextCaption" in last["code"]
        assert "Test Label" in last["code"]

    def test_section_calls_embed_data(self) -> None:
        """INVARIANT: Section calls embed the pre-fetched data in the code string."""
        svg_data = "<svg><rect width='100'/></svg>"
        png_data = base64.b64encode(b"fakepng").decode()
        sections = [
            SectionData(section=_make_section("11:1", "Header"), kind="svg", data=svg_data),
            SectionData(section=_make_section("11:2", "Body", y=116), kind="png", data=png_data),
        ]
        calls = self._calls(sections)
        svg_call = calls[1]
        png_call = calls[2]

        assert svg_data in svg_call["code"]
        assert "svg" in svg_call["code"]
        assert "Header" in svg_call["code"]

        assert png_data in png_call["code"]
        assert "png" in png_call["code"]
        assert "Body" in png_call["code"]

    def test_all_calls_target_correct_file(self) -> None:
        """INVARIANT: All calls reference the target file key."""
        sections = [SectionData(section=_make_section(), kind="png", data="x")]
        calls = self._calls(sections)
        assert all(c["file_key"] == "DRAFT_FILE" for c in calls)

    def test_all_calls_switch_to_target_page(self) -> None:
        """INVARIANT: All calls navigate to the target page before operating."""
        calls = self._calls()
        assert all("18:7" in c["code"] for c in calls)

    def test_section_positions_embedded(self) -> None:
        """INVARIANT: Section x/y/w/h from SectionNode are embedded in the call code."""
        section = _make_section("11:1", "Header", x=16, y=132, w=361, h=252)
        sd = SectionData(section=section, kind="png", data="abc")
        calls = make_context_calls(
            target_file_key="F",
            target_page_id="P",
            container_name="ctx",
            frame_w=393,
            frame_h=500,
            comp_x=80,
            comp_y=100,
            comp_w=200,
            label="",
            section_data_list=[sd],
        )
        section_call = calls[1]
        assert "x: 16" in section_call["code"]
        assert "y: 132" in section_call["code"]
        assert "w: 361" in section_call["code"]
        assert "h: 252" in section_call["code"]

    def test_svg_backticks_escaped(self) -> None:
        """INVARIANT: Backticks in SVG data are escaped to avoid breaking JS template literals."""
        svg_with_backtick = "<svg><!-- ` --></svg>"
        sd = SectionData(section=_make_section(), kind="svg", data=svg_with_backtick)
        calls = make_context_calls(
            target_file_key="F",
            target_page_id="P",
            container_name="ctx",
            frame_w=393,
            frame_h=500,
            comp_x=80,
            comp_y=100,
            comp_w=200,
            label="",
            section_data_list=[sd],
        )
        section_call = calls[1]
        assert "\\`" in section_call["code"]
