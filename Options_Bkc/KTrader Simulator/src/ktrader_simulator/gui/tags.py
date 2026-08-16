MAIN_WINDOW = "ktrader::main_window"
LEFT_PANEL = "ktrader::left_panel"
DATA_PANEL = "ktrader::data_panel"
RIGHT_PANEL = "ktrader::right_panel"
PORTFOLIO_PANEL = "ktrader::portfolio_panel"
HEADER_PANEL = "ktrader::header_panel"
INDIA_VIX_CARD = "ktrader::india_vix_card"
INDIA_VIX_VALUE = "ktrader::india_vix_value"
INDIA_VIX_STATUS = "ktrader::india_vix_status"
NIFTY_CARD = "ktrader::nifty_card"
NIFTY_VALUE = "ktrader::nifty_value"
NIFTY_STATUS = "ktrader::nifty_status"
ACCOUNT_CARD = "ktrader::account_card"
PNL_CARD = "ktrader::pnl_card"
METRICS_CARD = "ktrader::metrics_card"
ORDER_CARD = "ktrader::order_card"
RISK_CARD = "ktrader::risk_card"
CALL_ORDER_CARD = "ktrader::call_order_card"
PUT_ORDER_CARD = "ktrader::put_order_card"

INDEX_COMBO = "ktrader::index_combo"
REFRESH_BUTTON = "ktrader::refresh_button"
OPTION_CHAIN_TABLE = "ktrader::option_chain_table"
ANALYTICS_TABLE = "ktrader::analytics_table"
ANALYTICS_SUMMARY_TABLE = "ktrader::analytics_summary_table"
ANALYTICS_SUMMARY_VALUES = (
    "ktrader::analytics_summary::oi_pcr",
    "ktrader::analytics_summary::volume_pcr",
    "ktrader::analytics_summary::put_volume_oi",
    "ktrader::analytics_summary::call_volume_oi",
)
ANALYTICS_SUMMARY_STATUSES = (
    "ktrader::analytics_summary::oi_pcr_status",
    "ktrader::analytics_summary::volume_pcr_status",
    "ktrader::analytics_summary::put_volume_oi_status",
    "ktrader::analytics_summary::call_volume_oi_status",
)
CONNECTED_BROKER = "ktrader::connected_broker"
CONNECTION_MODE = "ktrader::connection_mode"
ORDER_MODE = "ktrader::order_mode"
ACCOUNT_BALANCE = "ktrader::account_balance"
RESERVED_BALANCE = "ktrader::reserved_balance"
FUNDS_STATUS = "ktrader::funds_status"
UNDERLYING_PRICE = "ktrader::underlying_price"
SELECTED_STRIKE = "ktrader::selected_strike"
SELECTED_CALL_LTP = "ktrader::selected_call_ltp"
SELECTED_PUT_LTP = "ktrader::selected_put_ltp"

BUY_CALL_BUTTON = "ktrader::buy_call"
BUY_PUT_BUTTON = "ktrader::buy_put"
LOTS_INPUT = "ktrader::lots"
ORDER_TOTAL = "ktrader::order_total"
CALL_LOTS_INPUT = LOTS_INPUT
PUT_LOTS_INPUT = "ktrader::put_lots"
CALL_ORDER_TOTAL = ORDER_TOTAL
PUT_ORDER_TOTAL = "ktrader::put_order_total"
PRICE_MODE_RADIO = "ktrader::price_mode"
CALL_LIMIT_PRICE_INPUT = "ktrader::limit_price"
PUT_LIMIT_PRICE_INPUT = "ktrader::put_limit_price"
ORDER_STATUS = "ktrader::order_status"
ORDER_TYPE_COMBO = "ktrader::order_type"
LIMIT_PRICE_INPUT = CALL_LIMIT_PRICE_INPUT
TARGET_PERCENT_INPUT = "ktrader::target_percent"
STOP_LOSS_PERCENT_INPUT = "ktrader::stop_loss_percent"
TRAILING_SL_PERCENT_INPUT = "ktrader::trailing_sl_percent"

CONSOLIDATED_PNL = "ktrader::consolidated_pnl"
SUMMARY_CONSOLIDATED_PNL = "ktrader::summary_consolidated_pnl"
PORTFOLIO_TABLE = "ktrader::portfolio_table"
CLOSED_POSITIONS_TABLE = "ktrader::closed_positions_table"
AMOUNT_INVESTED = "ktrader::amount_invested"
CURRENT_AMOUNT = "ktrader::current_amount"
PORTFOLIO_PLACEHOLDER_ROW = "ktrader::portfolio_placeholder_row"
PORTFOLIO_PLACEHOLDER_PNL = "ktrader::portfolio_placeholder_pnl"
PORTFOLIO_PLACEHOLDER_PNL_PERCENT = "ktrader::portfolio_placeholder_pnl_percent"
PORTFOLIO_PLACEHOLDER_EXIT = "ktrader::portfolio_placeholder_exit"

OPTION_ROW_COUNT = 5
OPTION_ROW_TAGS = tuple(f"ktrader::option_row::{index}" for index in range(OPTION_ROW_COUNT))
CALL_BID_TAGS = tuple(f"{row}::call_bid" for row in OPTION_ROW_TAGS)
CALL_ASK_TAGS = tuple(f"{row}::call_ask" for row in OPTION_ROW_TAGS)
STRIKE_TAGS = tuple(f"{row}::strike" for row in OPTION_ROW_TAGS)
PUT_BID_TAGS = tuple(f"{row}::put_bid" for row in OPTION_ROW_TAGS)
PUT_ASK_TAGS = tuple(f"{row}::put_ask" for row in OPTION_ROW_TAGS)
ANALYTICS_ROW_TAGS = tuple(f"ktrader::analytics_row::{index}" for index in range(OPTION_ROW_COUNT))
ANALYTICS_CELL_TAGS = tuple(
    tuple(f"{row}::cell::{column}" for column in range(10)) for row in ANALYTICS_ROW_TAGS
)

GLOBAL_THEME = "ktrader::theme::global"
BUY_CALL_THEME = "ktrader::theme::buy_call"
BUY_PUT_THEME = "ktrader::theme::buy_put"
EXIT_THEME = "ktrader::theme::exit"
STRIKE_DEFAULT_THEME = "ktrader::theme::strike_default"
SELECTED_STRIKE_THEME = "ktrader::theme::selected_strike"
CYAN_TEXT_THEME = "ktrader::theme::cyan_text"
GREEN_TEXT_THEME = "ktrader::theme::green_text"
RED_TEXT_THEME = "ktrader::theme::red_text"
BLUE_TEXT_THEME = "ktrader::theme::blue_text"
YELLOW_TEXT_THEME = "ktrader::theme::yellow_text"
ORANGE_TEXT_THEME = "ktrader::theme::orange_text"
MUTED_TEXT_THEME = "ktrader::theme::muted_text"
DARK_TEXT_THEME = "ktrader::theme::dark_text"
ANALYTICS_FONT = "ktrader::font::analytics"
ANALYTICS_HEADER_FONT = "ktrader::font::analytics_header"
DASHBOARD_TITLE_FONT = "ktrader::font::dashboard_title"
CARD_TITLE_FONT = "ktrader::font::card_title"
HEADER_DETAIL_FONT = "ktrader::font::header_detail"
SUMMARY_VALUE_FONT = "ktrader::font::summary_value"
SUMMARY_DETAIL_FONT = "ktrader::font::summary_detail"
INDIA_VIX_VALUE_FONT = "ktrader::font::india_vix_value"
CARD_THEME = "ktrader::theme::card"
INDIA_VIX_CARD_THEME = "ktrader::theme::india_vix_card"
CALL_CARD_THEME = "ktrader::theme::call_card"
PUT_CARD_THEME = "ktrader::theme::put_card"
INDEX_COMBO_THEME = "ktrader::theme::index_combo"
PRICE_MODE_RADIO_THEME = "ktrader::theme::price_mode_radio"

REQUIRED_LAYOUT_TAGS = (
    MAIN_WINDOW,
    LEFT_PANEL,
    DATA_PANEL,
    RIGHT_PANEL,
    PORTFOLIO_PANEL,
    INDEX_COMBO,
    OPTION_CHAIN_TABLE,
    ANALYTICS_TABLE,
    ANALYTICS_SUMMARY_TABLE,
    *ANALYTICS_SUMMARY_STATUSES,
    CONNECTED_BROKER,
    CONNECTION_MODE,
    INDIA_VIX_CARD,
    INDIA_VIX_VALUE,
    INDIA_VIX_STATUS,
    NIFTY_CARD,
    NIFTY_VALUE,
    NIFTY_STATUS,
    ORDER_MODE,
    ACCOUNT_BALANCE,
    RESERVED_BALANCE,
    FUNDS_STATUS,
    UNDERLYING_PRICE,
    SELECTED_STRIKE,
    SELECTED_CALL_LTP,
    SELECTED_PUT_LTP,
    BUY_CALL_BUTTON,
    BUY_PUT_BUTTON,
    LOTS_INPUT,
    CALL_LOTS_INPUT,
    PUT_LOTS_INPUT,
    ORDER_TOTAL,
    ORDER_STATUS,
    ORDER_TYPE_COMBO,
    LIMIT_PRICE_INPUT,
    TARGET_PERCENT_INPUT,
    STOP_LOSS_PERCENT_INPUT,
    TRAILING_SL_PERCENT_INPUT,
    CONSOLIDATED_PNL,
    SUMMARY_CONSOLIDATED_PNL,
    PORTFOLIO_TABLE,
    CLOSED_POSITIONS_TABLE,
)
