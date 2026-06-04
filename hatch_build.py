"""Hatchling build hook — compile Rust binary and bundle it into the wheel."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        if self.target_name != "wheel":
            return

        # Compile the Rust binary.
        result = subprocess.run(
            ["cargo", "build", "--release"],
            cwd=self.root,
        )
        if result.returncode != 0:
            print(
                "warning: cargo build --release failed — wheel will ship without "
                "the Rust binary and fall back to the Python implementation.",
                file=sys.stderr,
            )
            return

        bin_name = "advisor.exe" if platform.system() == "Windows" else "advisor"
        src = os.path.join(self.root, "target", "release", bin_name)
        if not os.path.isfile(src):
            print(
                f"warning: expected Rust binary at {src} but not found — "
                "wheel will fall back to Python implementation.",
                file=sys.stderr,
            )
            return

        dst_dir = os.path.join(self.root, "advisor", "_bin")
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, bin_name)
        shutil.copy2(src, dst)
        os.chmod(dst, 0o755)

        # Tell hatchling to include this file in the wheel.
        build_data.setdefault("force_include", {})[dst] = f"advisor/_bin/{bin_name}"
