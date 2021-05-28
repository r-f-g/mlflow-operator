#!/usr/bin/env python3
# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Charm the service.

Refer to the following post for a quick-start guide that will help you
develop a new k8s charm using the Operator Framework:

    https://discourse.charmhub.io/t/4208
"""

import logging
from typing import Optional

import yaml
from minio import Minio
from ops.charm import (
    CharmBase,
    PebbleReadyEvent,
    RelationBrokenEvent,
    RelationChangedEvent,
    ActionEvent,
)
from ops.framework import StoredState
from ops.main import main
from ops.model import (
    ActiveStatus,
    BlockedStatus,
    MaintenanceStatus,
    RelationDataContent,
    WaitingStatus,
    ModelError,
)
from ops.pebble import TimeoutError, ConnectionError, APIError

from serialized_data_interface import (
    NoCompatibleVersions,
    NoVersionsListed,
    get_interfaces,
)

from charms.nginx_ingress_integrator.v0.ingress import IngressRequires

DEFAULT_BACKEND_STORE_URI = "sqlite:///mlflow.db"
DEFAULT_ARTIFACT_ROOT = "./mlruns"
MINIO_BUCKET = "mlflow"

logger = logging.getLogger(__name__)


class MlflowCharm(CharmBase):
    """Charm the service."""
    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        # set interfaces
        try:
            self.interfaces = get_interfaces(self)
        except NoVersionsListed as error:
            # This is a transient error that will be resolved in the next run.
            self.model.unit.status = WaitingStatus(str(error))
            return
        except NoCompatibleVersions as error:
            self.model.unit.status = BlockedStatus(str(error))
            return
        except ModelError:
            # if minio relation is removed
            #   ops.model.ModelError: b'ERROR "" is not a valid unit or application\n'
            pass
        # install operator and prepare services
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.server_pebble_ready, self._on_server_pebble_ready)
        # configurations
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        # actions
        self.framework.observe(self.on.db_upgrade_action, self._dp_upgrade_action)
        # relations
        self.framework.observe(self.on.mysql_relation_changed, self._on_mysql_relation_changed)
        self.framework.observe(self.on.mysql_relation_broken, self._on_mysql_relation_broken)
        self.framework.observe(self.on.object_storage_relation_changed,
                               self._object_storage_relation_changed)
        self.framework.observe(self.on.object_storage_relation_broken,
                               self._object_storage_relation_broken)
        # initialise stored state
        self._stored.set_default(
            backend_store_uri=DEFAULT_BACKEND_STORE_URI,
            artifact_root=DEFAULT_ARTIFACT_ROOT,
            minio_environment={})
        # initialise ingress
        self.ingress = IngressRequires(self, {
            "service-hostname": self.config["host"],
            "service-name": self.app.name,
            "service-port": self.config["port"]
        })

    @staticmethod
    def _create_bucket(endpoint: str, access_key: str, secret_key, secure: bool):
        """Create bucket for MLflow server."""
        client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
        if not client.bucket_exists(MINIO_BUCKET):
            client.make_bucket(MINIO_BUCKET)

    def _on_install(self, _):
        """Install on charm."""
        self.unit.status = WaitingStatus("The Pebble plan need to be created.")

    def _on_mysql_relation_changed(self, event: RelationChangedEvent):
        """Handle DB relation changed event."""
        if not self.unit.is_leader():
            return

        mysql: Optional[RelationDataContent] = event.relation.data.get(event.unit)
        if "user" not in mysql:
            self.unit.status = WaitingStatus("MySQL data are missing.")
            return

        # TODO: need to install pymysql to container or check if it's installed
        self._stored.backend_store_uri = \
            "mysql+pymysql://{user}:{password}@{host}:{port}/{database}".format(
                user=mysql.get("user"), password=mysql.get("password"),
                host=mysql.get("host"), port=mysql.get("port"), database=mysql.get("database")
            )
        self._on_config_changed(event)

    def _on_mysql_relation_broken(self, event: RelationChangedEvent):
        """Handle DB relation changed event."""
        if not self.unit.is_leader():
            return

        self._stored.backend_store_uri = DEFAULT_BACKEND_STORE_URI
        self._on_config_changed(event)

    def _object_storage_relation_changed(self, event: RelationChangedEvent):
        """Handle minio relation changed event."""
        if not self.unit.is_leader():
            return

        secrets = yaml.safe_load(event.relation.data.get(event.app, {}).get("data", "{}"))
        if "service" not in secrets:
            self.unit.status = WaitingStatus("Minio data are missing.")
            return

        endpoint = f"{secrets['service']}:{secrets['port']}"
        self._stored.minio_environment.update({
            "MLFLOW_S3_ENDPOINT_URL": f"http://{endpoint}",
            "AWS_ACCESS_KEY_ID": secrets["access-key"],
            "AWS_SECRET_ACCESS_KEY": secrets["secret-key"],
            "MLFLOW_S3_IGNORE_TLS": "false" if secrets["secure"] is True else "true"
        })
        self._stored.artifact_root = f"s3://{MINIO_BUCKET}/"
        self._create_bucket(endpoint, secrets["access-key"],
                            secrets["secret-key"], secrets["secure"])
        self._on_config_changed(event)

    def _object_storage_relation_broken(self, event: RelationBrokenEvent):
        """Handle minio relation broken event."""
        if not self.unit.is_leader():
            return

        self._stored.minio_environment = {}
        self._stored.artifact_root = DEFAULT_ARTIFACT_ROOT
        self._on_config_changed(event)

    def _mlflow_layer(self):
        """Returns a Pebble configuration layer for Mlflow."""
        host = self.config["host"]
        port = self.config["port"]
        backend_store_uri = self._stored.backend_store_uri
        artifact_root = self._stored.artifact_root
        environment = dict(self._stored.minio_environment)

        return {
            "summary": "MLflow server layer",
            "description": "pebble config layer for MLflow server",
            "services": {
                "server": {
                    "override": "replace",
                    "summary": "MLflow server",
                    "command": "/bin/sh -c \"mlflow server "
                               f"--host {host} "
                               f"--port {port} "
                               f"--backend-store-uri {backend_store_uri} "
                               f"--default-artifact-root {artifact_root} "
                                "|| exit 2\"",
                    "startup": "enabled",
                    "environment": environment
                }
            }
        }

    def _manage_server_layer(self):
        """Manage MLflow server layer with Pebble."""
        container = self.unit.get_container("server")
        mlflow_layer = self._mlflow_layer()
        services = container.get_plan().to_dict().get("services", {})
        actual_services = {service: {key: value for key, value in fields.items() if value}
                           for service, fields in mlflow_layer["services"].items()}

        if services != actual_services:
            self.unit.status = MaintenanceStatus("MLflow server maintenance")
            container.add_layer("mlflow-server", mlflow_layer, combine=True)
            if container.get_service("server").is_running():
                logging.info("Restarting MLflow server service")
                container.stop("server")

            container.start("server")

    def _on_server_pebble_ready(self, event: PebbleReadyEvent):
        """Start a workload using the Pebble API."""
        # TODO: install mlflow in container or check if it's installed
        try:
            self._manage_server_layer()
        except (TimeoutError, ConnectionError, APIError):
            self.unit.status = BlockedStatus("Pebble API connection problem.")
            event.defer()
            return

        if not event.workload.get_service("server").is_running():
            self.unit.status = BlockedStatus("Mlflow server is not running.")
            event.defer()
            return

        self.unit.status = ActiveStatus()

    def _on_config_changed(self, _):
        """Handle the config-changed event."""
        try:
            self._manage_server_layer()
        except (TimeoutError, ConnectionError, APIError):
            self.unit.status = BlockedStatus("Pebble API connection problem.")
            return

        self.ingress.update_config({
            "service-hostname": self.config["host"], "service-port": self.config["port"]
        })
        self.unit.status = ActiveStatus()

    def _dp_upgrade_action(self, event: ActionEvent):
        """Run MLflow dp upgrade."""
        if not event.params.get("i-really-mean-it"):
            event.fail("The 'i-really-mean-it' parameter must be toggled to enable actually "
                       "performing this action.")
            return

        container = self.unit.get_container("server")

        if not container.get_service("server").is_running():
            event.fail("MLflow server is not running.")
            return

        self.unit.status = MaintenanceStatus("Running MLflow db upgrade")
        logger.info("Running MLflow db upgrade")
        container.stop("server")
        # TODO: run `mlflow db upgrade`
        container.start("server")
        if container.get_service("server").is_running():
            event.set_results({"result": "MLflow database was upgrade"})
            self.unit.status = ActiveStatus()
        else:
            event.fail("MLflow server does not start after a restart.")
            self.unit.status = BlockedStatus("MLflow server is not running")


if __name__ == "__main__":
    main(MlflowCharm, use_juju_for_storage=True)
