"""Skill 技能系统：扫描、SKILL.md 解析、按需加载。"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("kangclaw.skills")

# 系统内置 skills 目录
_BUILTIN_SKILLS_DIR = Path(__file__).parent


def _scan_skills_dir(skills_dir: Path, skills: list, seen: set):
    """扫描单个 skills 目录，提取摘要。seen 用于去重（用户 skill 优先）。"""
    if not skills_dir.exists():
        return

    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue

        name = skill_dir.name
        if name in seen:
            continue
        seen.add(name)

        try:
            text = skill_md.read_text(encoding="utf-8")
            lines = text.split("\n")

            # 跳过 YAML frontmatter（--- 到 --- 之间的内容）
            start = 0
            if lines and lines[0].strip() == "---":
                for i in range(1, len(lines)):
                    if lines[i].strip() == "---":
                        start = i + 1
                        break

            # 提取简述（第一个非空非标题行）
            description = ""
            for line in lines[start:]:
                line = line.strip()
                if line and not line.startswith("#") and not line.startswith("---"):
                    description = line
                    break

            # 确定 skill 文件路径（用于按需读取）
            skills.append({
                "name": name,
                "description": description,
                "path": str(skill_md),
            })
        except Exception as e:
            logger.warning(f"读取技能 {name} 失败: {e}")


def load_skills_summary(user_skills_dir: Path) -> str:
    """扫描系统内置 + 用户安装的 skills 目录，返回技能摘要文本。

    用户目录优先：如果同名 skill 在两处都存在，以用户目录为准。
    """
    skills: list[dict] = []
    seen: set[str] = set()

    # 用户 skills 优先扫描
    _scan_skills_dir(user_skills_dir, skills, seen)
    # 系统内置 skills
    _scan_skills_dir(_BUILTIN_SKILLS_DIR, skills, seen)

    if not skills:
        return ""
    lines = []
    for s in skills:
        desc = f" — {s['description']}" if s['description'] else ""
        # 统一使用相对路径，避免 agent 看到内置绝对路径后误将新 skill 写入系统目录
        rel_path = f"skills/{s['name']}/SKILL.md"
        lines.append(f"- {s['name']}{desc}（详情: `read_file {rel_path}`）")
    return "\n".join(lines)


def load_skill_detail(user_skills_dir: Path, skill_name: str) -> str | None:
    """加载指定技能的完整 SKILL.md 内容。用户目录优先。"""
    # 先查用户目录
    skill_md = user_skills_dir / skill_name / "SKILL.md"
    if skill_md.exists():
        return skill_md.read_text(encoding="utf-8")
    # 再查系统内置目录
    skill_md = _BUILTIN_SKILLS_DIR / skill_name / "SKILL.md"
    if skill_md.exists():
        return skill_md.read_text(encoding="utf-8")
    return None
