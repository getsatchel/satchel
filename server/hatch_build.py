"""Bundle the built curator UI (../ui/dist) into the wheel when it exists."""

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):  # type: ignore[type-arg]
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        dist = Path(self.root).parent / "ui" / "dist"
        if (dist / "index.html").is_file():
            build_data["force_include"][str(dist)] = "satchel/resources/ui"
