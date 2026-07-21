import json
from pathlib import Path

from scripts.split_raw_task_by_metadata import apply_split, build_split_plan


def _recording(root: Path, serial: str, robot: str, action_topics: list[str]) -> None:
    directory = root / serial
    directory.mkdir(parents=True)
    (directory / "metacard.json").write_text(
        json.dumps({
            "robot_type": robot,
            "fps": 30,
            "joint_names": ["j0"],
            "action_topics_map": {name: f"/{name}" for name in action_topics},
            "camera_topic_map": {"cam_head": "/cam/head"},
        }),
        encoding="utf-8",
    )


def test_split_plan_keeps_largest_group_and_apply_moves_only_other_groups(tmp_path):
    source = tmp_path / "cell007" / "task"
    _recording(source, "A", "robot-a", ["left", "right"])
    _recording(source, "B", "robot-a", ["left", "right"])
    _recording(source, "C", "robot-a", ["left", "right", "vacuum"])
    _recording(source, "D", "robot-b", ["left", "right"])

    plan = build_split_plan(source)

    assert [group["count"] for group in plan["groups"]] == [2, 1, 1]
    assert plan["groups"][0]["keep_in_source"] is True
    assert sorted(path.name for path in source.iterdir()) == ["A", "B", "C", "D"]

    assert apply_split(plan) == 2
    assert sorted(path.name for path in source.iterdir()) == ["A", "B"]
    moved_groups = [group for group in plan["groups"] if not group["keep_in_source"]]
    assert sorted(
        path.name
        for group in moved_groups
        for path in Path(group["destination"]).iterdir()
    ) == ["C", "D"]
