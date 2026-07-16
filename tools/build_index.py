#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
META_DIR = REPO_ROOT / "_meta"
OUTPUT_PATH = META_DIR / "config-index.json"

SOURCE_REPO = "github:azmiao/MiPushConfigurations"

INDEXED_SUBDIRS = (
    "icon",
)


@dataclass(frozen=True)
class ConfigFile:
    path: str
    name: str
    sha: str
    size: int
    updated_at: str


def git(*args: str) -> str:
    """
    在仓库根目录执行 Git 命令。

    Git 命令失败时输出完整错误信息，而不是只显示退出码 128。
    """
    command = [
        "git",
        "-C",
        str(REPO_ROOT),
        *args,
    ]

    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        command_text = " ".join(command)
        stderr = result.stderr.strip() or "(no stderr output)"
        stdout = result.stdout.strip()

        message = [
            f"Git command failed with exit code {result.returncode}:",
            command_text,
            "",
            stderr,
        ]

        if stdout:
            message.extend(
                [
                    "",
                    "stdout:",
                    stdout,
                ]
            )

        raise RuntimeError("\n".join(message))

    return result.stdout.strip()


def get_current_branch() -> str:
    """
    获取当前分支名称。

    优先使用工作流显式传入的 INDEX_BRANCH，然后依次兼容
    GitLab CI、GitHub Actions 和普通本地 Git 环境。
    """
    ci_ref = (
        os.environ.get("INDEX_BRANCH")
        or os.environ.get("CI_COMMIT_REF_NAME")
        or os.environ.get("CI_COMMIT_BRANCH")
        or os.environ.get("GITHUB_HEAD_REF")
        or os.environ.get("GITHUB_REF_NAME")
    )

    if ci_ref:
        return ci_ref

    branch = git(
        "rev-parse",
        "--abbrev-ref",
        "HEAD",
    )

    if branch and branch != "HEAD":
        return branch

    raise RuntimeError(
        "Unable to determine the current branch. "
        "Set INDEX_BRANCH explicitly."
    )


def get_last_updated_at(path: str) -> str:
    """
    获取文件最近一次 Git 提交的时间。

    如果文件还没有 Git 提交记录，则使用文件系统修改时间。
    """
    updated_at = git(
        "log",
        "-1",
        "--format=%cI",
        "--",
        path,
    )

    if updated_at:
        return updated_at

    timestamp = (REPO_ROOT / path).stat().st_mtime

    return datetime.fromtimestamp(
        timestamp,
        timezone.utc,
    ).isoformat()


def iter_json_paths() -> list[Path]:
    """
    查找需要加入索引的 JSON 配置文件。

    包括：
    - 仓库根目录下的 *.json
    - INDEXED_SUBDIRS 中的 *.json

    不会索引 _meta/config-index.json 本身。
    """
    files = list(REPO_ROOT.glob("*.json"))

    for directory_name in INDEXED_SUBDIRS:
        directory = REPO_ROOT / directory_name

        if directory.is_dir():
            files.extend(directory.glob("*.json"))

    return sorted(
        files,
        key=lambda file: file.relative_to(
            REPO_ROOT
        ).as_posix(),
    )


def iter_config_files() -> list[ConfigFile]:
    """
    读取所有配置文件并生成索引条目。
    """
    files: list[ConfigFile] = []

    for file in iter_json_paths():
        relative_path = file.relative_to(
            REPO_ROOT
        ).as_posix()

        raw_text = file.read_text(
            encoding="utf-8-sig"
        )

        # 解析 JSON，同时将内容标准化。
        # 格式、缩进和对象字段顺序不会影响 sha。
        parsed = json.loads(raw_text)

        canonical = json.dumps(
            parsed,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

        files.append(
            ConfigFile(
                path=relative_path,
                name=relative_path.removesuffix(".json"),
                sha=hashlib.sha256(
                    canonical.encode("utf-8")
                ).hexdigest(),
                # 使用磁盘上的实际文件大小。
                size=file.stat().st_size,
                updated_at=get_last_updated_at(
                    relative_path
                ),
            )
        )

    return files


def build_index() -> dict:
    """
    创建完整的配置索引对象。
    """
    return {
        "sourceRepo": SOURCE_REPO,
        "branch": get_current_branch(),
        "generatedAt": (
            datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        ),
        "files": [
            {
                "path": item.path,
                "name": item.name,
                "sha": item.sha,
                "size": item.size,
                "updatedAt": item.updated_at,
            }
            for item in iter_config_files()
        ],
    }


def comparable_index(index: dict) -> dict:
    """
    返回用于比较的索引。

    generatedAt 每次生成都会变化，因此检查时不比较这个字段。
    """
    comparable = dict(index)
    comparable.pop("generatedAt", None)
    return comparable


def check_index() -> None:
    """
    检查当前索引是否与仓库配置一致。
    """
    relative_output_path = OUTPUT_PATH.relative_to(
        REPO_ROOT
    )

    if not OUTPUT_PATH.exists():
        raise SystemExit(
            f"{relative_output_path} is missing; "
            "run tools/build_index.py"
        )

    try:
        current = json.loads(
            OUTPUT_PATH.read_text(
                encoding="utf-8-sig"
            )
        )
    except json.JSONDecodeError as error:
        raise SystemExit(
            f"{relative_output_path} is invalid JSON: "
            f"line {error.lineno}, "
            f"column {error.colno}: "
            f"{error.msg}"
        ) from error

    expected = build_index()

    if comparable_index(current) != comparable_index(
        expected
    ):
        raise SystemExit(
            f"{relative_output_path} is out of date; "
            "run tools/build_index.py"
        )

    print(f"{relative_output_path} is up to date")


def write_index() -> None:
    """
    创建 _meta 目录并写入索引。
    """
    META_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    index = build_index()

    OUTPUT_PATH.write_text(
        json.dumps(
            index,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(
        f"Generated "
        f"{OUTPUT_PATH.relative_to(REPO_ROOT)} "
        f"with {len(index['files'])} files"
    )


def main() -> None:
    if len(sys.argv) > 1:
        if sys.argv[1:] == ["--check"]:
            check_index()
            return

        raise SystemExit(
            "usage: tools/build_index.py [--check]"
        )

    write_index()


if __name__ == "__main__":
    main()