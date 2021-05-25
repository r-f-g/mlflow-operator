# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

import unittest
from unittest import mock
from unittest.mock import MagicMock, Mock

import yaml
from charm import MlflowCharm
from ops.model import BlockedStatus, ActiveStatus, Container
from ops.pebble import Plan, Service
from ops.testing import Harness
from serialized_data_interface import NoVersionsListed, NoCompatibleVersions

from tests.harness import TmpHarness


class TestCharmInit(unittest.TestCase):
    def test_get_interface(self):
        """Test get interface."""
        with mock.patch("charm.get_interfaces") as mock_get_interface:
            # no _supported_versions found
            harness = Harness(MlflowCharm)
            mock_get_interface.side_effect = NoVersionsListed("minio", "minio")
            harness.begin()
            self.assertFalse(hasattr(harness.charm, "interfaces"))

            # no compatible _supported_versions found
            harness = Harness(MlflowCharm)
            mock_get_interface.side_effect = NoCompatibleVersions("minio", "minio")
            harness.begin()
            self.assertFalse(hasattr(harness.charm, "interfaces"))


class TestCharm(unittest.TestCase):
    def setUp(self):
        self.harness = Harness(MlflowCharm)
        self.addCleanup(self.harness.cleanup)
        self.harness.begin()

    def test_on_install(self):
        """Test install hook."""
        self.harness.charm.unit.status = BlockedStatus("test")
        self.harness.charm.on.install.emit()
        self.assertEqual(self.harness.charm.unit.status, ActiveStatus())

    def test_relation_hook_on_no_leader(self):
        """Test all relation hook on no leader unit."""
        relation_event = MagicMock()
        self.harness.set_leader(False)
        self.harness.charm._stored.backend_store_uri = "test"
        self.harness.charm._stored.artifact_root = "test"

        # mysql
        self.harness.charm._on_mysql_relation_changed(relation_event)
        self.harness.charm._on_mysql_relation_broken(relation_event)
        # object-storage
        self.harness.charm._object_storage_relation_changed(relation_event)
        self.harness.charm._object_storage_relation_broken(relation_event)

        self.assertFalse(relation_event.relation.data.get.called)
        self.assertEqual(self.harness.charm._stored.backend_store_uri, "test")
        self.assertEqual(self.harness.charm._stored.artifact_root, "test")

    def test_config_changed(self):
        """Test handling configuration changes."""
        self.assertIn("--port 5000",
                      self.harness.charm._mlflow_layer()["services"]["server"]["command"])
        self.harness.update_config({"port": "5001"})
        self.assertIn("--port 5001",
                      self.harness.charm._mlflow_layer()["services"]["server"]["command"])

    def test_manage_server_layer(self):
        """Test managging the MLflow server with Pebble."""
        # check the initial Pebble plan is empty
        container = self.harness.model.unit.get_container("server")
        self.assertEqual(container.get_plan().to_dict(), {})  # validate that plan is empty

        # start service for first time
        with mock.patch.object(container, "_pebble", wraps=container._pebble) as mock_pebble:
            self.harness.charm._manage_server_layer()

            self.assertTrue(container.get_service("server").is_running())
            mock_pebble.start_services.assert_called_with(("server", ))
            mock_pebble.stop_services.assert_not_called()

        # pebble plan not changed
        with mock.patch.object(container, "_pebble", wraps=container._pebble) as mock_pebble:
            self.harness.charm._manage_server_layer()

            self.assertTrue(container.get_service("server").is_running())
            mock_pebble.start_services.assert_not_called()
            mock_pebble.stop_services.assert_not_called()

        # pebble plan changed
        self.harness.charm._stored.backend_store_uri = "sqlite:///test.db"  # to change pebble plan
        with mock.patch.object(container, "_pebble", wraps=container._pebble) as mock_pebble:
            self.harness.charm._manage_server_layer()

            self.assertTrue(container.get_service("server").is_running())
            mock_pebble.start_services.assert_called_with(("server", ))
            mock_pebble.stop_services.assert_called_with(("server", ))

    @mock.patch("charm.MlflowCharm._manage_server_layer")
    def test_server_pebble_ready(self, mock_manage_server_layer):
        """Test starting server container."""
        mock_service = MagicMock(is_running=Mock(return_value=True))
        mock_event = MagicMock()
        mock_event.workload.get_service.return_value = mock_service

        # service server is running
        self.harness.charm._on_server_pebble_ready(mock_event)
        self.assertEqual(self.harness.model.unit.status, ActiveStatus())

        # service server is not running
        mock_service.is_running.return_value = False
        self.harness.charm._on_server_pebble_ready(mock_event)
        self.assertEqual(self.harness.model.unit.status,
                         BlockedStatus("Mlflow server is not running."))

    def test_action_db_upgrade(self):
        """Test running MLflow database upgrade."""
        container = self.harness.model.unit.get_container("server")
        self.harness.charm.on.server_pebble_ready.emit(container)

        mock_service = MagicMock(is_running=Mock(return_value=True))
        mock_container = MagicMock()
        mock_container.get_service.return_value = mock_service
        self.harness.model.unit._containers = {"server": mock_container}

        # run without 'i-really-mean-it' parameter
        action_event = MagicMock(params={})
        self.harness.charm._dp_upgrade_action(action_event)

        self.assertFalse(action_event.set_results.called)
        self.assertTrue(action_event.fail.called)
        self.assertEqual(self.harness.charm.unit.status, ActiveStatus())
        mock_container.reset_mock()

        # run with 'i-really-mean-it' parameter [service is running]
        action_event = MagicMock(params={"i-really-mean-it": True})
        self.harness.charm._dp_upgrade_action(action_event)

        mock_container.stop.assert_called_with("server")
        # TODO: add test after implement execution of `mlflow db upgrade`
        mock_container.start.assert_called_with("server")
        self.assertTrue(action_event.set_results.called)
        self.assertFalse(action_event.fail.called)
        self.assertEqual(self.harness.charm.unit.status, ActiveStatus())
        mock_container.reset_mock()

        # run with 'i-really-mean-it' parameter [service is not running]
        action_event = MagicMock(params={"i-really-mean-it": True})
        mock_service.is_running.return_value = False
        self.harness.charm._dp_upgrade_action(action_event)

        mock_container.stop.assert_called_with("server")
        # TODO: add test after implement execution of `mlflow db upgrade`
        mock_container.start.assert_called_with("server")
        self.assertFalse(action_event.set_results.called)
        self.assertTrue(action_event.fail.called)
        self.assertEqual(self.harness.charm.unit.status,
                         BlockedStatus("MLflow server is not running"))
        mock_container.reset_mock()

    def test_action_db_upgrade_fail(self):
        """Test running MLflow database upgrade which fails."""
        # TODO: add test after implement execution of `mlflow db upgrade`
        pass


class TestInitialCharm(unittest.TestCase):
    def setUp(self):
        self.harness = TmpHarness(MlflowCharm)
        self.addCleanup(self.harness.cleanup)

    def check_server_container(self, host, port, backend_store_uri, artifact_root, environment):
        """Check server container and all services."""
        server_container: Container = self.harness.model.unit.get_container("server")
        self.assertTrue(server_container.get_service("server").is_running())
        pebble_plan: Plan = server_container.get_plan()
        self.assertIn("server", pebble_plan.services)
        server_service: Service = pebble_plan.services["server"]
        self.assertIn(f"--host {host}", server_service.command)
        self.assertIn(f"--port {port}", server_service.command)
        self.assertIn(f"--backend-store-uri {backend_store_uri}", server_service.command)
        self.assertEqual(self.harness.charm._stored.backend_store_uri, backend_store_uri)
        self.assertIn(f"--default-artifact-root {artifact_root}", server_service.command)
        self.assertEqual(self.harness.charm._stored.artifact_root, artifact_root)
        self.assertEqual(server_service.environment, environment)
        self.assertEqual(self.harness.charm._stored.minio_environment, environment)

    def test_main_no_relation(self):
        """Test initial without any relations."""
        self.harness.set_leader(True)
        self.harness.begin_with_initial_hooks()
        self.check_server_container("0.0.0.0", "5000", "sqlite:///mlflow.db", "./mlruns", {})
        self.assertEqual(self.harness.charm.unit.status, ActiveStatus())

    def test_main_mysql_relation(self):
        """Test initial with MySQL relation."""
        self.harness.set_leader(True)
        self.harness.begin_with_initial_hooks()
        self.assertEqual(self.harness.charm.unit.status, ActiveStatus())

        # add mysql relation
        rel_id = self.harness.add_relation("mysql", "mysql")
        self.harness.add_relation_unit(rel_id, "mysql/0")
        self.harness.update_relation_data(rel_id, "mysql/0", {
            "host": "mysql", "port": "3306", "user": "test",
            "password": "password", "database": "database"
        })
        self.check_server_container(
            "0.0.0.0", "5000", "mysql+pymysql://test:password@mysql:3306/database", "./mlruns", {}
        )
        self.assertEqual(self.harness.charm.unit.status, ActiveStatus())

        # remove mysql relation
        self.harness.remove_relation("mysql", "mysql")
        self.check_server_container("0.0.0.0", "5000", "sqlite:///mlflow.db", "./mlruns", {})
        self.assertEqual(self.harness.charm.unit.status, ActiveStatus())

    def test_main_minio_relation(self):
        """Test initial with Minio relation."""
        self.harness.set_leader(True)
        self.harness.begin_with_initial_hooks()
        self.assertEqual(self.harness.charm.unit.status, ActiveStatus())

        # add minio relation
        rel_id = self.harness.add_relation("object-storage", "minio")
        self.harness.add_relation_unit(rel_id, "minio/0")
        data = {
            "service": "test",
            "port": 9000,
            "access-key": "access-key",
            "secret-key": "secret-key",
            "secure": True,
        }
        self.harness.update_relation_data(
            rel_id, "minio", {"data": yaml.dump(data), "_supported_versions": yaml.dump(["v1"])},
        )
        self.check_server_container(
            "0.0.0.0", "5000", "sqlite:///mlflow.db", "s3://mlflow/", {
                "MLFLOW_S3_ENDPOINT_URL": "http://test:9000",
                "AWS_ACCESS_KEY_ID": "access-key",
                "AWS_SECRET_ACCESS_KEY": "secret-key",
                "MLFLOW_S3_IGNORE_TLS": "false",
            }
        )
        self.assertEqual(self.harness.charm.unit.status, ActiveStatus())

        # remove minio relation
        self.harness.remove_relation("object-storage", "minio")
        self.check_server_container("0.0.0.0", "5000", "sqlite:///mlflow.db", "./mlruns", {})
        self.assertEqual(self.harness.charm.unit.status, ActiveStatus())
