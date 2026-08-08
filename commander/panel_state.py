from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path


logger = logging.getLogger("commander.panel_state")


@dataclass(frozen=True)
class CommanderPanelLocation:
    channel_id: int
    message_id: int


class CommanderPanelStore:
    """Persist the single Commander panel location without storing any secrets."""

    def __init__(self, state_file: Path | None = None) -> None:
        configured_path = os.getenv("COMMANDER_PANEL_STATE_FILE", "/data/commander-panel.json")
        self._state_file = state_file or Path(configured_path)

    def load(self) -> CommanderPanelLocation | None:
        try:
            payload = json.loads(self._state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as error:
            logger.warning(
                "Could not read stored Commander panel state from %s: %s",
                self._state_file,
                error,
            )
            return None

        if not isinstance(payload, dict):
            logger.warning("Stored Commander panel state in %s is invalid", self._state_file)
            return None

        channel_id = payload.get("channel_id")
        message_id = payload.get("message_id")
        if not self._is_snowflake(channel_id) or not self._is_snowflake(message_id):
            logger.warning("Stored Commander panel IDs in %s are invalid", self._state_file)
            return None

        return CommanderPanelLocation(channel_id=channel_id, message_id=message_id)

    def save(self, location: CommanderPanelLocation) -> None:
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary_file = self._state_file.with_suffix(f"{self._state_file.suffix}.tmp")
        temporary_file.write_text(
            json.dumps(
                {"channel_id": location.channel_id, "message_id": location.message_id},
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary_file.replace(self._state_file)

    def clear(self) -> None:
        try:
            self._state_file.unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def _is_snowflake(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value > 0
