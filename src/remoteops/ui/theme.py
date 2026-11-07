"""Shared desktop console styling."""

APP_STYLESHEET = """
QWidget {
    font-family: "Segoe UI", "Noto Sans", sans-serif;
    font-size: 15px;
    color: #272c30;
}
QMainWindow, QDialog { background: #f6f7f8; }
QWidget#wmiQueryEditor, QWidget#inspectionPanel, QWidget#resultSurface, QWidget#argumentFields { background: #f6f7f8; }
QLabel { background: transparent; }
QLabel#brand { font-size: 19px; font-weight: 700; padding: 0 16px 0 4px; }
QLabel#sectionTitle { color: #686f76; font-size: 12px; font-weight: 600; }
QLabel#hostTitle { font-size: 20px; font-weight: 600; }
QLabel#muted { color: #747c83; }
QToolBar { background: #ffffff; border: 0; border-bottom: 1px solid #dfe3e5;
    spacing: 8px; padding: 6px 12px; }
QToolBar::separator { background: #dfe3e5; width: 1px; margin: 6px; }
QPushButton, QToolButton {
    background: #ffffff; border: 1px solid #d7dcdf; border-radius: 5px;
    padding: 6px 12px; min-height: 20px;
}
QPushButton:hover, QToolButton:hover { background: #eaf3f1; border-color: #95bab1; }
QPushButton:pressed, QToolButton:pressed { background: #d8ebe5; }
QPushButton:disabled, QToolButton:disabled { color: #9ba1a6; background: #edf0f1; border-color: #e2e5e7; }
QPushButton#primary, QToolButton#primary { background: #147d69; color: #ffffff; border-color: #147d69; font-weight: 600; }
QPushButton#primary:hover, QToolButton#primary:hover { background: #106854; }
QPushButton#primary:disabled, QToolButton#primary:disabled { background: #cbd9d5; color: #7c8e88; border-color: #cbd9d5; }
QToolButton { padding: 4px; }
QToolButton:checked { background: #d8ebe5; border-color: #70a897; }
QLineEdit, QComboBox, QPlainTextEdit, QSpinBox {
    background: #ffffff; border: 1px solid #d7dcdf; border-radius: 5px;
    padding: 7px; selection-background-color: #b8e0d5; selection-color: #182c25;
}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QSpinBox:focus { border-color: #218d76; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled { background: #eef0f1; color: #8a9298; }
QComboBox { padding-right: 24px; }
QComboBox::drop-down { border: 0; width: 22px; }
QComboBox QAbstractItemView { background: #ffffff; selection-background-color: #d8ebe5; }
QWidget#hostRail { background: #f0f2f3; }
QTableWidget, QTableView { background: #ffffff; alternate-background-color: #f2f6f7; border: 1px solid #dfe3e5; gridline-color: #edf0f1;
    selection-background-color: #d9ebe5; selection-color: #164e40; }
QListView { background: #ffffff; border: 1px solid #dfe3e5; selection-background-color: #d9ebe5; selection-color: #164e40; }
QListView::item { padding: 6px; }
QTableWidget#hostList { background: transparent; border: 0; outline: 0; }
QTreeWidget#hostList { background: transparent; border: 0; outline: 0; selection-background-color: #d9ebe5; selection-color: #164e40; }
QTreeWidget#hostList::item { padding: 3px; }
QTableWidget#hostList::item { border-bottom: 1px solid #e4e7e9; padding: 0; }
QTableWidget#hostList::item:selected { background: #d9ebe5; color: #164e40; }
QHeaderView::section { background: #e9eff1; border: 0; padding: 7px; color: #48585f; font-size: 13px; font-weight: 600; }
QTableView::item { padding: 5px; }
QSplitter::handle { background: #dfe3e5; width: 1px; }
QTabWidget::pane { border: 0; background: #f6f7f8; }
QTabBar::tab { background: #edf0f1; color: #6b747b; padding: 12px 18px;
    border: 0; border-right: 1px solid #dfe3e5; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { background: #f6f7f8; color: #1e5548; border-bottom: 2px solid #14816a; }
QTabBar::tab:hover { background: #e0e9e5; }
QTabBar::scroller { width: 26px; }
QTabBar#hostViews::tab { padding: 6px 12px; }
QTabBar#resultTabs::tab { padding: 6px 12px; }
QWidget#terminalSurface { background: #171b1e; border-radius: 6px; }
QWidget#terminalHeader { background: #202629; border-top-left-radius: 6px; border-top-right-radius: 6px; }
QWidget#terminalHeader QLabel { color: #adb9bf; font-size: 12px; }
QWidget#terminalHeader QToolButton { background: #e0e7e6; border: 0; }
QWidget#terminalHeader QToolButton:hover, QWidget#terminalHeader QToolButton:checked { background: #97ccb9; }
QTextEdit#terminal { background: #171b1e; color: #dce2e5; border: 0; padding: 12px; selection-background-color: #35584f; }
QWidget#commandBar { background: #202629; border-top: 1px solid #323b40; }
QLabel#prompt { color: #6bd4b1; font-family: "Cascadia Mono", "DejaVu Sans Mono", monospace; font-weight: 600; }
QLineEdit#commandInput { background: #202629; color: #e6ebed; border: 0; padding: 10px 4px;
    font-family: "Cascadia Mono", "DejaVu Sans Mono", monospace; font-size: 15px; }
QLineEdit#commandInput:disabled { color: #818d92; }
QWidget#sessionFooter QLabel { color: #717b82; font-size: 12px; }
QProgressBar { background: #d9e3df; border: 0; max-height: 3px; }
QProgressBar::chunk { background: #198b70; }
QScrollBar:vertical { background: #e4e8e9; width: 10px; margin: 0; }
QScrollBar:horizontal { background: #e4e8e9; height: 10px; margin: 0; }
QScrollBar::handle { background: #9da9ae; border-radius: 4px; min-height: 24px; min-width: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QStatusBar { background: #ffffff; border-top: 1px solid #dfe3e5; color: #717b82; font-size: 11px; }
QStatusBar::item { border: 0; }
QToolTip { background: #ffffff; color: #272c30; border: 1px solid #c6cfcc; padding: 5px; }
"""


DARK_OVERRIDES = """
QWidget { color: #e0e5e7; }
QMainWindow, QDialog { background: #202528; }
QWidget#wmiQueryEditor, QWidget#inspectionPanel, QWidget#resultSurface, QWidget#argumentFields { background: #202528; }
QToolBar, QStatusBar { background: #242b2e; border-color: #41494d; color: #b5bfc4; }
QLabel#sectionTitle, QLabel#muted { color: #a8b4ba; }
QPushButton, QToolButton { background: #30383c; color: #e0e5e7; border-color: #515c62; }
QPushButton:hover, QToolButton:hover { background: #3a4e47; border-color: #72b29e; }
QPushButton:disabled, QToolButton:disabled { background: #2b3135; color: #818d93; border-color: #41494d; }
QLineEdit, QComboBox, QPlainTextEdit, QTextEdit, QSpinBox { background: #191f22; color: #e0e5e7; border-color: #515c62; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled { background: #2b3135; color: #929da3; }
QComboBox QAbstractItemView, QMenu { background: #293135; color: #e0e5e7; selection-background-color: #36594c; }
QMenu::item:selected { background: #36594c; }
QWidget#hostRail { background: #252d31; }
QTableWidget, QTableView { background: #20272b; alternate-background-color: #293237; color: #e0e5e7; border-color: #41494d;
    gridline-color: #41494d; selection-background-color: #36594c; selection-color: #ecfff6; }
QListView { background: #20272b; color: #e0e5e7; border-color: #41494d; selection-background-color: #36594c; selection-color: #ecfff6; }
QTableWidget#hostList::item { border-bottom-color: #364046; }
QTableWidget#hostList::item:selected { background: #36594c; color: #ecfff6; }
QTreeWidget#hostList { color: #e0e5e7; selection-background-color: #36594c; selection-color: #ecfff6; }
QHeaderView::section { background: #2c353a; color: #b7c2c8; }
QTabWidget::pane { background: #202528; }
QTabBar::tab { background: #2a3237; color: #aab8bf; border-right-color: #41494d; }
QTabBar::tab:selected { background: #202528; color: #8fddbc; border-bottom-color: #65bd99; }
QTabBar::tab:hover { background: #364b42; }
QWidget#sessionFooter QLabel { color: #a5b2b9; }
QSplitter::handle { background: #41494d; }
QScrollBar { background: #242c30; }
QScrollBar::handle { background: #65767f; }
QToolTip { background: #293135; color: #eff7fa; border-color: #65767f; }
QPushButton#primary:disabled { background: #354b43; color: #9aacA5; border-color: #435b51; }
QWidget#terminalHeader QToolButton { background: #30383c; color: #e0e5e7; }
QWidget#terminalHeader QToolButton:hover, QWidget#terminalHeader QToolButton:checked { background: #3a4e47; }
"""


def stylesheet(dark=False):
    return APP_STYLESHEET + (DARK_OVERRIDES if dark else "")
