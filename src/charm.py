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
from ops.charm import CharmBase, PebbleReadyEvent, RelationEvent
from ops.framework import StoredState
from ops.main import main
from ops.model import (
    ActiveStatus,
    BlockedStatus,
    MaintenanceStatus,
    RelationDataContent,
    WaitingStatus,
)
from serialized_data_interface import (
    NoCompatibleVersions,
    NoVersionsListed,
    get_interfaces,
)

from charms.nginx_ingress_integrator.v0.ingress import IngressRequires

DEFAULT_BACKEND_STORE_URI = "sqlite:///mlflow.db"
DEFAULT_ARTIFACT_ROOT = "./mlruns"

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
            self.model.unit.status = WaitingStatus(str(error))
            return
        except NoCompatibleVersions as error:
            self.model.unit.status = BlockedStatus(str(error))
            return
        # install operator and prepare services
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.server_pebble_ready, self._on_server_pebble_ready)
        # configurations
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        # actions
        self.framework.observe(self.on.db_upgrade_action, self._dp_upgrade_action)
        # relations
        self.framework.observe(self.on.mysql_relation_changed, self._on_mysql_relation_changed)
        self.framework.observe(self.on.mysql_relation_broken, self._on_mysql_relation_changed)
        self.framework.observe(self.on.object_storage_relation_changed,
                               self._object_storage_relation_changed)
        self.framework.observe(self.on.object_storage_relation_departed,
                               self._object_storage_relation_changed)
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

    def _on_install(self, _):
        """Install on charm."""
        self.unit.status = ActiveStatus()

    def _on_mysql_relation_changed(self, event: RelationEvent):
        """Handle DB relation event."""
        if not self.unit.is_leader():
            return

        mysql: Optional[RelationDataContent] = event.relation.data.get(event.unit)
        if mysql:
            # TODO: need to install pymysql to container or check if it's installed
            db_uri = "mysql+pymysql://{user}:{password}@{host}:{port}/{database}".format(
                user=mysql.get("user"), password=mysql.get("password"),
                host=mysql.get("host"), port=mysql.get("port"), database=mysql.get("database")
            )
        else:
            db_uri = DEFAULT_BACKEND_STORE_URI

        self._stored.backend_store_uri = db_uri
        self._on_config_changed(event)

    def _object_storage_relation_changed(self, event: RelationEvent):
        """Handle minio relation event."""
        if not self.unit.is_leader():
            return

        minio_secrets = yaml.safe_load(event.relation.data.get(event.app, {}).get("data", "{}"))
        minio_service = minio_secrets.get("service", None)

        if minio_service:
            minio_url = f"http://{minio_secrets['service']}:{minio_secrets['port']}"
            self._stored.minio_environment.update({
                "MLFLOW_S3_ENDPOINT_URL": minio_url,
                "AWS_ACCESS_KEY_ID": minio_secrets["access-key"],
                "AWS_SECRET_ACCESS_KEY": minio_secrets["secret-key"],
            })
            artifact_root = "s3://mlflow/"
        else:
            self._stored.minio_environment.clean()
            artifact_root = DEFAULT_ARTIFACT_ROOT

        self._stored.artifact_root = artifact_root
        self._on_config_changed(event)

    def _mlflow_layer(self):
        """Returns a Pebble configuration layer for Mlflow."""
        host = self.config["host"]
        port = self.config["port"]
        backend_store_uri = self._stored.backend_store_uri
        artifact_root = self._stored.artifact_root
        environment = self._stored.minio_environment

        return {
            "summary": "MLflow server layer",
            "description": "pebble config layer for MLflow server",
            "services": {
                "server": {
                    "override": "replace",
                    "summary": "MLflow server",
                    "command": "mlflow server"
                               f" --host {host}"
                               f" --port {port}"
                               f" --backend-store-uri {backend_store_uri}"
                               f" --default-artifact-root {artifact_root}",
                    "startup": "enabled",
                    "environment": environment
                }
            }
        }

    def _manage_server_layer(self):
        """Manage MLflow server layer w/ Pebble."""
        container = self.unit.get_container("server")
        mlflow_layer = self._mlflow_layer()
        services = container.get_plan().to_dict().get("services", {})
        if services.get("server") != mlflow_layer["services"]["server"]:
            self.unit.status = MaintenanceStatus("MLflow server maintenance")
            container.add_layer("mlflow-server", mlflow_layer, combine=True)
            if container.get_service("server").is_running():
                logging.info("Restarting MLflow server service")
                container.stop("server")

            container.start("server")

    def _on_server_pebble_ready(self, event: PebbleReadyEvent):
        """Define and start a workload using the Pebble API."""
        # TODO: install mlflow in container or check if it's installed
        self._manage_server_layer()

        if not event.workload.get_service("server").is_running():
            self.unit.status = BlockedStatus("The Mlflow server is not running.")
            event.defer()
            return

        self.unit.status = ActiveStatus()

    def _on_config_changed(self, _):
        """Handle the config-changed event."""
        self._manage_server_layer()
        self.ingress.update_config({
            "service-hostname": self.config["host"], "service-port": self.config["port"]
        })
        self.unit.status = ActiveStatus()

    def _dp_upgrade_action(self, event):
        """Run MLflow dp upgrade."""
        container = self.unit.get_container("server")
        if event.params["i-really-mean-it"] is True:
            self.unit.status = MaintenanceStatus("Running MLflow db upgrade")
            logger.info("Running MLflow db upgrade")
            container.stop("server")
            # TODO: run `mlflow db upgrade`
            container.start("server")
            self.unit.status = ActiveStatus()


if __name__ == "__main__":
    main(MlflowCharm)
