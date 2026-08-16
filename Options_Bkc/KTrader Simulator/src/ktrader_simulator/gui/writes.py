from __future__ import annotations

from collections.abc import Mapping

import dearpygui.dearpygui as dpg

ItemId = str | int
_MISSING = object()


class UiWriteCache:
    """Send only changed values, configuration, and themes to Dear PyGui."""

    def __init__(self) -> None:
        self._values: dict[ItemId, object] = {}
        self._configuration: dict[tuple[ItemId, str], object] = {}
        self._themes: dict[ItemId, ItemId] = {}
        self._write_count = 0

    @property
    def write_count(self) -> int:
        return self._write_count

    def set_value(self, item: ItemId, value: object) -> bool:
        if self._values.get(item, _MISSING) == value:
            return False
        dpg.set_value(item, value)
        self._values[item] = value
        self._write_count += 1
        return True

    def configure(self, item: ItemId, values: Mapping[str, object]) -> bool:
        changed = {
            key: value
            for key, value in values.items()
            if self._configuration.get((item, key), _MISSING) != value
        }
        if not changed:
            return False
        dpg.configure_item(item, **changed)
        for key, value in changed.items():
            self._configuration[(item, key)] = value
        self._write_count += 1
        return True

    def bind_theme(self, item: ItemId, theme: ItemId) -> bool:
        if self._themes.get(item) == theme:
            return False
        dpg.bind_item_theme(item, theme)
        self._themes[item] = theme
        self._write_count += 1
        return True

    def invalidate_value(self, item: ItemId) -> None:
        """Forget a value changed directly by user interaction."""

        self._values.pop(item, None)
