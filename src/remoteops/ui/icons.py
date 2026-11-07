"""Consistent library icons with explicit contrast in both application themes."""
from PySide6.QtCore import QEvent, QObject, QSize
from PySide6.QtWidgets import QAbstractButton, QStyle
from importlib.resources import files
from qtawesome.iconic_font import IconicFont


class _IconThemeFilter(QObject):
    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.Show, QEvent.Type.PaletteChange, QEvent.Type.StyleChange):
            if name := obj.property("remoteopsIcon"):
                set_button_icon(obj, name)
        return False


_theme_filter = None
_font = None


NAMES = {
    QStyle.StandardPixmap.SP_FileIcon: "fa5s.copy",
    QStyle.StandardPixmap.SP_DialogSaveButton: "fa5s.save",
    QStyle.StandardPixmap.SP_TrashIcon: "fa5s.trash-alt",
    QStyle.StandardPixmap.SP_BrowserReload: "fa5s.sync-alt",
    QStyle.StandardPixmap.SP_ArrowUp: "fa5s.chevron-up",
    QStyle.StandardPixmap.SP_ArrowDown: "fa5s.chevron-down",
    QStyle.StandardPixmap.SP_ArrowLeft: "fa5s.chevron-left",
    QStyle.StandardPixmap.SP_ArrowRight: "fa5s.chevron-right",
    QStyle.StandardPixmap.SP_MediaPlay: "fa5s.play",
    QStyle.StandardPixmap.SP_MediaStop: "fa5s.stop",
    QStyle.StandardPixmap.SP_DirIcon: "fa5s.exchange-alt",
    QStyle.StandardPixmap.SP_DirHomeIcon: "fa5s.key",
    QStyle.StandardPixmap.SP_FileDialogDetailedView: "fa5s.sliders-h",
    QStyle.StandardPixmap.SP_FileDialogContentsView: "fa5s.edit",
    QStyle.StandardPixmap.SP_FileDialogListView: "fa5s.align-left",
    QStyle.StandardPixmap.SP_FileDialogNewFolder: "fa5s.plus",
    QStyle.StandardPixmap.SP_ComputerIcon: "fa5s.server",
    QStyle.StandardPixmap.SP_DesktopIcon: "fa5s.adjust",
    QStyle.StandardPixmap.SP_MessageBoxWarning: "fa5s.exclamation-triangle",
    QStyle.StandardPixmap.SP_DialogCancelButton: "fa5s.times",
    QStyle.StandardPixmap.SP_DialogOpenButton: "fa5s.folder-open",
    QStyle.StandardPixmap.SP_DialogApplyButton: "fa5s.check",
}


def set_button_icon(button, icon):
    global _theme_filter, _font
    if _theme_filter is None:
        _theme_filter = _IconThemeFilter()
    if _font is None:
        # Load only the packaged font into Qt, without installing OS-wide fonts.
        _font = IconicFont(("fa5s", "fontawesome5-solid-webfont-5.15.4.ttf",
                          "fontawesome5-solid-webfont-charmap-5.15.4.json",
                          str(files("qtawesome") / "fonts")))
    if not button.property("remoteopsIcon"):
        button.installEventFilter(_theme_filter)
    name = icon if isinstance(icon, str) else NAMES[icon]
    button.setProperty("remoteopsIcon", name)
    parent = button
    dark = False
    while parent is not None:
        if hasattr(parent, "dark_mode"):
            dark = parent.dark_mode
            break
        parent = parent.parentWidget()
    color = "#f4f8f9" if dark or button.objectName() == "primary" else "#27383e"
    button.setIcon(_font.icon(name, color=color, color_active=color, color_selected=color,
                           color_disabled="#839198" if dark else "#7b888e"))
    button.setIconSize(QSize(19, 19))


def refresh_icons(root):
    for button in root.findChildren(QAbstractButton):
        if name := button.property("remoteopsIcon"):
            set_button_icon(button, name)
