import tempfile
import unittest
from pathlib import Path

from PySide6.QtWidgets import QApplication

from remoteops.core.database import Repository
from remoteops.core.models import Host, CredentialProfile, CommandResult
from remoteops.ui.host_tree import HostTree


class HostGroupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Repository(Path(self.temp.name) / "inventory.db")
        self.profile = self.repo.save_credential_profile(CredentialProfile("account", "user"))
        self.a = self.repo.save_host(Host("a", "a.example", group_name="Servers", auth_profile_id=self.profile.id))
        self.b = self.repo.save_host(Host("b", "b.example", group_name="servers"))
        self.c = self.repo.save_host(Host("c", "c.example"))
        self.repo.create_host_group("Lab")
        self.tree = HostTree()
        self.addCleanup(self.tree.deleteLater)
        self.tree.order_changed.connect(self.save)
        self.reload()

    def reload(self):
        self.tree.populate(self.repo.list_hosts(), self.repo.list_host_groups())

    def save(self, inventory):
        self.repo.save_inventory_order(inventory)
        self.reload()

    def test_reorder_and_move_survive_reload(self):
        self.assertEqual(self.b.group_name, "Servers")
        self.tree.move_host(self.b.id, "Servers", 0)
        self.assertEqual(dict(self.tree.inventory())["Servers"], [self.b.id, self.a.id])
        self.tree.move_host(self.a.id, "Lab")
        self.tree.move_group("Lab", 0)
        self.assertEqual(self.repo.list_host_groups(), ["Lab", "Servers"])
        self.assertEqual(self.repo.get_host(self.a.id).group_name, "Lab")
        reopened = Repository(self.repo.db_path)
        self.assertEqual(reopened.list_host_groups(), ["Lab", "Servers"])

    def test_group_deletion_keeps_hosts_history_credentials(self):
        self.repo.save_command_result(CommandResult(self.a.id, self.a.name, "sysinfo"))
        self.repo.rename_host_group("Servers", "Production")
        self.assertEqual(self.repo.get_host(self.b.id).group_name, "Production")
        self.repo.delete_host_group("Production")
        self.assertEqual([h.id for h in self.repo.list_hosts()], [self.c.id, self.a.id, self.b.id])
        self.assertTrue(all(h.group_name is None for h in self.repo.list_hosts()))
        self.repo.delete_host(self.a.id)
        self.assertEqual(len(self.repo.list_command_history()), 1)
        self.assertIsNotNone(self.repo.get_credential_profile(self.profile.id))

    def test_invalid_snapshot_cannot_partly_change_inventory(self):
        before = self.tree.inventory()
        for snapshot in [[("Lab", [self.a.id]), (None, [])],
                         [("Lab", []), ("Servers", [self.a.id, self.a.id, self.b.id]), (None, [self.c.id])]]:
            with self.assertRaises(ValueError):
                self.repo.save_inventory_order(snapshot)
            self.reload()
            self.assertEqual(self.tree.inventory(), before)
        with self.assertRaises(ValueError):
            self.repo.rename_host_group("Servers", "lab")
        self.assertEqual(self.repo.get_host(self.a.id).group_name, "Servers")

    def test_filter_and_collapsed_group(self):
        self.tree.populate(self.repo.list_hosts(), self.repo.list_host_groups(), ["Servers"])
        self.assertFalse(self.tree.group_items["Servers"].isExpanded())
        self.assertEqual(self.tree.filter_hosts("servers"), 2)
        self.assertTrue(self.tree.group_items["Servers"].isExpanded())
        self.assertTrue(self.tree.host_items[self.c.id].isHidden())
        self.assertFalse(self.tree.dragEnabled())
        self.tree.filter_hosts("")
        self.assertTrue(self.tree.dragEnabled())

    def test_legacy_names_become_groups(self):
        with self.repo.connect() as db:
            db.execute("delete from host_groups")
            db.execute("update hosts set group_name='old' where id=?", (self.a.id,))
            db.execute("update hosts set group_name='OLD' where id=?", (self.b.id,))
            db.execute("update hosts set group_name=' ' where id=?", (self.c.id,))
        migrated = Repository(self.repo.db_path)
        self.assertEqual(len(migrated.list_host_groups()), 1)
        self.assertEqual(migrated.get_host(self.a.id).group_name, migrated.get_host(self.b.id).group_name)
        self.assertIsNone(migrated.get_host(self.c.id).group_name)
