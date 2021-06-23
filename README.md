# mlflow

**MLflow** is an open source platform for managing the end-to-end machine learning lifecycle.

## Description

This charm provides **MLflow Tracking Server** and **Model Registry**, that can be described as
these functions:
 * Tracking experiments to record and compare parameters along with the model results.
 * Providing a central model store to collaboratively manage the full lifecycle of an MLflow Model, 
   including model versioning, stage transitions, and annotations.
   
For more information visit [MLflow Documentation][MLflow-docs]

## Usage

Right now there is no published version in [charmhub] and it needs to be
manually built with `charmcraft`.

    charmcraft build -v
    juju deploy ./mlflow.charm --resource server=docker.io/blueunicorn90/mlflow-operator:latest

The `mlflow-operator` image is simple image based on the `python:3.9-slim` image together with
the following python packages: 

 * mlflow >= 1.0
 * pymysql  # for connection with MySQL
 * boto3  # for connection with Minio

The use of deployed **mlflow** requires the following environmental variable:
    
    export MLFLOW_TRACKING_URI=http://<mlflow_ip>:<mlflow.config.port>

### Usage with Ingress [Optional]

The **nginx ingress integrator** can be used by a Nginx Ingress Controller in a Kubernetes 
cluster to expose MLflow server container. This charm requires your Kubernetes cluster to have
a Nginx Ingress Controller already deployed to it. For more information visit this
[link][nginx-ingress-integrator].

    juju deploy nginx-ingress-integrator ingress
    juju relate ingress mlflow
    # Add an entry to /etc/hosts
    echo "127.0.0.1 mlflow.server" | sudo tee -a /etc/hosts

Now you can visit [http://mlflow.server](http://mlflow.server).

---
**NOTE:**
To enable a Nginx Ingress Controller on **MicroK8s**, just run `microk8s enable ingress`.
---


### Usage with Minio [Optional]

**Minio** is used as object storage for artifact store of **MLflow Tracking server**, where clients
log their artifact output (e.g. models). 

    juju deploy cs:minio-55
    juju relate minio mlflow

A bucket named `mlflow` is created and artifact data is stored in it.

The use of deployed **mlflow** requires the following environmental variables:
    
    export MLFLOW_S3_ENDPOINT_URL=http://<minio_ip>:<minio.config.port>
    export AWS_ACCESS_KEY_ID=<minio.config.access-key>
    export AWS_SECRET_ACCESS_KEY=<minio.config.secret-key>
    export MLFLOW_S3_IGNORE_TLS=true

More information about artifact stores can be found [here][artifact-stores].

---
**NOTE:**
By default, the file storage is used inside the sidecar container.
---

### Usage with MySQL [Optional]

**MLflow Tracking Server** is database to stores experiment and run metadata as well as params,
metrics, and tags for each run.

    juju deploy cs:~charmed-osm/mariadb-k8s-35
    juju relate mariadb-k8s mlflow

---
**NOTE:**
By default, the **sqlite** database is used inside the sidecar container.
---

### Usage example

Create and activate a virtualenv:

    virtualenv -p python3 venv
    source venv/bin/activate
    pip install 'mlflow>=1.0'

Define environmental variables:

    export MLFLOW_TRACKING_URI=http://<mlflow_ip>:<mlflow.config.port>
    export MLFLOW_S3_ENDPOINT_URL=http://<minio_ip>:<minio.config.port>
    export AWS_ACCESS_KEY_ID=<minio.config.access-key>
    export AWS_SECRET_ACCESS_KEY=<minio.config.secret-key>
    export MLFLOW_S3_IGNORE_TLS=true

Create python `train.py` script (`chmod +x train.py`):

```python
#!/usr/bin/env python
import os
import random
import tempfile

import mlflow


with mlflow.start_run():
    mlflow.log_param("param1", 1)
    mlflow.log_param("param2", 2)
    mlflow.log_metric("score", 0.8)
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "artifact-file"), "w") as file:
            file.write(str(random.randint(0, 10)))

        mlflow.log_artifacts(tmpdir)

print("DONE")
```

Execute script:

    ./train.py


## Developing

Create and activate a virtualenv with the development requirements:

    virtualenv -p python3 venv
    source venv/bin/activate
    pip install -r requirements-dev.txt

## Testing

The Python operator framework includes a very nice harness for testing
operator behaviour without full deployment. Just `run_tests`:

    ./run_tests

## Roadmap

* [x] MLflow Tracking Server
  * [x] using local file system or Minio (via relation)
  * [ ] make a configurable Minio bucket name
* [x] MLflow Model Registry
  * [x] using local database or MySQL (via relation)
  * [ ] run database upgrade with `mlflow dp upgrade` command
* [ ] MLflow Projects
   * [ ] create/remove MLProject
   * [ ] create templates for MLProject (using kubernetes as backend)
   * [ ] create worker to train models (execute `mlflow run`)
* [ ] MLflow Models
   * [ ] create template for MLModels (using kubernetes as backend)
   * [ ] create worker to deploy models (e.g. execute `mlflow server`)
* [ ] functional tests


---
[MLflow-docs]: https://mlflow.org/docs/latest/index.html
[charmhub]: https://charmhub.io/
[artifact-stores]: https://mlflow.org/docs/latest/tracking.html#id69
[nginx-ingress-integrator]: https://charmhub.io/nginx-ingress-integrator