import logging
import os
import random
import re
import tempfile

import mlflow
import pytest
import pymysql
from minio import Minio

log = logging.getLogger(__name__)


def _get_ip(text):
    """Get subnet IP address."""
    try:
        return re.findall(r"[0-9]+(?:\.[0-9]+){3}", text)[0]
    except IndexError:
        return None


def _run_test_train():
    """Run test train."""
    experiment_id = mlflow.create_experiment(f"experiment-{random.randint(0, 1000):04d}")
    with mlflow.start_run(experiment_id=experiment_id) as run:
        mlflow.log_params({"param1": 1, "param2": 2})
        mlflow.log_metric("score", 0.8)
        with tempfile.TemporaryDirectory() as tmpdir:
            local_artifact_path = os.path.join(tmpdir, "test")
            with open(local_artifact_path, "w") as file:
                file.write(str(random.randint(0, 10)))

            mlflow.log_artifact(local_artifact_path)

        return run.info.run_id


async def _check_mlflow_server(model, use_ingress=False):
    """Validate that the mlflow server is working correctly."""
    if use_ingress:
        mlflow_host = _get_ip(model.applications["ingress"].units[0].workload_status_message)
        assert mlflow_host is not None, "Failed to get IP address from ingress unit."
    else:
        status = await model.get_status()
        mlflow_host = status.applications["mlflow"].units["mlflow/0"].address

    mlflow_config = await model.applications["mlflow"].get_config()
    mlflow_port = mlflow_config.get("port", {}).get("value")

    mlflow.set_tracking_uri(f"http://{mlflow_host}:{mlflow_port}")
    run_id = _run_test_train()
    run = mlflow.get_run(run_id)

    assert run.info.status == "FINISHED"
    assert run.data.metrics == {"score": 0.8}
    assert run.data.params == {"param1": "1", "param2": "2"}

    log.info(f"the training '{run.info.run_id}' was successful ")
    return run


@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test):
    """Build and deploy Flannel in bundle."""
    mlflow_operator = await ops_test.build_charm(".")
    # work around bug https://bugs.launchpad.net/juju/+bug/1928796
    rc, stdout, stderr = await ops_test._run(
        "juju",
        "deploy",
        mlflow_operator,
        "-m", ops_test.model_full_name,
        "--resource", "server=blueunicorn90/mlflow-operator:1.18",
        "--channel", "edge"
    )
    assert rc == 0, f"Failed to deploy with resource: {stderr or stdout}"
    await ops_test.model.deploy(ops_test.render_bundle(
        "tests/data/bundle.yaml", master_charm=mlflow_operator))
    # work around bug https://github.com/juju/python-libjuju/issues/511
    rc, stdout, stderr = await ops_test._run(
        "juju",
        "deploy",
        "nginx-ingress-integrator",
        "ingress",
        "-m", ops_test.model_full_name,
        "--channel", "stable"
    )
    assert rc == 0, f"Failed to deploy with resource: {stderr or stdout}"
    await ops_test.model.wait_for_idle(wait_for_active=True)


async def test_mlflow_status_message(ops_test):
    """Validate mlflow status message."""
    unit = ops_test.model.applications["mlflow"].units[0]
    assert unit.workload_status == "active"
    assert unit.workload_status_message == "MLflow server is ready"
    await _check_mlflow_server(ops_test.model)


async def test_add_ingress_relations(ops_test):
    """Validate that adding the Nginx Ingress Integrator relations works."""
    await ops_test.model.add_relation("mlflow", "ingress")
    await ops_test.model.wait_for_idle(wait_for_active=True)
    await _check_mlflow_server(ops_test.model, use_ingress=True)


async def test_remove_ingress_relations(ops_test):
    """Validate that removing the Nginx Ingress Integrator relations works."""
    ingress_application = ops_test.model.applications["ingress"]
    await ingress_application.destroy_relation("ingress", "mlflow")
    await ops_test.model.wait_for_idle(wait_for_active=True)
    await _check_mlflow_server(ops_test.model)


async def test_add_minio_relations(ops_test):
    """Validate that adding the Minio relation works."""
    await ops_test.model.add_relation("mlflow", "minio")
    await ops_test.model.wait_for_idle(wait_for_active=True)

    # configuration environment variables before test run
    minio_app = ops_test.model.applications["minio"]
    await minio_app.set_config({"secret-key": "minio1234"})
    await ops_test.model.wait_for_idle(wait_for_active=True)

    status = await ops_test.model.get_status()
    minio_ip = status.applications["minio"].units["minio/0"].address
    os.environ["MLFLOW_S3_ENDPOINT_URL"] = f"http://{minio_ip}:9000"
    os.environ["AWS_ACCESS_KEY_ID"] = "minio"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "minio1234"
    os.environ["MLFLOW_S3_IGNORE_TLS"] = "true"

    run = await _check_mlflow_server(ops_test.model)

    client = Minio(f"{minio_ip}:9000", access_key="minio", secret_key="minio1234", secure=False)
    assert client.bucket_exists("mlflow")
    prefix = run.info.artifact_uri.replace("s3://mlflow/", "")
    objects = [obj.object_name for obj in client.list_objects("mlflow", prefix, recursive=True)]
    assert f"{prefix}/test" in objects


async def test_remove_minio_relations(ops_test):
    """Validate that removing the Minio relations works."""
    minio_application = ops_test.model.applications["minio"]
    await minio_application.destroy_relation("object-storage", "mlflow")
    await ops_test.model.wait_for_idle(wait_for_active=True)
    await _check_mlflow_server(ops_test.model)


async def test_add_db_relations(ops_test):
    """Validate that adding a DB relation works."""
    await ops_test.model.add_relation("mlflow", "mariadb-k8s")
    await ops_test.model.wait_for_idle(wait_for_active=True)
    run = await _check_mlflow_server(ops_test.model)

    status = await ops_test.model.get_status()
    mariadb_k8s_ip = status.applications["mariadb-k8s"].units["mariadb-k8s/0"].address

    connection = pymysql.connect(
        host=mariadb_k8s_ip,
        port=3306,
        user="root",
        password="root",
        db="database",
        cursorclass=pymysql.cursors.DictCursor
    )
    with connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT run_uuid FROM runs;")
            results = cursor.fetchall()
            assert run.info.run_uuid in [result.get("run_uuid") for result in results]


async def test_remove_db_relations(ops_test):
    """Validate that removing a DB relations works."""
    db_application = ops_test.model.applications["mariadb-k8s"]
    await db_application.destroy_relation("mysql", "mlflow")
    await ops_test.model.wait_for_idle(wait_for_active=True)
    await _check_mlflow_server(ops_test.model)
