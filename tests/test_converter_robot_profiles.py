"""Tests for converter robot profile files."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = REPO_ROOT / "conversion_configs" / "robots"
SUBMODULE_PROFILE_DIR = REPO_ROOT / "rosbag2lerobot-svt" / "configs" / "robots"
EXTERNAL_CONVERSION_DIR = REPO_ROOT / "rosbag2lerobot-svt" / "conversion"
SUPPORTED_PROFILE_NAMES = {
    "RBY1_M_v1.2",
    "ffw_bg2_follower",
    "ffw_bg2_rev4",
    "ffw_sg2_follower",
    "rby1a",
}


def _load(name: str) -> dict:
    return yaml.safe_load((PROFILE_DIR / f"{name}.yaml").read_text(encoding="utf-8"))


def _ordered_joints(profile: dict) -> list[str]:
    joints: list[str] = []
    excluded = set(profile["joint_filter"]["exclude"])
    for arm in profile["arms"]:
        for joint in arm["arm_joints"] + arm.get("gripper_joints", []):
            if joint not in excluded:
                joints.append(joint)
    return joints


def test_runtime_profiles_match_the_submodule_source_of_truth():
    runtime_profiles = {
        path.stem
        for path in PROFILE_DIR.glob("*.yaml")
    }

    assert runtime_profiles == SUPPORTED_PROFILE_NAMES
    for profile_name in sorted(SUPPORTED_PROFILE_NAMES):
        assert _load(profile_name) == yaml.safe_load(
            (
                SUBMODULE_PROFILE_DIR / f"{profile_name}.yaml"
            ).read_text(encoding="utf-8")
        )


def _production_assembled_joint_order_for_synthetic_profile() -> list[str]:
    spec = importlib.util.spec_from_file_location(
        "conversion.robot_profile",
        EXTERNAL_CONVERSION_DIR / "robot_profile.py",
    )
    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    profile = module.RobotProfile(
        robot_type="synthetic",
        arms=[
            module.ArmConfig(
                side="left",
                arm_joints=["left_keep_1", "head_joint1", "left_keep_2"],
                gripper_joints=["lift_joint", "left_keep_3"],
            ),
            module.ArmConfig(
                side="right",
                arm_joints=["right_keep_1", "right_keep_2"],
                gripper_joints=[],
            ),
        ],
        joint_filter=module.JointFilter(exclude=["head_joint1", "lift_joint"]),
    )
    return module.assemble_joint_order(profile)


def test_rby1a_profile_matches_approved_joint_order():
    profile = _load("rby1a")

    assert profile["robot_type"] == "rby1a"
    assert [arm["side"] for arm in profile["arms"]] == ["left", "right"]
    assert _ordered_joints(profile) == [
        "left_arm_0",
        "left_arm_1",
        "left_arm_2",
        "left_arm_3",
        "left_arm_4",
        "left_arm_5",
        "left_arm_6",
        "left_trigger",
        "right_arm_0",
        "right_arm_1",
        "right_arm_2",
        "right_arm_3",
        "right_arm_4",
        "right_arm_5",
        "right_arm_6",
        "right_trigger",
    ]


def test_ffw_profile_matches_approved_joint_order_and_exclusions():
    profile = _load("ffw_bg2_rev4")

    assert profile["robot_type"] == "ffw_bg2_rev4"
    assert [arm["side"] for arm in profile["arms"]] == ["left", "right"]
    assert profile["joint_filter"]["exclude"] == [
        "head_joint1",
        "head_joint2",
        "lift_joint",
    ]
    assert _ordered_joints(profile) == [
        "arm_l_joint1",
        "arm_l_joint2",
        "arm_l_joint3",
        "arm_l_joint4",
        "arm_l_joint5",
        "arm_l_joint6",
        "arm_l_joint7",
        "gripper_l_joint1",
        "arm_r_joint1",
        "arm_r_joint2",
        "arm_r_joint3",
        "arm_r_joint4",
        "arm_r_joint5",
        "arm_r_joint6",
        "arm_r_joint7",
        "gripper_r_joint1",
    ]


def test_production_assemble_joint_order_excludes_filtered_joints():
    ordered_joints = _production_assembled_joint_order_for_synthetic_profile()

    assert ordered_joints == [
        "left_keep_1",
        "left_keep_2",
        "left_keep_3",
        "right_keep_1",
        "right_keep_2",
    ]
    assert "head_joint1" not in ordered_joints
    assert "lift_joint" not in ordered_joints
