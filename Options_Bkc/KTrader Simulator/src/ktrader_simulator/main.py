from __future__ import annotations

from time import monotonic

import dearpygui.dearpygui as dpg

from ktrader_simulator.config import ConfigurationError, Settings, load_settings
from ktrader_simulator.controller import TradingController
from ktrader_simulator.gui import tags
from ktrader_simulator.gui.dashboard import DashboardBindings
from ktrader_simulator.gui.layout import build_layout, resize_layout


def run(settings: Settings) -> None:
    controller = TradingController(settings)
    dpg.create_context()
    try:
        build_layout(settings)
        bindings = DashboardBindings(settings=settings, controller=controller)
        bindings.bind_callbacks()
        dpg.create_viewport(
            title=settings.app_title,
            width=settings.viewport_width,
            height=settings.viewport_height,
            resizable=settings.viewport_resizable,
            vsync=settings.viewport_vsync,
        )
        dpg.setup_dearpygui()
        dpg.show_viewport()
        if settings.viewport_start_maximized:
            dpg.maximize_viewport()
        dpg.set_viewport_resize_callback(
            lambda _sender, app_data: resize_layout(
                settings,
                int(app_data[0]),
                int(app_data[1]),
            )
        )
        dpg.set_primary_window(tags.MAIN_WINDOW, True)
        if settings.auto_connect:
            controller.start()

        next_ui_update = monotonic()
        while dpg.is_dearpygui_running():
            now = monotonic()
            if now >= next_ui_update:
                bindings.apply_events(controller.drain_events())
                next_ui_update = now + settings.frame_interval_seconds
            dpg.render_dearpygui_frame()
    finally:
        controller.stop()
        dpg.destroy_context()


def main() -> None:
    try:
        settings = load_settings()
    except ConfigurationError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc
    run(settings)


if __name__ == "__main__":
    main()
