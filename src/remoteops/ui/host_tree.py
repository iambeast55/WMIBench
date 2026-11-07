from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtWidgets import QAbstractItemView, QTreeWidget, QTreeWidgetItem

ROLE = Qt.ItemDataRole.UserRole


class HostTree(QTreeWidget):
    order_changed = Signal(object)

    def __init__(self):
        super().__init__()
        self.setObjectName("hostList")
        self.setHeaderHidden(True)
        self.setColumnCount(1)
        self.setIndentation(16)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.host_items = {}
        self.group_items = {}
        self.hosts = []

    def populate(self, hosts, groups, collapsed=()):
        self.blockSignals(True)
        self.clear()
        self.host_items = {}
        self.group_items = {}
        self.hosts = list(hosts)
        for name in [*groups, None]:
            item = QTreeWidgetItem([name if name is not None else "Ungrouped"])
            item.setData(0, ROLE, ("group", name))
            item.setSizeHint(0, QSize(0, 34))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsDropEnabled)
            if name is None:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)
            self.addTopLevelItem(item)
            item.setExpanded(name not in collapsed)
            self.group_items[name] = item
        for host in hosts:
            item = QTreeWidgetItem([host.name + "\n" + host.address])
            item.setData(0, ROLE, ("host", host.id))
            item.setSizeHint(0, QSize(0, 56))
            item.setToolTip(0, f"{host.name}\n{host.endpoint}")
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
            self.group_items[host.group_name].addChild(item)
            self.host_items[host.id] = item
        self.blockSignals(False)

    def selected_host_id(self):
        item = self.currentItem()
        identity = item.data(0, ROLE) if item else None
        return identity[1] if identity and identity[0] == "host" else None

    def select_host(self, host_id):
        item = self.host_items.get(host_id)
        if item and self.currentItem() is not item:
            self.setCurrentItem(item)

    def filter_hosts(self, text):
        text = text.casefold()
        visible = 0
        for host in self.hosts:
            matches = text in " ".join((host.name, host.address, host.group_name or "", *host.tags)).casefold()
            self.host_items[host.id].setHidden(not matches)
            visible += int(matches)
        for name, group in self.group_items.items():
            matching = any(not group.child(i).isHidden() for i in range(group.childCount()))
            group.setHidden(bool(text) and not matching and text not in (name or "Ungrouped").casefold())
            if text and matching:
                group.setExpanded(True)
        self.setDragEnabled(not text)
        self.setAcceptDrops(not text)
        return visible

    def inventory(self):
        return [(self.topLevelItem(i).data(0, ROLE)[1],
                 [self.topLevelItem(i).child(j).data(0, ROLE)[1] for j in range(self.topLevelItem(i).childCount())])
                for i in range(self.topLevelItemCount())]

    def move_host(self, host_id, group, index=None):
        groups = self.inventory()
        if group not in dict(groups) or host_id not in self.host_items:
            return
        for _, ids in groups:
            if host_id in ids:
                ids.remove(host_id)
        target = dict(groups)[group]
        target.insert(len(target) if index is None else max(0, min(index, len(target))), host_id)
        self.order_changed.emit(groups)

    def move_group(self, name, index):
        groups = self.inventory()
        source = next((row for row in groups if row[0] == name), None)
        if source is None or name is None:
            return
        groups.remove(source)
        groups.insert(max(0, min(index, len(groups) - 1)), source)
        self.order_changed.emit(groups)

    def dropEvent(self, event):
        source = self.currentItem()
        target = self.itemAt(event.position().toPoint())
        if event.source() is not self or source is None or source is target:
            event.ignore()
            return
        kind, identity = source.data(0, ROLE)
        after = self.dropIndicatorPosition() == QAbstractItemView.DropIndicatorPosition.BelowItem
        if kind == "group":
            if target is None or target.parent() is not None:
                event.ignore()
                return
            target_index = self.indexOfTopLevelItem(target) + int(after)
            if self.indexOfTopLevelItem(source) < target_index:
                target_index -= 1
            self.move_group(identity, target_index)
        else:
            group = target.parent() if target and target.parent() else target
            group_name = group.data(0, ROLE)[1] if group else None
            index = group.indexOfChild(target) + int(after) if target and target.parent() else None
            if index is not None and source.parent() is group and group.indexOfChild(source) < index:
                index -= 1
            self.move_host(identity, group_name, index)
        event.acceptProposedAction()
