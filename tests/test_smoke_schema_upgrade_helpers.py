from __future__ import annotations

from pathlib import Path

import pytest

from tests.smoke.test_figma_api_smoke import (
    _body_from_rendered_markdown,
    _drop_instance_component_ids_from_frontmatter,
    _find_schema_upgrade_target,
    _has_populated_instance_component_ids,
)

_BODY = "# Page\n\n| Node ID | Name |\n|---|---|\n| 1:2 | Screen |\n"


def _md_with_sections(section_yaml: str) -> str:
    return f"""---
file_key: file123
page_node_id: '1:1'
frames:
- '1:2'
flows: []
enriched_schema_version: 0
frame_sections:
  '1:2':
{section_yaml}
---
{_BODY}"""


def test_schema_upgrade_target_requires_populated_instance_component_ids() -> None:
    missing_key = _md_with_sections(
        """  - node_id: '2:1'
    name: Empty
    x: 0
    y: 0
    w: 100
    h: 50
    instances: []
    raw_count: 0
"""
    )
    empty_ids = _md_with_sections(
        """  - node_id: '2:1'
    name: Empty
    x: 0
    y: 0
    w: 100
    h: 50
    instances: []
    instance_component_ids: []
    raw_count: 0
"""
    )
    populated_ids = _md_with_sections(
        """  - node_id: '2:1'
    name: Button
    x: 0
    y: 0
    w: 100
    h: 50
    instances: [Button]
    instance_component_ids: ['42:99']
    raw_count: 0
"""
    )

    assert not _has_populated_instance_component_ids(missing_key)
    assert not _has_populated_instance_component_ids(empty_ids)
    assert _has_populated_instance_component_ids(populated_ids)


def test_find_schema_upgrade_target_uses_pull_result_order(tmp_path: Path) -> None:
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    third = tmp_path / "third.md"
    first.write_text(
        _md_with_sections(
            """  - node_id: '2:1'
    name: Cover
    x: 0
    y: 0
    w: 100
    h: 50
    instances: []
    raw_count: 0
"""
        )
    )
    second.write_text(
        _md_with_sections(
            """  - node_id: '2:2'
    name: Button
    x: 0
    y: 0
    w: 100
    h: 50
    instances: [Button]
    instance_component_ids: ['42:99']
    raw_count: 0
"""
        )
    )
    third.write_text(
        _md_with_sections(
            """  - node_id: '2:3'
    name: Input
    x: 0
    y: 0
    w: 100
    h: 50
    instances: [Input]
    instance_component_ids: ['42:100']
    raw_count: 0
"""
        )
    )

    target = _find_schema_upgrade_target(tmp_path, ["first.md", "second.md", "third.md"])

    assert target == second


def test_drop_instance_component_ids_preserves_body_and_removes_nested_keys() -> None:
    md = _md_with_sections(
        """  - node_id: '2:1'
    name: Button
    x: 0
    y: 0
    w: 100
    h: 50
    instances: [Button]
    instance_component_ids: ['42:99']
    raw_count: 0
"""
    )

    mutated = _drop_instance_component_ids_from_frontmatter(md)

    assert _body_from_rendered_markdown(mutated) == _BODY
    assert "instance_component_ids" not in mutated
    assert not _has_populated_instance_component_ids(mutated)


def test_drop_instance_component_ids_fails_closed_when_target_is_not_exercisable() -> None:
    md = _md_with_sections(
        """  - node_id: '2:1'
    name: Cover
    x: 0
    y: 0
    w: 100
    h: 50
    instances: []
    raw_count: 0
"""
    )

    with pytest.raises(AssertionError, match="no instance_component_ids keys"):
        _drop_instance_component_ids_from_frontmatter(md)
