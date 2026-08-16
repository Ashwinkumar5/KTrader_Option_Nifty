from __future__ import annotations

from pathlib import Path

import dearpygui.dearpygui as dpg

from ktrader_simulator.gui import tags


def create_themes() -> None:
    font_path = Path("C:/Windows/Fonts/consola.ttf")
    bold_font_path = Path("C:/Windows/Fonts/consolab.ttf")
    if font_path.is_file():
        with dpg.font_registry():
            dpg.add_font(str(font_path), 17, tag=tags.ANALYTICS_FONT)
            if bold_font_path.is_file():
                dpg.add_font(str(bold_font_path), 17, tag=tags.ANALYTICS_HEADER_FONT)
                dpg.add_font(str(bold_font_path), 26, tag=tags.DASHBOARD_TITLE_FONT)
                dpg.add_font(str(bold_font_path), 19, tag=tags.CARD_TITLE_FONT)
                dpg.add_font(str(bold_font_path), 17, tag=tags.HEADER_DETAIL_FONT)
                dpg.add_font(str(bold_font_path), 20, tag=tags.SUMMARY_VALUE_FONT)
                dpg.add_font(str(bold_font_path), 16, tag=tags.SUMMARY_DETAIL_FONT)
                dpg.add_font(str(bold_font_path), 30, tag=tags.INDIA_VIX_VALUE_FONT)
    with dpg.theme(tag=tags.GLOBAL_THEME), dpg.theme_component(dpg.mvAll):
        dpg.add_theme_color(dpg.mvThemeCol_Text, (232, 237, 244, 255))
        dpg.add_theme_color(dpg.mvThemeCol_WindowBg, (35, 35, 35, 255))
        dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (35, 35, 35, 255))
        dpg.add_theme_color(dpg.mvThemeCol_PopupBg, (35, 35, 35, 255))
        dpg.add_theme_color(dpg.mvThemeCol_Border, (75, 75, 75, 255))
        dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (55, 55, 60, 255))
        dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, (70, 70, 76, 255))
        dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, (82, 82, 88, 255))
        dpg.add_theme_color(dpg.mvThemeCol_Header, (55, 55, 60, 255))
        dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, (70, 70, 76, 255))
        dpg.add_theme_color(dpg.mvThemeCol_HeaderActive, (82, 82, 88, 255))
        dpg.add_theme_color(dpg.mvThemeCol_TableHeaderBg, (45, 45, 49, 255))
        dpg.add_theme_color(dpg.mvThemeCol_TableRowBg, (40, 42, 46, 255))
        dpg.add_theme_color(dpg.mvThemeCol_TableRowBgAlt, (31, 34, 38, 255))
        dpg.add_theme_color(dpg.mvThemeCol_TableBorderStrong, (85, 85, 90, 255))
        dpg.add_theme_color(dpg.mvThemeCol_TableBorderLight, (65, 65, 70, 255))
        dpg.add_theme_color(dpg.mvThemeCol_TextSelectedBg, (65, 78, 105, 255))
        dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 7, 7)
        dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 4, 3)
        dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 7, 5)
        dpg.add_theme_style(dpg.mvStyleVar_CellPadding, 5, 2)
        dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 0)
        dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, 0)

    _button_theme(tags.BUY_CALL_THEME, (25, 142, 35, 255), (33, 165, 44, 255))
    _button_theme(tags.BUY_PUT_THEME, (200, 30, 36, 255), (224, 40, 46, 255))
    _button_theme(tags.EXIT_THEME, (205, 84, 0, 255), (230, 100, 0, 255))
    _selectable_theme(
        tags.STRIKE_DEFAULT_THEME,
        text=(235, 235, 235, 255),
        normal=(55, 55, 60, 255),
        hovered=(70, 70, 76, 255),
    )
    _selectable_theme(
        tags.SELECTED_STRIKE_THEME,
        text=(38, 28, 18, 255),
        normal=(255, 185, 110, 255),
        hovered=(255, 202, 145, 255),
    )

    _text_theme(tags.CYAN_TEXT_THEME, (90, 200, 255, 255))
    _text_theme(tags.GREEN_TEXT_THEME, (50, 255, 70, 255))
    _text_theme(tags.RED_TEXT_THEME, (255, 125, 125, 255))
    _text_theme(tags.BLUE_TEXT_THEME, (105, 175, 255, 255))
    _text_theme(tags.YELLOW_TEXT_THEME, (255, 235, 0, 255))
    _text_theme(tags.ORANGE_TEXT_THEME, (255, 145, 75, 255))
    _text_theme(tags.MUTED_TEXT_THEME, (195, 202, 214, 255))
    _text_theme(tags.DARK_TEXT_THEME, (28, 28, 28, 255))
    _child_theme(tags.CARD_THEME, (31, 37, 45, 255), (91, 105, 122, 255))
    _child_theme(tags.INDIA_VIX_CARD_THEME, (22, 39, 48, 255), (62, 196, 216, 255))
    _child_theme(tags.CALL_CARD_THEME, (24, 57, 39, 255), (62, 175, 97, 255))
    _child_theme(tags.PUT_CARD_THEME, (57, 29, 37, 255), (205, 73, 86, 255))
    with dpg.theme(tag=tags.INDEX_COMBO_THEME), dpg.theme_component(dpg.mvCombo):
        dpg.add_theme_color(dpg.mvThemeCol_Text, (232, 237, 244, 255))
        dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (55, 55, 60, 255))
        dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, (68, 91, 111, 255))
        dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, (73, 105, 130, 255))
        dpg.add_theme_color(dpg.mvThemeCol_Border, (105, 175, 255, 255))
    with dpg.theme(tag=tags.PRICE_MODE_RADIO_THEME), dpg.theme_component(
        dpg.mvRadioButton
    ):
        dpg.add_theme_color(dpg.mvThemeCol_CheckMark, (255, 235, 0, 255))
        dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (55, 55, 60, 255))
        dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, (75, 75, 80, 255))
        dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, (85, 85, 90, 255))


def _button_theme(
    tag: str,
    normal: tuple[int, int, int, int],
    hovered: tuple[int, int, int, int],
) -> None:
    with dpg.theme(tag=tag), dpg.theme_component(dpg.mvButton):
        dpg.add_theme_color(dpg.mvThemeCol_Button, normal)
        dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, hovered)
        dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, hovered)
        dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 0)


def _text_theme(tag: str, color: tuple[int, int, int, int]) -> None:
    with dpg.theme(tag=tag), dpg.theme_component(dpg.mvText):
        dpg.add_theme_color(dpg.mvThemeCol_Text, color)


def _child_theme(
    tag: str,
    background: tuple[int, int, int, int],
    border: tuple[int, int, int, int],
) -> None:
    with dpg.theme(tag=tag), dpg.theme_component(dpg.mvChildWindow):
        dpg.add_theme_color(dpg.mvThemeCol_ChildBg, background)
        dpg.add_theme_color(dpg.mvThemeCol_Border, border)


def _selectable_theme(
    tag: str,
    *,
    text: tuple[int, int, int, int],
    normal: tuple[int, int, int, int],
    hovered: tuple[int, int, int, int],
) -> None:
    with dpg.theme(tag=tag), dpg.theme_component(dpg.mvSelectable):
        dpg.add_theme_color(dpg.mvThemeCol_Text, text)
        dpg.add_theme_color(dpg.mvThemeCol_Header, normal)
        dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, hovered)
        dpg.add_theme_color(dpg.mvThemeCol_HeaderActive, hovered)


def bind_analytics_font(item: str | int) -> None:
    if dpg.does_item_exist(tags.ANALYTICS_FONT):
        dpg.bind_item_font(item, tags.ANALYTICS_FONT)


def bind_analytics_header_font(item: str | int) -> None:
    if dpg.does_item_exist(tags.ANALYTICS_HEADER_FONT):
        dpg.bind_item_font(item, tags.ANALYTICS_HEADER_FONT)
    else:
        bind_analytics_font(item)


def bind_dashboard_title_font(item: str | int) -> None:
    if dpg.does_item_exist(tags.DASHBOARD_TITLE_FONT):
        dpg.bind_item_font(item, tags.DASHBOARD_TITLE_FONT)
    else:
        bind_analytics_header_font(item)


def bind_card_title_font(item: str | int) -> None:
    if dpg.does_item_exist(tags.CARD_TITLE_FONT):
        dpg.bind_item_font(item, tags.CARD_TITLE_FONT)
    else:
        bind_analytics_header_font(item)


def bind_header_detail_font(item: str | int) -> None:
    if dpg.does_item_exist(tags.HEADER_DETAIL_FONT):
        dpg.bind_item_font(item, tags.HEADER_DETAIL_FONT)
    else:
        bind_card_title_font(item)


def bind_summary_value_font(item: str | int) -> None:
    if dpg.does_item_exist(tags.SUMMARY_VALUE_FONT):
        dpg.bind_item_font(item, tags.SUMMARY_VALUE_FONT)
    else:
        bind_card_title_font(item)


def bind_summary_detail_font(item: str | int) -> None:
    if dpg.does_item_exist(tags.SUMMARY_DETAIL_FONT):
        dpg.bind_item_font(item, tags.SUMMARY_DETAIL_FONT)
    else:
        bind_analytics_header_font(item)


def bind_india_vix_value_font(item: str | int) -> None:
    if dpg.does_item_exist(tags.INDIA_VIX_VALUE_FONT):
        dpg.bind_item_font(item, tags.INDIA_VIX_VALUE_FONT)
    else:
        bind_dashboard_title_font(item)
