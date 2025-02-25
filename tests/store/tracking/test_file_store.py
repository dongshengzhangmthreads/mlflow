#!/usr/bin/env python
import os
import posixpath
import random
import shutil
import tempfile
import time
import unittest
import uuid
from pathlib import Path
import re

import pytest
from unittest import mock

from mlflow.entities import (
    Metric,
    Param,
    RunTag,
    ViewType,
    LifecycleStage,
    RunStatus,
    RunData,
    ExperimentTag,
)
from mlflow.store.entities.paged_list import PagedList
from mlflow.exceptions import MlflowException, MissingConfigException
from mlflow.store.tracking import SEARCH_MAX_RESULTS_DEFAULT
from mlflow.store.tracking.file_store import FileStore
from mlflow.utils.file_utils import write_yaml, read_yaml, path_to_local_file_uri, TempDir
from mlflow.utils.uri import append_to_uri_path
from mlflow.utils.name_utils import _GENERATOR_PREDICATES, _EXPERIMENT_ID_FIXED_WIDTH
from mlflow.utils.mlflow_tags import MLFLOW_RUN_NAME
from mlflow.utils.time_utils import get_current_time_millis
from mlflow.protos.databricks_pb2 import (
    ErrorCode,
    RESOURCE_DOES_NOT_EXIST,
    INTERNAL_ERROR,
    INVALID_PARAMETER_VALUE,
)

from tests.helper_functions import random_int, random_str, safe_edit_yaml, is_local_os_windows
from tests.store.tracking import AbstractStoreTest

FILESTORE_PACKAGE = "mlflow.store.tracking.file_store"


class TestFileStore(unittest.TestCase, AbstractStoreTest):
    ROOT_LOCATION = tempfile.gettempdir()

    def create_test_run(self):
        fs = FileStore(self.test_root)
        return self._create_run(fs)

    def create_experiments(self, experiment_names):
        ids = []
        for name in experiment_names:
            # ensure that the field `last_update_time` is distinct for search ordering
            time.sleep(0.001)
            ids.append(self.store.create_experiment(name))
        return ids

    def initialize(self):
        shutil.rmtree(self.test_root, ignore_errors=True)
        self.store = self.get_store()  # pylint: disable=attribute-defined-outside-init

    def setUp(self):
        self._create_root(TestFileStore.ROOT_LOCATION)
        self.maxDiff = None

    def get_store(self):
        return FileStore(self.test_root)

    def _create_root(self, root):
        self.test_root = os.path.join(root, "test_file_store_%d" % random_int())
        os.mkdir(self.test_root)
        self.experiments = [str(random_int(100, int(1e9))) for _ in range(3)]
        self.exp_data = {}
        self.run_data = {}
        # Include default experiment
        self.experiments.append(FileStore.DEFAULT_EXPERIMENT_ID)
        for exp in self.experiments:
            # create experiment
            exp_folder = os.path.join(self.test_root, str(exp))
            os.makedirs(exp_folder)
            current_time = get_current_time_millis()
            d = {
                "experiment_id": exp,
                "name": random_str(),
                "artifact_location": exp_folder,
                "creation_time": current_time,
                "last_update_time": current_time,
            }
            self.exp_data[exp] = d
            write_yaml(exp_folder, FileStore.META_DATA_FILE_NAME, d)
            # add runs
            self.exp_data[exp]["runs"] = []
            for _ in range(2):
                run_id = uuid.uuid4().hex
                self.exp_data[exp]["runs"].append(run_id)
                run_folder = os.path.join(exp_folder, run_id)
                os.makedirs(run_folder)
                run_info = {
                    "run_uuid": run_id,
                    "run_id": run_id,
                    "run_name": "name",
                    "experiment_id": exp,
                    "user_id": random_str(random_int(10, 25)),
                    "status": random.choice(RunStatus.all_status()),
                    "start_time": random_int(1, 10),
                    "end_time": random_int(20, 30),
                    "deleted_time": random_int(20, 30),
                    "tags": [],
                    "artifact_uri": os.path.join(run_folder, FileStore.ARTIFACTS_FOLDER_NAME),
                }
                write_yaml(run_folder, FileStore.META_DATA_FILE_NAME, run_info)
                self.run_data[run_id] = run_info
                # tags
                os.makedirs(os.path.join(run_folder, FileStore.TAGS_FOLDER_NAME))
                # params
                params_folder = os.path.join(run_folder, FileStore.PARAMS_FOLDER_NAME)
                os.makedirs(params_folder)
                params = {}
                for _ in range(5):
                    param_name = random_str(random_int(10, 12))
                    param_value = random_str(random_int(10, 15))
                    param_file = os.path.join(params_folder, param_name)
                    with open(param_file, "w") as f:
                        f.write(param_value)
                    params[param_name] = param_value
                self.run_data[run_id]["params"] = params
                # metrics
                metrics_folder = os.path.join(run_folder, FileStore.METRICS_FOLDER_NAME)
                os.makedirs(metrics_folder)
                metrics = {}
                for _ in range(3):
                    metric_name = random_str(random_int(10, 12))
                    timestamp = get_current_time_millis()
                    metric_file = os.path.join(metrics_folder, metric_name)
                    values = []
                    for _ in range(10):
                        metric_value = random_int(100, 2000)
                        timestamp += random_int(10000, 2000000)
                        values.append((timestamp, metric_value))
                        with open(metric_file, "a") as f:
                            f.write("%d %d\n" % (timestamp, metric_value))
                    metrics[metric_name] = values
                self.run_data[run_id]["metrics"] = metrics
                # artifacts
                os.makedirs(os.path.join(run_folder, FileStore.ARTIFACTS_FOLDER_NAME))

    def tearDown(self):
        shutil.rmtree(self.test_root, ignore_errors=True)

    def test_valid_root(self):
        # Test with valid root
        file_store = FileStore(self.test_root)
        try:
            file_store._check_root_dir()
        except Exception as e:
            self.fail("test_valid_root raised exception '%s'" % e.message)

        # Test removing root
        second_file_store = FileStore(self.test_root)
        shutil.rmtree(self.test_root)
        with pytest.raises(Exception, match=r"does not exist"):
            second_file_store._check_root_dir()

    def test_attempting_to_remove_default_experiment(self):
        def _is_default_in_experiments(view_type):
            search_result = file_store.search_experiments(view_type=view_type)
            ids = [experiment.experiment_id for experiment in search_result]
            return FileStore.DEFAULT_EXPERIMENT_ID in ids

        file_store = FileStore(self.test_root)
        assert _is_default_in_experiments(ViewType.ACTIVE_ONLY)

        # Ensure experiment deletion of default id raises
        with pytest.raises(MlflowException, match="Cannot delete the default experiment"):
            file_store.delete_experiment(FileStore.DEFAULT_EXPERIMENT_ID)

    def test_search_experiments_view_type(self):
        self.initialize()
        experiment_names = ["a", "b"]
        experiment_ids = self.create_experiments(experiment_names)
        self.store.delete_experiment(experiment_ids[1])

        experiments = self.store.search_experiments(view_type=ViewType.ACTIVE_ONLY)
        assert [e.name for e in experiments] == ["a", "Default"]
        experiments = self.store.search_experiments(view_type=ViewType.DELETED_ONLY)
        assert [e.name for e in experiments] == ["b"]
        experiments = self.store.search_experiments(view_type=ViewType.ALL)
        assert [e.name for e in experiments] == ["b", "a", "Default"]

    def test_search_experiments_filter_by_attribute(self):
        self.initialize()
        experiment_names = ["a", "ab", "Abc"]
        self.create_experiments(experiment_names)

        experiments = self.store.search_experiments(filter_string="name = 'a'")
        assert [e.name for e in experiments] == ["a"]
        experiments = self.store.search_experiments(filter_string="attribute.name = 'a'")
        assert [e.name for e in experiments] == ["a"]
        experiments = self.store.search_experiments(filter_string="attribute.`name` = 'a'")
        assert [e.name for e in experiments] == ["a"]
        experiments = self.store.search_experiments(filter_string="attribute.`name` != 'a'")
        assert [e.name for e in experiments] == ["Abc", "ab", "Default"]
        experiments = self.store.search_experiments(filter_string="name LIKE 'a%'")
        assert [e.name for e in experiments] == ["ab", "a"]
        experiments = self.store.search_experiments(
            filter_string="name ILIKE 'a%'", order_by=["last_update_time asc"]
        )
        assert [e.name for e in experiments] == ["a", "ab", "Abc"]
        experiments = self.store.search_experiments(filter_string="name ILIKE 'a%'")
        assert [e.name for e in experiments] == ["Abc", "ab", "a"]
        experiments = self.store.search_experiments(
            filter_string="name ILIKE 'a%' AND name ILIKE '%b'"
        )
        assert [e.name for e in experiments] == ["ab"]

    def test_search_experiments_filter_by_time_attribute(self):
        self.initialize()
        # Sleep to ensure that the first experiment has a different creation_time than the default
        # experiment and eliminate flakiness.
        time.sleep(0.001)
        time_before_create1 = get_current_time_millis()
        exp_id1 = self.store.create_experiment("1")
        exp1 = self.store.get_experiment(exp_id1)
        time.sleep(0.001)
        time_before_create2 = get_current_time_millis()
        exp_id2 = self.store.create_experiment("2")
        exp2 = self.store.get_experiment(exp_id2)

        experiments = self.store.search_experiments(
            filter_string=f"creation_time = {exp1.creation_time}"
        )
        assert [e.experiment_id for e in experiments] == [exp_id1]

        experiments = self.store.search_experiments(
            filter_string=f"creation_time != {exp1.creation_time}"
        )
        assert [e.experiment_id for e in experiments] == [exp_id2, self.store.DEFAULT_EXPERIMENT_ID]

        experiments = self.store.search_experiments(
            filter_string=f"creation_time >= {time_before_create1}"
        )
        assert [e.experiment_id for e in experiments] == [exp_id2, exp_id1]

        experiments = self.store.search_experiments(
            filter_string=f"creation_time < {time_before_create2}"
        )
        assert [e.experiment_id for e in experiments] == [exp_id1, self.store.DEFAULT_EXPERIMENT_ID]

        now = get_current_time_millis()
        experiments = self.store.search_experiments(filter_string=f"creation_time > {now}")
        assert experiments == []

        time_before_rename = get_current_time_millis()
        self.store.rename_experiment(exp_id1, "new_name")
        experiments = self.store.search_experiments(
            filter_string=f"last_update_time >= {time_before_rename}"
        )
        assert [e.experiment_id for e in experiments] == [exp_id1]

        experiments = self.store.search_experiments(
            filter_string=f"last_update_time <= {get_current_time_millis()}"
        )
        assert {e.experiment_id for e in experiments} == {
            exp_id1,
            exp_id2,
            self.store.DEFAULT_EXPERIMENT_ID,
        }

        experiments = self.store.search_experiments(
            filter_string=f"last_update_time = {exp2.last_update_time}"
        )
        assert [e.experiment_id for e in experiments] == [exp_id2]

    def test_search_experiments_filter_by_tag(self):
        self.initialize()
        experiments = [
            ("exp1", [ExperimentTag("key", "value")]),
            ("exp2", [ExperimentTag("key", "vaLue")]),
            ("exp3", [ExperimentTag("k e y", "value")]),
        ]
        for name, tags in experiments:
            # sleep for windows file system current_time precision in Python to enforce
            # deterministic ordering based on last_update_time (creation_time due to no
            # mutation of experiment state)
            time.sleep(0.01)
            self.store.create_experiment(name, tags=tags)

        experiments = self.store.search_experiments(filter_string="tag.key = 'value'")
        assert [e.name for e in experiments] == ["exp1"]
        experiments = self.store.search_experiments(filter_string="tag.`k e y` = 'value'")
        assert [e.name for e in experiments] == ["exp3"]
        experiments = self.store.search_experiments(filter_string="tag.\"k e y\" = 'value'")
        assert [e.name for e in experiments] == ["exp3"]
        experiments = self.store.search_experiments(filter_string="tag.key != 'value'")
        assert [e.name for e in experiments] == ["exp2"]
        experiments = self.store.search_experiments(filter_string="tag.key LIKE 'val%'")
        assert [e.name for e in experiments] == ["exp1"]
        experiments = self.store.search_experiments(filter_string="tag.key LIKE '%Lue'")
        assert [e.name for e in experiments] == ["exp2"]
        experiments = self.store.search_experiments(filter_string="tag.key ILIKE '%alu%'")
        assert [e.name for e in experiments] == ["exp2", "exp1"]
        experiments = self.store.search_experiments(
            filter_string="tag.key LIKE 'va%' AND tags.key LIKE '%Lue'"
        )
        assert [e.name for e in experiments] == ["exp2"]

    def test_search_experiments_filter_by_attribute_and_tag(self):
        self.initialize()
        self.store.create_experiment(
            "exp1", tags=[ExperimentTag("a", "1"), ExperimentTag("b", "2")]
        )
        self.store.create_experiment(
            "exp2", tags=[ExperimentTag("a", "3"), ExperimentTag("b", "4")]
        )
        experiments = self.store.search_experiments(
            filter_string="name ILIKE 'exp%' AND tag.a = '1'"
        )
        assert [e.name for e in experiments] == ["exp1"]

    def test_search_experiments_order_by(self):
        self.initialize()
        experiment_names = ["x", "y", "z"]
        time.sleep(0.05)
        self.create_experiments(experiment_names)

        # Test the case where an experiment does not have a creation time by simulating a time of
        # `None`. This is applicable to experiments created in older versions of MLflow where the
        # `creation_time` attribute did not exist
        with mock.patch(
            "mlflow.store.tracking.file_store.get_current_time_millis",
            return_value=None,
        ):
            self.create_experiments(["n"])

        experiments = self.store.search_experiments(order_by=["name"])
        assert [e.name for e in experiments] == ["Default", "n", "x", "y", "z"]

        experiments = self.store.search_experiments(order_by=["name ASC"])
        assert [e.name for e in experiments] == ["Default", "n", "x", "y", "z"]

        experiments = self.store.search_experiments(order_by=["name DESC"])
        assert [e.name for e in experiments] == ["z", "y", "x", "n", "Default"]

        experiments = self.store.search_experiments(order_by=["creation_time DESC"])
        assert [e.name for e in experiments] == ["z", "y", "x", "Default", "n"]

        experiments = self.store.search_experiments(order_by=["creation_time ASC"])
        assert [e.name for e in experiments] == ["Default", "x", "y", "z", "n"]

        experiments = self.store.search_experiments(order_by=["name", "last_update_time asc"])
        assert [e.name for e in experiments] == ["Default", "n", "x", "y", "z"]

    def test_search_experiments_order_by_time_attribute(self):
        self.initialize()
        # Sleep to ensure that the first experiment has a different creation_time than the default
        # experiment and eliminate flakiness.
        time.sleep(0.001)
        exp_id1 = self.store.create_experiment("1")
        time.sleep(0.001)
        exp_id2 = self.store.create_experiment("2")

        experiments = self.store.search_experiments(order_by=["creation_time"])
        assert [e.experiment_id for e in experiments] == [
            self.store.DEFAULT_EXPERIMENT_ID,
            exp_id1,
            exp_id2,
        ]

        experiments = self.store.search_experiments(order_by=["creation_time DESC"])
        assert [e.experiment_id for e in experiments] == [
            exp_id2,
            exp_id1,
            self.store.DEFAULT_EXPERIMENT_ID,
        ]

        experiments = self.store.search_experiments(order_by=["last_update_time"])
        assert [e.experiment_id for e in experiments] == [
            self.store.DEFAULT_EXPERIMENT_ID,
            exp_id1,
            exp_id2,
        ]

        time.sleep(0.001)
        self.store.rename_experiment(exp_id1, "new_name")
        experiments = self.store.search_experiments(order_by=["last_update_time"])
        assert [e.experiment_id for e in experiments] == [
            self.store.DEFAULT_EXPERIMENT_ID,
            exp_id2,
            exp_id1,
        ]

    def test_search_experiments_max_results(self):
        self.initialize()
        experiment_names = list(map(str, range(9)))
        self.create_experiments(experiment_names)
        reversed_experiment_names = experiment_names[::-1]

        experiments = self.store.search_experiments()
        assert [e.name for e in experiments] == reversed_experiment_names + ["Default"]
        experiments = self.store.search_experiments(max_results=3)
        assert [e.name for e in experiments] == reversed_experiment_names[:3]

    def test_search_experiments_max_results_validation(self):
        self.initialize()
        with pytest.raises(MlflowException, match=r"It must be a positive integer, but got None"):
            self.store.search_experiments(max_results=None)
        with pytest.raises(MlflowException, match=r"It must be a positive integer, but got 0"):
            self.store.search_experiments(max_results=0)
        with pytest.raises(MlflowException, match=r"It must be at most \d+, but got 1000000"):
            self.store.search_experiments(max_results=1_000_000)

    def test_search_experiments_pagination(self):
        self.initialize()
        experiment_names = list(map(str, range(9)))
        self.create_experiments(experiment_names)
        reversed_experiment_names = experiment_names[::-1]

        experiments = self.store.search_experiments(max_results=4)
        assert [e.name for e in experiments] == reversed_experiment_names[:4]
        assert experiments.token is not None

        experiments = self.store.search_experiments(max_results=4, page_token=experiments.token)
        assert [e.name for e in experiments] == reversed_experiment_names[4:8]
        assert experiments.token is not None

        experiments = self.store.search_experiments(max_results=4, page_token=experiments.token)
        assert [e.name for e in experiments] == reversed_experiment_names[8:] + ["Default"]
        assert experiments.token is None

    def _verify_experiment(self, fs, exp_id):
        exp = fs.get_experiment(exp_id)
        assert exp.experiment_id == exp_id
        assert exp.name == self.exp_data[exp_id]["name"]
        assert exp.artifact_location == self.exp_data[exp_id]["artifact_location"]

    def test_get_experiment(self):
        fs = FileStore(self.test_root)
        for exp_id in self.experiments:
            self._verify_experiment(fs, exp_id)

        # test that fake experiments dont exist.
        # look for random experiment ids between 8000, 15000 since created ones are (100, 2000)
        for exp_id in {random_int(8000, 15000) for x in range(20)}:
            with pytest.raises(Exception, match=f"Could not find experiment with ID {exp_id}"):
                fs.get_experiment(str(exp_id))

    def test_get_experiment_int_experiment_id_backcompat(self):
        fs = FileStore(self.test_root)
        exp_id = FileStore.DEFAULT_EXPERIMENT_ID
        root_dir = os.path.join(self.test_root, exp_id)
        with safe_edit_yaml(root_dir, "meta.yaml", self._experiment_id_edit_func):
            self._verify_experiment(fs, exp_id)

    def test_get_experiment_retries_for_transient_empty_yaml_read(self):
        fs = FileStore(self.test_root)
        exp_name = random_str()
        exp_id = fs.create_experiment(exp_name)

        mock_empty_call_count = 0

        def mock_read_yaml_impl(*args, **kwargs):
            nonlocal mock_empty_call_count
            if mock_empty_call_count < 2:
                mock_empty_call_count += 1
                return None
            else:
                return read_yaml(*args, **kwargs)

        with mock.patch(
            "mlflow.store.tracking.file_store.read_yaml", side_effect=mock_read_yaml_impl
        ) as mock_read_yaml:
            fetched_experiment = fs.get_experiment(exp_id)
            assert fetched_experiment.experiment_id == exp_id
            assert fetched_experiment.name == exp_name
            assert mock_read_yaml.call_count == 3

    def test_get_experiment_by_name(self):
        fs = FileStore(self.test_root)
        for exp_id in self.experiments:
            name = self.exp_data[exp_id]["name"]
            exp = fs.get_experiment_by_name(name)
            assert exp.experiment_id == exp_id
            assert exp.name == self.exp_data[exp_id]["name"]
            assert exp.artifact_location == self.exp_data[exp_id]["artifact_location"]

        # test that fake experiments dont exist.
        # look up experiments with names of length 15 since created ones are of length 10
        for exp_names in {random_str(15) for x in range(20)}:
            exp = fs.get_experiment_by_name(exp_names)
            assert exp is None

    def test_create_additional_experiment_generates_random_fixed_length_id(self):
        fs = FileStore(self.test_root)
        fs._get_active_experiments = mock.Mock(return_value=[])
        fs._get_deleted_experiments = mock.Mock(return_value=[])
        fs._create_experiment_with_id = mock.Mock()
        fs.create_experiment(random_str())
        fs._create_experiment_with_id.assert_called_once()
        experiment_id = fs._create_experiment_with_id.call_args[0][1]
        assert len(experiment_id) == _EXPERIMENT_ID_FIXED_WIDTH

    def test_create_experiment(self):
        fs = FileStore(self.test_root)

        # Error cases
        with pytest.raises(Exception, match="Invalid experiment name: 'None'"):
            fs.create_experiment(None)
        with pytest.raises(Exception, match="Invalid experiment name: ''"):
            fs.create_experiment("")
        name = random_str(25)  # since existing experiments are 10 chars long
        time_before_create = get_current_time_millis()
        created_id = fs.create_experiment(name)
        # test that newly created experiment id is random but of a fixed length
        assert len(created_id) == _EXPERIMENT_ID_FIXED_WIDTH

        # get the new experiment (by id) and verify (by name)
        exp1 = fs.get_experiment(created_id)
        assert exp1.name == name
        assert exp1.artifact_location == path_to_local_file_uri(
            posixpath.join(self.test_root, created_id)
        )
        assert exp1.creation_time >= time_before_create
        assert exp1.last_update_time == exp1.creation_time

        # get the new experiment (by name) and verify (by id)
        exp2 = fs.get_experiment_by_name(name)
        assert exp2.experiment_id == created_id
        assert exp2.creation_time == exp1.creation_time
        assert exp2.last_update_time == exp1.last_update_time

    def test_create_experiment_with_tags_works_correctly(self):
        fs = FileStore(self.test_root)

        created_id = fs.create_experiment(
            "heresAnExperiment",
            "heresAnArtifact",
            [ExperimentTag("key1", "val1"), ExperimentTag("key2", "val2")],
        )
        experiment = fs.get_experiment(created_id)
        assert len(experiment.tags) == 2
        assert experiment.tags["key1"] == "val1"
        assert experiment.tags["key2"] == "val2"

    def test_create_duplicate_experiments(self):
        fs = FileStore(self.test_root)
        for exp_id in self.experiments:
            name = self.exp_data[exp_id]["name"]
            with pytest.raises(Exception, match=f"Experiment '{name}' already exists"):
                fs.create_experiment(name)

    def _extract_ids(self, experiments):
        return [e.experiment_id for e in experiments]

    def test_delete_restore_experiment(self):
        fs = FileStore(self.test_root)
        exp_id = fs.create_experiment("test_delete")
        exp_name = fs.get_experiment(exp_id).name

        exp1 = fs.get_experiment(exp_id)
        time.sleep(0.001)

        # delete it
        fs.delete_experiment(exp_id)
        assert exp_id not in self._extract_ids(
            fs.search_experiments(view_type=ViewType.ACTIVE_ONLY)
        )
        assert exp_id in self._extract_ids(fs.search_experiments(view_type=ViewType.DELETED_ONLY))
        assert exp_id in self._extract_ids(fs.search_experiments(view_type=ViewType.ALL))
        assert fs.get_experiment(exp_id).lifecycle_stage == LifecycleStage.DELETED

        deleted_exp1 = fs.get_experiment(exp_id)
        assert deleted_exp1.last_update_time > exp1.last_update_time
        assert deleted_exp1.lifecycle_stage == LifecycleStage.DELETED

        # restore it
        exp1 = fs.get_experiment(exp_id)
        time.sleep(0.01)
        fs.restore_experiment(exp_id)
        restored_1 = fs.get_experiment(exp_id)
        assert restored_1.experiment_id == exp_id
        assert restored_1.name == exp_name
        assert restored_1.last_update_time > exp1.last_update_time

        restored_2 = fs.get_experiment_by_name(exp_name)
        assert restored_2.experiment_id == exp_id
        assert restored_2.name == exp_name
        assert exp_id in self._extract_ids(fs.search_experiments(view_type=ViewType.ACTIVE_ONLY))
        assert exp_id not in self._extract_ids(
            fs.search_experiments(view_type=ViewType.DELETED_ONLY)
        )
        assert exp_id in self._extract_ids(fs.search_experiments(view_type=ViewType.ALL))
        assert fs.get_experiment(exp_id).lifecycle_stage == LifecycleStage.ACTIVE

    def test_rename_experiment(self):
        fs = FileStore(self.test_root)
        exp_id = fs.create_experiment("test_rename")

        # Error cases
        with pytest.raises(Exception, match="Invalid experiment name: 'None'"):
            fs.rename_experiment(exp_id, None)
        # test that names of existing experiments are checked before renaming
        other_exp_id = None
        for exp in self.experiments:
            if exp != exp_id:
                other_exp_id = exp
                break
        name = fs.get_experiment(other_exp_id).name
        with pytest.raises(Exception, match=f"Experiment '{name}' already exists"):
            fs.rename_experiment(exp_id, name)

        exp_name = fs.get_experiment(exp_id).name
        new_name = exp_name + "!!!"
        assert exp_name != new_name
        assert fs.get_experiment(exp_id).name == exp_name
        fs.rename_experiment(exp_id, new_name)
        assert fs.get_experiment(exp_id).name == new_name

        # Ensure that we cannot rename deleted experiments.
        fs.delete_experiment(exp_id)
        with pytest.raises(
            Exception, match="Cannot rename experiment in non-active lifecycle stage"
        ) as e:
            fs.rename_experiment(exp_id, exp_name)
        assert "non-active lifecycle" in str(e.value)
        assert fs.get_experiment(exp_id).name == new_name

        # Restore the experiment, and confirm that we can now rename it.
        exp1 = fs.get_experiment(exp_id)
        time.sleep(0.01)
        fs.restore_experiment(exp_id)
        restored_exp1 = fs.get_experiment(exp_id)
        assert restored_exp1.name == new_name
        assert restored_exp1.last_update_time > exp1.last_update_time

        exp1 = fs.get_experiment(exp_id)
        time.sleep(0.01)
        fs.rename_experiment(exp_id, exp_name)
        renamed_exp1 = fs.get_experiment(exp_id)
        assert renamed_exp1.name == exp_name
        assert renamed_exp1.last_update_time > exp1.last_update_time

    def test_delete_restore_run(self):
        fs = FileStore(self.test_root)
        exp_id = self.experiments[random_int(0, len(self.experiments) - 1)]
        run_id = self.exp_data[exp_id]["runs"][0]
        _, run_dir = fs._find_run_root(run_id)
        # Should not throw.
        assert fs.get_run(run_id).info.lifecycle_stage == "active"
        # Verify that run deletion is idempotent by deleting twice
        fs.delete_run(run_id)
        fs.delete_run(run_id)
        assert fs.get_run(run_id).info.lifecycle_stage == "deleted"
        meta = read_yaml(run_dir, FileStore.META_DATA_FILE_NAME)
        assert "deleted_time" in meta and meta["deleted_time"] is not None
        # Verify that run restoration is idempotent by restoring twice
        fs.restore_run(run_id)
        fs.restore_run(run_id)
        assert fs.get_run(run_id).info.lifecycle_stage == "active"
        meta = read_yaml(run_dir, FileStore.META_DATA_FILE_NAME)
        assert "deleted_time" not in meta

    def test_hard_delete_run(self):
        fs = FileStore(self.test_root)
        exp_id = self.experiments[random_int(0, len(self.experiments) - 1)]
        run_id = self.exp_data[exp_id]["runs"][0]
        fs._hard_delete_run(run_id)
        with pytest.raises(MlflowException, match=f"Run '{run_id}' not found"):
            fs.get_run(run_id)
        with pytest.raises(MlflowException, match=f"Run '{run_id}' not found"):
            fs.get_all_tags(run_id)
        with pytest.raises(MlflowException, match=f"Run '{run_id}' not found"):
            fs.get_all_metrics(run_id)
        with pytest.raises(MlflowException, match=f"Run '{run_id}' not found"):
            fs.get_all_params(run_id)

    def test_get_deleted_runs(self):
        fs = FileStore(self.test_root)
        exp_id = self.experiments[0]
        run_id = self.exp_data[exp_id]["runs"][0]
        fs.delete_run(run_id)
        deleted_runs = fs._get_deleted_runs()
        assert len(deleted_runs) == 1
        assert deleted_runs[0] == run_id

    def test_create_run_in_deleted_experiment(self):
        fs = FileStore(self.test_root)
        exp_id = fs.create_experiment("test")
        fs.delete_experiment(exp_id)
        with pytest.raises(Exception, match="Could not create run under non-active experiment"):
            fs.create_run(exp_id, "user", 0, [], "name")

    def test_create_run_returns_expected_run_data(self):
        fs = FileStore(self.test_root)
        no_tags_run = fs.create_run(
            experiment_id=FileStore.DEFAULT_EXPERIMENT_ID,
            user_id="user",
            start_time=0,
            tags=[],
            run_name=None,
        )
        assert isinstance(no_tags_run.data, RunData)
        assert len(no_tags_run.data.tags) == 1

        run_name = no_tags_run.info.run_name
        assert run_name.split("-")[0] in _GENERATOR_PREDICATES

        run_name = no_tags_run.info.run_name
        assert run_name.split("-")[0] in _GENERATOR_PREDICATES

        tags_dict = {
            "my_first_tag": "first",
            "my-second-tag": "2nd",
        }
        tags_entities = [RunTag(key, value) for key, value in tags_dict.items()]
        tags_run = fs.create_run(
            experiment_id=FileStore.DEFAULT_EXPERIMENT_ID,
            user_id="user",
            start_time=0,
            tags=tags_entities,
            run_name=None,
        )
        assert isinstance(tags_run.data, RunData)
        assert tags_run.data.tags == {**tags_dict, MLFLOW_RUN_NAME: tags_run.info.run_name}

        name_empty_str_run = fs.create_run(
            experiment_id=FileStore.DEFAULT_EXPERIMENT_ID,
            user_id="user",
            start_time=0,
            tags=tags_entities,
            run_name="",
        )
        run_name = name_empty_str_run.info.run_name
        assert run_name.split("-")[0] in _GENERATOR_PREDICATES

    def test_create_run_sets_name(self):
        fs = FileStore(self.test_root)
        run = fs.create_run(
            experiment_id=FileStore.DEFAULT_EXPERIMENT_ID,
            user_id="user",
            start_time=0,
            tags=[],
            run_name="my name",
        )

        run = fs.get_run(run.info.run_id)
        assert run.info.run_name == "my name"
        assert run.data.tags.get(MLFLOW_RUN_NAME) == "my name"

        run_id = fs.create_run(
            experiment_id=FileStore.DEFAULT_EXPERIMENT_ID,
            user_id="user",
            start_time=0,
            run_name=None,
            tags=[RunTag(MLFLOW_RUN_NAME, "test")],
        ).info.run_id
        run = fs.get_run(run_id)
        assert run.info.run_name == "test"

        with pytest.raises(
            MlflowException,
            match=re.escape(
                "Both 'run_name' argument and 'mlflow.runName' tag are specified, but with "
                "different values (run_name='my name', mlflow.runName='test')."
            ),
        ):
            fs.create_run(
                experiment_id=FileStore.DEFAULT_EXPERIMENT_ID,
                user_id="user",
                start_time=0,
                run_name="my name",
                tags=[RunTag(MLFLOW_RUN_NAME, "test")],
            )

    def _experiment_id_edit_func(self, old_dict):
        old_dict["experiment_id"] = int(old_dict["experiment_id"])
        return old_dict

    def _verify_run(self, fs, run_id):
        run = fs.get_run(run_id)
        run_info = self.run_data[run_id]
        run_info.pop("metrics", None)
        run_info.pop("params", None)
        run_info.pop("tags", None)
        run_info.pop("deleted_time", None)
        run_info["lifecycle_stage"] = LifecycleStage.ACTIVE
        run_info["status"] = RunStatus.to_string(run_info["status"])
        # get a copy of run_info as we need to remove the `deleted_time`
        # key without actually deleting it from self.run_data
        _run_info = run_info.copy()
        _run_info.pop("deleted_time", None)
        assert _run_info == dict(run.info)

    def test_get_run(self):
        fs = FileStore(self.test_root)
        for exp_id in self.experiments:
            runs = self.exp_data[exp_id]["runs"]
            for run_id in runs:
                self._verify_run(fs, run_id)

    def test_get_run_returns_name_in_info(self):
        fs = FileStore(self.test_root)
        run_id = fs.create_run(
            experiment_id=FileStore.DEFAULT_EXPERIMENT_ID,
            user_id="user",
            start_time=0,
            tags=[],
            run_name="my name",
        ).info.run_id

        get_run = fs.get_run(run_id)
        assert get_run.info.run_name == "my name"

    def test_get_run_retries_for_transient_empty_yaml_read(self):
        fs = FileStore(self.test_root)
        run = self._create_run(fs)

        mock_empty_call_count = 0

        def mock_read_yaml_impl(*args, **kwargs):
            nonlocal mock_empty_call_count
            if mock_empty_call_count < 2:
                mock_empty_call_count += 1
                return None
            else:
                return read_yaml(*args, **kwargs)

        with mock.patch(
            "mlflow.store.tracking.file_store.read_yaml", side_effect=mock_read_yaml_impl
        ) as mock_read_yaml:
            fetched_run = fs.get_run(run.info.run_id)
            assert fetched_run.info.run_id == run.info.run_id
            assert fetched_run.info.artifact_uri == run.info.artifact_uri
            assert mock_read_yaml.call_count == 3

    def test_get_run_int_experiment_id_backcompat(self):
        fs = FileStore(self.test_root)
        exp_id = FileStore.DEFAULT_EXPERIMENT_ID
        run_id = self.exp_data[exp_id]["runs"][0]
        root_dir = os.path.join(self.test_root, exp_id, run_id)
        with safe_edit_yaml(root_dir, "meta.yaml", self._experiment_id_edit_func):
            self._verify_run(fs, run_id)

    def test_update_run_renames_run(self):
        fs = FileStore(self.test_root)
        run_id = fs.create_run(
            experiment_id=FileStore.DEFAULT_EXPERIMENT_ID,
            user_id="user",
            start_time=0,
            tags=[],
            run_name="first name",
        ).info.run_id
        fs.update_run_info(run_id, RunStatus.FINISHED, 1000, "new name")
        get_run = fs.get_run(run_id)
        assert get_run.info.run_name == "new name"

    def test_update_run_does_not_rename_run_with_none_name(self):
        fs = FileStore(self.test_root)
        run_id = fs.create_run(
            experiment_id=FileStore.DEFAULT_EXPERIMENT_ID,
            user_id="user",
            start_time=0,
            tags=[],
            run_name="first name",
        ).info.run_id
        fs.update_run_info(run_id, RunStatus.FINISHED, 1000, None)
        get_run = fs.get_run(run_id)
        assert get_run.info.run_name == "first name"

    def test_log_metric_allows_multiple_values_at_same_step_and_run_data_uses_max_step_value(self):
        fs = FileStore(self.test_root)
        run_id = self._create_run(fs).info.run_id

        metric_name = "test-metric-1"
        # Check that we get the max of (step, timestamp, value) in that order
        tuples_to_log = [
            (0, 100, 1000),
            (3, 40, 100),  # larger step wins even though it has smaller value
            (3, 50, 10),  # larger timestamp wins even though it has smaller value
            (3, 50, 20),  # tiebreak by max value
            (3, 50, 20),  # duplicate metrics with same (step, timestamp, value) are ok
            # verify that we can log steps out of order / negative steps
            (-3, 900, 900),
            (-1, 800, 800),
        ]
        for step, timestamp, value in reversed(tuples_to_log):
            fs.log_metric(run_id, Metric(metric_name, value, timestamp, step))

        metric_history = fs.get_metric_history(run_id, metric_name)
        logged_tuples = [(m.step, m.timestamp, m.value) for m in metric_history]
        assert set(logged_tuples) == set(tuples_to_log)

        run_data = fs.get_run(run_id).data
        run_metrics = run_data.metrics
        assert len(run_metrics) == 1
        assert run_metrics[metric_name] == 20
        metric_obj = run_data._metric_objs[0]
        assert metric_obj.key == metric_name
        assert metric_obj.step == 3
        assert metric_obj.timestamp == 50
        assert metric_obj.value == 20

    def test_log_metric_with_non_numeric_value_raises_exception(self):
        fs = FileStore(self.test_root)
        run_id = self._create_run(fs).info.run_id
        with pytest.raises(MlflowException, match=r"Got invalid value string for metric"):
            fs.log_metric(run_id, Metric("test", "string", 0, 0))

    def test_get_all_metrics(self):
        fs = FileStore(self.test_root)
        for exp_id in self.experiments:
            runs = self.exp_data[exp_id]["runs"]
            for run_id in runs:
                run_info = self.run_data[run_id]
                metrics = fs.get_all_metrics(run_id)
                metrics_dict = run_info.pop("metrics")
                for metric in metrics:
                    expected_timestamp, expected_value = max(metrics_dict[metric.key])
                    assert metric.timestamp == expected_timestamp
                    assert metric.value == expected_value

    def test_get_metric_history(self):
        fs = FileStore(self.test_root)
        for exp_id in self.experiments:
            runs = self.exp_data[exp_id]["runs"]
            for run_id in runs:
                run_info = self.run_data[run_id]
                metrics = run_info.pop("metrics")
                for metric_name, values in metrics.items():
                    metric_history = fs.get_metric_history(run_id, metric_name)
                    sorted_values = sorted(values, reverse=True)
                    for metric in metric_history:
                        timestamp, metric_value = sorted_values.pop()
                        assert metric.timestamp == timestamp
                        assert metric.key == metric_name
                        assert metric.value == metric_value

    def test_get_metric_history_paginated_request_raises(self):
        fs = FileStore(self.test_root)
        with pytest.raises(
            MlflowException,
            match="The FileStore backend does not support pagination for the `get_metric_history` "
            "API.",
        ):
            fs.get_metric_history("fake_run", "fake_metric", max_results=50, page_token="42")

    def _search(
        self,
        fs,
        experiment_id,
        filter_str=None,
        run_view_type=ViewType.ALL,
        max_results=SEARCH_MAX_RESULTS_DEFAULT,
    ):
        return [
            r.info.run_id
            for r in fs.search_runs([experiment_id], filter_str, run_view_type, max_results)
        ]

    def test_search_runs(self):
        # replace with test with code is implemented
        fs = FileStore(self.test_root)
        # Expect 2 runs for each experiment
        assert len(self._search(fs, self.experiments[0], run_view_type=ViewType.ACTIVE_ONLY)) == 2
        assert len(self._search(fs, self.experiments[0])) == 2
        assert len(self._search(fs, self.experiments[0], run_view_type=ViewType.DELETED_ONLY)) == 0

    def test_search_tags(self):
        fs = FileStore(self.test_root)
        experiment_id = self.experiments[0]
        r1 = fs.create_run(experiment_id, "user", 0, [], "name").info.run_id
        r2 = fs.create_run(experiment_id, "user", 0, [], "name").info.run_id

        fs.set_tag(r1, RunTag("generic_tag", "p_val"))
        fs.set_tag(r2, RunTag("generic_tag", "p_val"))

        fs.set_tag(r1, RunTag("generic_2", "some value"))
        fs.set_tag(r2, RunTag("generic_2", "another value"))

        fs.set_tag(r1, RunTag("p_a", "abc"))
        fs.set_tag(r2, RunTag("p_b", "ABC"))

        # test search returns both runs
        assert sorted(
            [r1, r2],
        ) == sorted(self._search(fs, experiment_id, filter_str="tags.generic_tag = 'p_val'"))
        # test search returns appropriate run (same key different values per run)
        assert self._search(fs, experiment_id, filter_str="tags.generic_2 = 'some value'") == [r1]
        assert self._search(fs, experiment_id, filter_str="tags.generic_2='another value'") == [r2]
        assert self._search(fs, experiment_id, filter_str="tags.generic_tag = 'wrong_val'") == []
        assert self._search(fs, experiment_id, filter_str="tags.generic_tag != 'p_val'") == []
        assert sorted([r1, r2],) == sorted(
            self._search(fs, experiment_id, filter_str="tags.generic_tag != 'wrong_val'"),
        )
        assert sorted([r1, r2],) == sorted(
            self._search(fs, experiment_id, filter_str="tags.generic_2 != 'wrong_val'"),
        )
        assert self._search(fs, experiment_id, filter_str="tags.p_a = 'abc'") == [r1]
        assert self._search(fs, experiment_id, filter_str="tags.p_b = 'ABC'") == [r2]

        assert self._search(fs, experiment_id, filter_str="tags.generic_2 LIKE '%other%'") == [r2]
        assert self._search(fs, experiment_id, filter_str="tags.generic_2 LIKE 'other%'") == []
        assert self._search(fs, experiment_id, filter_str="tags.generic_2 LIKE '%other'") == []
        assert self._search(fs, experiment_id, filter_str="tags.generic_2 ILIKE '%OTHER%'") == [r2]

    def test_search_with_max_results(self):
        fs = FileStore(self.test_root)
        exp = fs.create_experiment("search_with_max_results")

        runs = [fs.create_run(exp, "user", r, [], "name").info.run_id for r in range(10)]
        runs.reverse()

        assert runs[:10] == self._search(fs, exp)
        for n in [0, 1, 2, 4, 8, 10, 20, 50, 100, 500, 1000, 1200, 2000]:
            assert runs[: min(1200, n)] == self._search(fs, exp, max_results=n)

        with pytest.raises(
            MlflowException, match="Invalid value for request parameter max_results. It "
        ):
            self._search(fs, exp, None, max_results=int(1e10))

    def test_search_with_deterministic_max_results(self):
        fs = FileStore(self.test_root)
        exp = fs.create_experiment("test_search_with_deterministic_max_results")

        # Create 10 runs with the same start_time.
        # Sort based on run_id
        runs = sorted([fs.create_run(exp, "user", 1000, [], "name").info.run_id for r in range(10)])
        for n in [0, 1, 2, 4, 8, 10, 20]:
            assert runs[: min(10, n)] == self._search(fs, exp, max_results=n)

    def test_search_runs_pagination(self):
        fs = FileStore(self.test_root)
        exp = fs.create_experiment("test_search_runs_pagination")
        # test returned token behavior
        runs = sorted([fs.create_run(exp, "user", 1000, [], "name").info.run_id for r in range(10)])
        result = fs.search_runs([exp], None, ViewType.ALL, max_results=4)
        assert [r.info.run_id for r in result] == runs[0:4]
        assert result.token is not None
        result = fs.search_runs([exp], None, ViewType.ALL, max_results=4, page_token=result.token)
        assert [r.info.run_id for r in result] == runs[4:8]
        assert result.token is not None
        result = fs.search_runs([exp], None, ViewType.ALL, max_results=4, page_token=result.token)
        assert [r.info.run_id for r in result] == runs[8:]
        assert result.token is None

    def test_search_runs_run_name(self):
        fs = FileStore(self.test_root)
        exp_id = fs.create_experiment("test_search_runs_pagination")
        run1 = fs.create_run(exp_id, user_id="user", start_time=1000, tags=[], run_name="run_name1")
        run2 = fs.create_run(exp_id, user_id="user", start_time=1000, tags=[], run_name="run_name2")
        result = fs.search_runs(
            [exp_id],
            filter_string="attributes.run_name = 'run_name1'",
            run_view_type=ViewType.ACTIVE_ONLY,
        )
        assert [r.info.run_id for r in result] == [run1.info.run_id]
        result = fs.search_runs(
            [exp_id],
            filter_string="tags.`mlflow.runName` = 'run_name2'",
            run_view_type=ViewType.ACTIVE_ONLY,
        )
        assert [r.info.run_id for r in result] == [run2.info.run_id]

        fs.update_run_info(
            run1.info.run_id,
            RunStatus.FINISHED,
            end_time=run1.info.end_time,
            run_name="new_run_name1",
        )
        result = fs.search_runs(
            [exp_id],
            filter_string="attributes.run_name = 'new_run_name1'",
            run_view_type=ViewType.ACTIVE_ONLY,
        )
        assert [r.info.run_id for r in result] == [run1.info.run_id]

        result = fs.search_runs(
            [exp_id],
            filter_string="attributes.`run name` = 'new_run_name1'",
            run_view_type=ViewType.ACTIVE_ONLY,
        )
        assert [r.info.run_id for r in result] == [run1.info.run_id]

        result = fs.search_runs(
            [exp_id],
            filter_string="attributes.`Run name` = 'new_run_name1'",
            run_view_type=ViewType.ACTIVE_ONLY,
        )
        assert [r.info.run_id for r in result] == [run1.info.run_id]

        result = fs.search_runs(
            [exp_id],
            filter_string="attributes.`Run Name` = 'new_run_name1'",
            run_view_type=ViewType.ACTIVE_ONLY,
        )
        assert [r.info.run_id for r in result] == [run1.info.run_id]

        # TODO: Test attribute-based search after set_tag

        # Test run name filter works for runs logged in MLflow <= 1.29.0
        run_meta_path = Path(self.test_root, exp_id, run1.info.run_id, "meta.yaml")
        without_run_name = run_meta_path.read_text().replace("run_name: new_run_name1\n", "")
        run_meta_path.write_text(without_run_name)
        result = fs.search_runs(
            [exp_id],
            filter_string="attributes.run_name = 'new_run_name1'",
            run_view_type=ViewType.ACTIVE_ONLY,
        )
        assert [r.info.run_id for r in result] == [run1.info.run_id]
        result = fs.search_runs(
            [exp_id],
            filter_string="tags.`mlflow.runName` = 'new_run_name1'",
            run_view_type=ViewType.ACTIVE_ONLY,
        )
        assert [r.info.run_id for r in result] == [run1.info.run_id]

    def test_search_runs_run_id(self):
        fs = FileStore(self.test_root)
        exp_id = fs.create_experiment("test_search_runs_run_id")
        # Set start_time to ensure the search result is deterministic
        run1 = fs.create_run(exp_id, user_id="user", start_time=1, tags=[], run_name="1")
        run2 = fs.create_run(exp_id, user_id="user", start_time=2, tags=[], run_name="2")
        run_id1 = run1.info.run_id
        run_id2 = run2.info.run_id

        result = fs.search_runs(
            [exp_id],
            filter_string=f"attributes.run_id = '{run_id1}'",
            run_view_type=ViewType.ACTIVE_ONLY,
        )
        assert [r.info.run_id for r in result] == [run_id1]

        result = fs.search_runs(
            [exp_id],
            filter_string=f"attributes.run_id != '{run_id1}'",
            run_view_type=ViewType.ACTIVE_ONLY,
        )
        assert [r.info.run_id for r in result] == [run_id2]

        result = fs.search_runs(
            [exp_id],
            filter_string=f"attributes.run_id IN ('{run_id1}')",
            run_view_type=ViewType.ACTIVE_ONLY,
        )
        assert [r.info.run_id for r in result] == [run_id1]

        result = fs.search_runs(
            [exp_id],
            filter_string=f"attributes.run_id NOT IN ('{run_id1}')",
            run_view_type=ViewType.ACTIVE_ONLY,
        )
        assert [r.info.run_id for r in result] == [run_id2]

        for filter_string in [
            f"attributes.run_id IN ('{run_id1}','{run_id2}')",
            f"attributes.run_id IN ('{run_id1}', '{run_id2}')",
            f"attributes.run_id IN ('{run_id1}',  '{run_id2}')",
        ]:
            result = fs.search_runs(
                [exp_id], filter_string=filter_string, run_view_type=ViewType.ACTIVE_ONLY
            )
            assert [r.info.run_id for r in result] == [run_id2, run_id1]

        result = fs.search_runs(
            [exp_id],
            filter_string=f"attributes.run_id NOT IN ('{run_id1}', '{run_id2}')",
            run_view_type=ViewType.ACTIVE_ONLY,
        )
        assert result == []

    def test_search_runs_start_time_alias(self):
        fs = FileStore(self.test_root)
        exp_id = fs.create_experiment("test_search_runs_start_time_alias")
        # Set start_time to ensure the search result is deterministic
        run1 = fs.create_run(exp_id, user_id="user", start_time=1, tags=[], run_name="name")
        run2 = fs.create_run(exp_id, user_id="user", start_time=2, tags=[], run_name="name")
        run_id1 = run1.info.run_id
        run_id2 = run2.info.run_id

        result = fs.search_runs(
            [exp_id],
            filter_string="attributes.run_name = 'name'",
            run_view_type=ViewType.ACTIVE_ONLY,
            order_by=["attributes.start_time DESC"],
        )
        assert [r.info.run_id for r in result] == [run_id2, run_id1]

        result = fs.search_runs(
            [exp_id],
            filter_string="attributes.run_name = 'name'",
            run_view_type=ViewType.ACTIVE_ONLY,
            order_by=["attributes.created ASC"],
        )
        assert [r.info.run_id for r in result] == [run_id1, run_id2]

        result = fs.search_runs(
            [exp_id],
            filter_string="attributes.run_name = 'name'",
            run_view_type=ViewType.ACTIVE_ONLY,
            order_by=["attributes.Created DESC"],
        )
        assert [r.info.run_id for r in result] == [run_id2, run_id1]

        result = fs.search_runs(
            [exp_id],
            filter_string="attributes.start_time > 0",
            run_view_type=ViewType.ACTIVE_ONLY,
        )
        assert {r.info.run_id for r in result} == {run_id1, run_id2}

        result = fs.search_runs(
            [exp_id],
            filter_string="attributes.created > 1",
            run_view_type=ViewType.ACTIVE_ONLY,
        )
        assert [r.info.run_id for r in result] == [run_id2]

        result = fs.search_runs(
            [exp_id],
            filter_string="attributes.Created > 2",
            run_view_type=ViewType.ACTIVE_ONLY,
        )
        assert result == []

    def test_weird_param_names(self):
        WEIRD_PARAM_NAME = "this is/a weird/but valid param"
        fs = FileStore(self.test_root)
        run_id = self.exp_data[FileStore.DEFAULT_EXPERIMENT_ID]["runs"][0]
        fs.log_param(run_id, Param(WEIRD_PARAM_NAME, "Value"))
        run = fs.get_run(run_id)
        assert run.data.params[WEIRD_PARAM_NAME] == "Value"

    def test_log_param_empty_str(self):
        PARAM_NAME = "new param"
        fs = FileStore(self.test_root)
        run_id = self.exp_data[FileStore.DEFAULT_EXPERIMENT_ID]["runs"][0]
        fs.log_param(run_id, Param(PARAM_NAME, ""))
        run = fs.get_run(run_id)
        assert run.data.params[PARAM_NAME] == ""

    def test_log_param_with_newline(self):
        param_name = "new param"
        param_value = "a string\nwith multiple\nlines"
        fs = FileStore(self.test_root)
        run_id = self.exp_data[FileStore.DEFAULT_EXPERIMENT_ID]["runs"][0]
        fs.log_param(run_id, Param(param_name, param_value))
        run = fs.get_run(run_id)
        assert run.data.params[param_name] == param_value

    def test_log_param_enforces_value_immutability(self):
        param_name = "new param"
        fs = FileStore(self.test_root)
        run_id = self.exp_data[FileStore.DEFAULT_EXPERIMENT_ID]["runs"][0]
        fs.log_param(run_id, Param(param_name, "value1"))
        # Duplicate calls to `log_param` with the same key and value should succeed
        fs.log_param(run_id, Param(param_name, "value1"))
        with pytest.raises(
            MlflowException, match="Changing param values is not allowed. Param with key="
        ) as e:
            fs.log_param(run_id, Param(param_name, "value2"))
        assert e.value.error_code == ErrorCode.Name(INVALID_PARAMETER_VALUE)
        run = fs.get_run(run_id)
        assert run.data.params[param_name] == "value1"

    def test_log_param_max_length_value(self):
        param_name = "new param"
        param_value = "x" * 500
        fs = FileStore(self.test_root)
        run_id = self.exp_data[FileStore.DEFAULT_EXPERIMENT_ID]["runs"][0]
        fs.log_param(run_id, Param(param_name, param_value))
        run = fs.get_run(run_id)
        assert run.data.params[param_name] == param_value
        with pytest.raises(MlflowException, match="exceeded length"):
            fs.log_param(run_id, Param(param_name, "x" * 1000))

    def test_weird_metric_names(self):
        WEIRD_METRIC_NAME = "this is/a weird/but valid metric"
        fs = FileStore(self.test_root)
        run_id = self.exp_data[FileStore.DEFAULT_EXPERIMENT_ID]["runs"][0]
        fs.log_metric(run_id, Metric(WEIRD_METRIC_NAME, 10, 1234, 0))
        run = fs.get_run(run_id)
        assert run.data.metrics[WEIRD_METRIC_NAME] == 10
        history = fs.get_metric_history(run_id, WEIRD_METRIC_NAME)
        assert len(history) == 1
        metric = history[0]
        assert metric.key == WEIRD_METRIC_NAME
        assert metric.value == 10
        assert metric.timestamp == 1234

    def test_weird_tag_names(self):
        WEIRD_TAG_NAME = "this is/a weird/but valid tag"
        fs = FileStore(self.test_root)
        run_id = self.exp_data[FileStore.DEFAULT_EXPERIMENT_ID]["runs"][0]
        fs.set_tag(run_id, RunTag(WEIRD_TAG_NAME, "Muhahaha!"))
        run = fs.get_run(run_id)
        assert run.data.tags[WEIRD_TAG_NAME] == "Muhahaha!"

    def test_set_experiment_tags(self):
        fs = FileStore(self.test_root)
        fs.set_experiment_tag(FileStore.DEFAULT_EXPERIMENT_ID, ExperimentTag("tag0", "value0"))
        fs.set_experiment_tag(FileStore.DEFAULT_EXPERIMENT_ID, ExperimentTag("tag1", "value1"))
        experiment = fs.get_experiment(FileStore.DEFAULT_EXPERIMENT_ID)
        assert len(experiment.tags) == 2
        assert experiment.tags["tag0"] == "value0"
        assert experiment.tags["tag1"] == "value1"
        # test that updating a tag works
        fs.set_experiment_tag(FileStore.DEFAULT_EXPERIMENT_ID, ExperimentTag("tag0", "value00000"))
        experiment = fs.get_experiment(FileStore.DEFAULT_EXPERIMENT_ID)
        assert experiment.tags["tag0"] == "value00000"
        assert experiment.tags["tag1"] == "value1"
        # test that setting a tag on 1 experiment does not impact another experiment.
        exp_id = None
        for exp in self.experiments:
            if exp != FileStore.DEFAULT_EXPERIMENT_ID:
                exp_id = exp
                break
        experiment = fs.get_experiment(exp_id)
        assert len(experiment.tags) == 0
        # setting a tag on different experiments maintains different values across experiments
        fs.set_experiment_tag(exp_id, ExperimentTag("tag1", "value11111"))
        experiment = fs.get_experiment(exp_id)
        assert len(experiment.tags) == 1
        assert experiment.tags["tag1"] == "value11111"
        experiment = fs.get_experiment(FileStore.DEFAULT_EXPERIMENT_ID)
        assert experiment.tags["tag0"] == "value00000"
        assert experiment.tags["tag1"] == "value1"
        # test can set multi-line tags
        fs.set_experiment_tag(exp_id, ExperimentTag("multiline_tag", "value2\nvalue2\nvalue2"))
        experiment = fs.get_experiment(exp_id)
        assert experiment.tags["multiline_tag"] == "value2\nvalue2\nvalue2"
        # test cannot set tags on deleted experiments
        fs.delete_experiment(exp_id)
        with pytest.raises(MlflowException, match="must be in the 'active'lifecycle_stage"):
            fs.set_experiment_tag(exp_id, ExperimentTag("should", "notset"))

    def test_set_tags(self):
        fs = FileStore(self.test_root)
        run_id = self.exp_data[FileStore.DEFAULT_EXPERIMENT_ID]["runs"][0]
        fs.set_tag(run_id, RunTag("tag0", "value0"))
        fs.set_tag(run_id, RunTag("tag1", "value1"))
        tags = fs.get_run(run_id).data.tags
        assert tags["tag0"] == "value0"
        assert tags["tag1"] == "value1"

        # Can overwrite tags.
        fs.set_tag(run_id, RunTag("tag0", "value2"))
        tags = fs.get_run(run_id).data.tags
        assert tags["tag0"] == "value2"
        assert tags["tag1"] == "value1"

        # Can set multiline tags.
        fs.set_tag(run_id, RunTag("multiline_tag", "value2\nvalue2\nvalue2"))
        tags = fs.get_run(run_id).data.tags
        assert tags["multiline_tag"] == "value2\nvalue2\nvalue2"

    def test_delete_tags(self):
        fs = FileStore(self.test_root)
        exp_id = self.experiments[random_int(0, len(self.experiments) - 1)]
        run_id = self.exp_data[exp_id]["runs"][0]
        fs.set_tag(run_id, RunTag("tag0", "value0"))
        fs.set_tag(run_id, RunTag("tag1", "value1"))
        tags = fs.get_run(run_id).data.tags
        assert tags["tag0"] == "value0"
        assert tags["tag1"] == "value1"
        fs.delete_tag(run_id, "tag0")
        new_tags = fs.get_run(run_id).data.tags
        assert "tag0" not in new_tags.keys()
        # test that you cannot delete tags that don't exist.
        with pytest.raises(MlflowException, match="No tag with name"):
            fs.delete_tag(run_id, "fakeTag")
        # test that you cannot delete tags for nonexistent runs
        with pytest.raises(MlflowException, match=r"Run .+ not found"):
            fs.delete_tag("random_id", "tag0")
        fs = FileStore(self.test_root)
        fs.delete_run(run_id)
        # test that you cannot delete tags for deleted runs.
        assert fs.get_run(run_id).info.lifecycle_stage == LifecycleStage.DELETED
        with pytest.raises(MlflowException, match="must be in 'active' lifecycle_stage"):
            fs.delete_tag(run_id, "tag0")

    def test_unicode_tag(self):
        fs = FileStore(self.test_root)
        run_id = self.exp_data[FileStore.DEFAULT_EXPERIMENT_ID]["runs"][0]
        value = "𝐼 𝓈𝑜𝓁𝑒𝓂𝓃𝓁𝓎 𝓈𝓌𝑒𝒶𝓇 𝓉𝒽𝒶𝓉 𝐼 𝒶𝓂 𝓊𝓅 𝓉𝑜 𝓃𝑜 𝑔𝑜𝑜𝒹"
        fs.set_tag(run_id, RunTag("message", value))
        tags = fs.get_run(run_id).data.tags
        assert tags["message"] == value

    def test_get_deleted_run(self):
        """
        Getting metrics/tags/params/run info should be allowed on deleted runs.
        """
        fs = FileStore(self.test_root)
        exp_id = self.experiments[random_int(0, len(self.experiments) - 1)]
        run_id = self.exp_data[exp_id]["runs"][0]
        fs.delete_run(run_id)
        assert fs.get_run(run_id)

    def test_set_deleted_run(self):
        """
        Setting metrics/tags/params/updating run info should not be allowed on deleted runs.
        """
        fs = FileStore(self.test_root)
        exp_id = self.experiments[random_int(0, len(self.experiments) - 1)]
        run_id = self.exp_data[exp_id]["runs"][0]
        fs.delete_run(run_id)

        assert fs.get_run(run_id).info.lifecycle_stage == LifecycleStage.DELETED
        match = "must be in 'active' lifecycle_stage"
        with pytest.raises(MlflowException, match=match):
            fs.set_tag(run_id, RunTag("a", "b"))
        with pytest.raises(MlflowException, match=match):
            fs.log_metric(run_id, Metric("a", 0.0, timestamp=0, step=0))
        with pytest.raises(MlflowException, match=match):
            fs.log_param(run_id, Param("a", "b"))

    def test_default_experiment_attempted_deletion(self):
        fs = FileStore(self.test_root)
        with pytest.raises(MlflowException, match="Cannot delete the default experiment"):
            fs.delete_experiment(FileStore.DEFAULT_EXPERIMENT_ID)
        fs = FileStore(self.test_root)
        experiment = fs.get_experiment(FileStore.DEFAULT_EXPERIMENT_ID)
        assert experiment.lifecycle_stage == LifecycleStage.ACTIVE
        test_id = fs.create_experiment("test")
        fs.delete_experiment(test_id)
        test_experiment = fs.get_experiment(test_id)
        assert test_experiment.lifecycle_stage == LifecycleStage.DELETED

    def test_malformed_experiment(self):
        fs = FileStore(self.test_root)
        exp_0 = fs.get_experiment(FileStore.DEFAULT_EXPERIMENT_ID)
        assert exp_0.experiment_id == FileStore.DEFAULT_EXPERIMENT_ID

        experiments = len(fs.search_experiments(view_type=ViewType.ALL))

        # delete metadata file.
        path = os.path.join(self.test_root, str(exp_0.experiment_id), "meta.yaml")
        os.remove(path)
        with pytest.raises(MissingConfigException, match="does not exist"):
            fs.get_experiment(FileStore.DEFAULT_EXPERIMENT_ID)

        assert len(fs.search_experiments(view_type=ViewType.ALL)) == experiments - 1

    def test_malformed_run(self):
        fs = FileStore(self.test_root)
        exp_0 = fs.get_experiment(FileStore.DEFAULT_EXPERIMENT_ID)
        all_runs = self._search(fs, exp_0.experiment_id)

        all_run_ids = self.exp_data[exp_0.experiment_id]["runs"]
        assert len(all_runs) == len(all_run_ids)

        # delete metadata file.
        bad_run_id = self.exp_data[exp_0.experiment_id]["runs"][0]
        path = os.path.join(self.test_root, str(exp_0.experiment_id), str(bad_run_id), "meta.yaml")
        os.remove(path)
        with pytest.raises(MissingConfigException, match="does not exist"):
            fs.get_run(bad_run_id)

        valid_runs = self._search(fs, exp_0.experiment_id)
        assert len(valid_runs) == len(all_runs) - 1

        for rid in all_run_ids:
            if rid != bad_run_id:
                fs.get_run(rid)

    def test_mismatching_experiment_id(self):
        fs = FileStore(self.test_root)
        exp_0 = fs.get_experiment(FileStore.DEFAULT_EXPERIMENT_ID)
        assert exp_0.experiment_id == FileStore.DEFAULT_EXPERIMENT_ID

        experiments = len(fs.search_experiments(view_type=ViewType.ALL))

        # mv experiment folder
        target = "1"
        path_orig = os.path.join(self.test_root, str(exp_0.experiment_id))
        path_new = os.path.join(self.test_root, str(target))
        os.rename(path_orig, path_new)

        with pytest.raises(MlflowException, match="Could not find experiment with ID"):
            fs.get_experiment(FileStore.DEFAULT_EXPERIMENT_ID)

        with pytest.raises(MlflowException, match="does not exist"):
            fs.get_experiment(target)
        assert len(fs.search_experiments(view_type=ViewType.ALL)) == experiments - 1

    def test_bad_experiment_id_recorded_for_run(self):
        fs = FileStore(self.test_root)
        exp_0 = fs.get_experiment(FileStore.DEFAULT_EXPERIMENT_ID)
        all_runs = self._search(fs, exp_0.experiment_id)

        all_run_ids = self.exp_data[exp_0.experiment_id]["runs"]
        assert len(all_runs) == len(all_run_ids)

        # change experiment pointer in run
        bad_run_id = str(self.exp_data[exp_0.experiment_id]["runs"][0])
        path = os.path.join(self.test_root, str(exp_0.experiment_id), bad_run_id)
        experiment_data = read_yaml(path, "meta.yaml")
        experiment_data["experiment_id"] = 1
        write_yaml(path, "meta.yaml", experiment_data, True)

        with pytest.raises(MlflowException, match="metadata is in invalid state"):
            fs.get_run(bad_run_id)

        valid_runs = self._search(fs, exp_0.experiment_id)
        assert len(valid_runs) == len(all_runs) - 1

        for rid in all_run_ids:
            if rid != bad_run_id:
                fs.get_run(rid)

    def test_log_batch(self):
        fs = FileStore(self.test_root)
        run = fs.create_run(
            experiment_id=FileStore.DEFAULT_EXPERIMENT_ID,
            user_id="user",
            start_time=0,
            tags=[],
            run_name="name",
        )
        run_id = run.info.run_id
        metric_entities = [Metric("m1", 0.87, 12345, 0), Metric("m2", 0.49, 12345, 0)]
        param_entities = [Param("p1", "p1val"), Param("p2", "p2val")]
        tag_entities = [RunTag("t1", "t1val"), RunTag("t2", "t2val")]
        fs.log_batch(
            run_id=run_id, metrics=metric_entities, params=param_entities, tags=tag_entities
        )
        self._verify_logged(fs, run_id, metric_entities, param_entities, tag_entities)

    def _create_run(self, fs):
        return fs.create_run(
            experiment_id=FileStore.DEFAULT_EXPERIMENT_ID,
            user_id="user",
            start_time=0,
            tags=[],
            run_name="name",
        )

    def test_log_batch_max_length_value(self):
        param_entities = [Param("long param", "x" * 500), Param("short param", "xyz")]
        expected_param_entities = [
            Param("long param", "x" * 500),
            Param("short param", "xyz"),
        ]
        fs = FileStore(self.test_root)
        run = self._create_run(fs)
        fs.log_batch(run.info.run_id, (), param_entities, ())
        self._verify_logged(fs, run.info.run_id, (), expected_param_entities, ())

        param_entities = [Param("long param", "x" * 1000), Param("short param", "xyz")]
        with pytest.raises(MlflowException, match="exceeded length"):
            fs.log_batch(run.info.run_id, (), param_entities, ())

    def test_log_batch_internal_error(self):
        # Verify that internal errors during log_batch result in MlflowExceptions
        fs = FileStore(self.test_root)
        run = self._create_run(fs)

        def _raise_exception_fn(*args, **kwargs):  # pylint: disable=unused-argument
            raise Exception("Some internal error")

        with mock.patch(
            FILESTORE_PACKAGE + ".FileStore._log_run_metric"
        ) as log_metric_mock, mock.patch(
            FILESTORE_PACKAGE + ".FileStore._log_run_param"
        ) as log_param_mock, mock.patch(
            FILESTORE_PACKAGE + ".FileStore._set_run_tag"
        ) as set_tag_mock:
            log_metric_mock.side_effect = _raise_exception_fn
            log_param_mock.side_effect = _raise_exception_fn
            set_tag_mock.side_effect = _raise_exception_fn
            for kwargs in [
                {"metrics": [Metric("a", 3, 1, 0)]},
                {"params": [Param("b", "c")]},
                {"tags": [RunTag("c", "d")]},
            ]:
                log_batch_kwargs = {"metrics": [], "params": [], "tags": []}
                log_batch_kwargs.update(kwargs)
                with pytest.raises(MlflowException, match="Some internal error") as e:
                    fs.log_batch(run.info.run_id, **log_batch_kwargs)
                assert e.value.error_code == ErrorCode.Name(INTERNAL_ERROR)

    def test_log_batch_nonexistent_run(self):
        fs = FileStore(self.test_root)
        nonexistent_uuid = uuid.uuid4().hex
        with pytest.raises(MlflowException, match=f"Run '{nonexistent_uuid}' not found") as e:
            fs.log_batch(nonexistent_uuid, [], [], [])
        assert e.value.error_code == ErrorCode.Name(RESOURCE_DOES_NOT_EXIST)

    def test_log_batch_params_idempotency(self):
        fs = FileStore(self.test_root)
        run = self._create_run(fs)
        params = [Param("p-key", "p-val")]
        fs.log_batch(run.info.run_id, metrics=[], params=params, tags=[])
        fs.log_batch(run.info.run_id, metrics=[], params=params, tags=[])
        self._verify_logged(fs, run.info.run_id, metrics=[], params=params, tags=[])

    def test_log_batch_tags_idempotency(self):
        fs = FileStore(self.test_root)
        run = self._create_run(fs)
        fs.log_batch(run.info.run_id, metrics=[], params=[], tags=[RunTag("t-key", "t-val")])
        fs.log_batch(run.info.run_id, metrics=[], params=[], tags=[RunTag("t-key", "t-val")])
        self._verify_logged(
            fs, run.info.run_id, metrics=[], params=[], tags=[RunTag("t-key", "t-val")]
        )

    def test_log_batch_allows_tag_overwrite(self):
        fs = FileStore(self.test_root)
        run = self._create_run(fs)
        fs.log_batch(run.info.run_id, metrics=[], params=[], tags=[RunTag("t-key", "val")])
        fs.log_batch(run.info.run_id, metrics=[], params=[], tags=[RunTag("t-key", "newval")])
        self._verify_logged(
            fs, run.info.run_id, metrics=[], params=[], tags=[RunTag("t-key", "newval")]
        )

    def test_log_batch_same_metric_repeated_single_req(self):
        fs = FileStore(self.test_root)
        run = self._create_run(fs)
        metric0 = Metric(key="metric-key", value=1, timestamp=2, step=0)
        metric1 = Metric(key="metric-key", value=2, timestamp=3, step=0)
        fs.log_batch(run.info.run_id, params=[], metrics=[metric0, metric1], tags=[])
        self._verify_logged(fs, run.info.run_id, params=[], metrics=[metric0, metric1], tags=[])

    def test_log_batch_same_metric_repeated_multiple_reqs(self):
        fs = FileStore(self.test_root)
        run = self._create_run(fs)
        metric0 = Metric(key="metric-key", value=1, timestamp=2, step=0)
        metric1 = Metric(key="metric-key", value=2, timestamp=3, step=0)
        fs.log_batch(run.info.run_id, params=[], metrics=[metric0], tags=[])
        self._verify_logged(fs, run.info.run_id, params=[], metrics=[metric0], tags=[])
        fs.log_batch(run.info.run_id, params=[], metrics=[metric1], tags=[])
        self._verify_logged(fs, run.info.run_id, params=[], metrics=[metric0, metric1], tags=[])

    def test_log_batch_allows_tag_overwrite_single_req(self):
        fs = FileStore(self.test_root)
        run = self._create_run(fs)
        tags = [RunTag("t-key", "val"), RunTag("t-key", "newval")]
        fs.log_batch(run.info.run_id, metrics=[], params=[], tags=tags)
        self._verify_logged(fs, run.info.run_id, metrics=[], params=[], tags=[tags[-1]])

    def test_log_batch_accepts_empty_payload(self):
        fs = FileStore(self.test_root)
        run = self._create_run(fs)
        fs.log_batch(run.info.run_id, metrics=[], params=[], tags=[])
        self._verify_logged(fs, run.info.run_id, metrics=[], params=[], tags=[])

    def test_log_batch_with_duplicate_params_errors_no_partial_write(self):
        fs = FileStore(self.test_root)
        run = self._create_run(fs)
        with pytest.raises(
            MlflowException, match="Duplicate parameter keys have been submitted"
        ) as e:
            fs.log_batch(
                run.info.run_id, metrics=[], params=[Param("a", "1"), Param("a", "2")], tags=[]
            )
        assert e.value.error_code == ErrorCode.Name(INVALID_PARAMETER_VALUE)
        self._verify_logged(fs, run.info.run_id, metrics=[], params=[], tags=[])

    def test_update_run_name(self):
        fs = FileStore(self.test_root)
        run = self._create_run(fs)
        run_id = run.info.run_id

        assert run.info.run_name == "name"
        assert run.data.tags.get(MLFLOW_RUN_NAME) == "name"

        fs.update_run_info(run_id, RunStatus.FINISHED, 100, "new name")
        run = fs.get_run(run_id)
        assert run.info.run_name == "new name"
        assert run.data.tags.get(MLFLOW_RUN_NAME) == "new name"

        fs.update_run_info(run_id, RunStatus.FINISHED, 100, None)
        run = fs.get_run(run_id)
        assert run.info.run_name == "new name"
        assert run.data.tags.get(MLFLOW_RUN_NAME) == "new name"

        fs.delete_tag(run_id, MLFLOW_RUN_NAME)
        run = fs.get_run(run_id)
        assert run.info.run_name == "new name"
        assert run.data.tags.get(MLFLOW_RUN_NAME) is None

        fs.update_run_info(run_id, RunStatus.FINISHED, 100, "another name")
        run = fs.get_run(run_id)
        assert run.data.tags.get(MLFLOW_RUN_NAME) == "another name"
        assert run.info.run_name == "another name"

        fs.set_tag(run_id, RunTag(MLFLOW_RUN_NAME, "yet another name"))
        run = fs.get_run(run_id)
        assert run.info.run_name == "yet another name"
        assert run.data.tags.get(MLFLOW_RUN_NAME) == "yet another name"

        fs.log_batch(run_id, metrics=[], params=[], tags=[RunTag(MLFLOW_RUN_NAME, "batch name")])
        run = fs.get_run(run_id)
        assert run.info.run_name == "batch name"
        assert run.data.tags.get(MLFLOW_RUN_NAME) == "batch name"

    def test_get_metric_history_on_non_existent_metric_key(self):
        file_store = FileStore(self.test_root)
        run = self._create_run(file_store)
        run_id = run.info.run_id
        test_metrics = file_store.get_metric_history(run_id, "test_metric")
        assert isinstance(test_metrics, PagedList)
        assert test_metrics == []


def test_experiment_with_default_root_artifact_uri(tmp_path):
    file_store_root_uri = path_to_local_file_uri(tmp_path)
    file_store = FileStore(file_store_root_uri)
    experiment_id = file_store.create_experiment(name="test", artifact_location="test")
    experiment_info = file_store.get_experiment(experiment_id)
    if is_local_os_windows():
        assert experiment_info.artifact_location == Path.cwd().joinpath("test").as_uri()
    else:
        assert experiment_info.artifact_location == str(Path.cwd().joinpath("test"))


def test_experiment_with_relative_artifact_uri(tmp_path):
    file_store_root_uri = append_to_uri_path(path_to_local_file_uri(tmp_path), "experiments")
    artifacts_root_uri = append_to_uri_path(path_to_local_file_uri(tmp_path), "artifacts")
    file_store = FileStore(file_store_root_uri, artifacts_root_uri)
    experiment_id = file_store.create_experiment(name="test")
    experiment_info = file_store.get_experiment(experiment_id)
    assert experiment_info.artifact_location == append_to_uri_path(
        artifacts_root_uri, experiment_id
    )


def _assert_create_run_appends_to_artifact_uri_path_correctly(
    artifact_root_uri, expected_artifact_uri_format
):
    with TempDir() as tmp:
        fs = FileStore(tmp.path(), artifact_root_uri)
        exp_id = fs.create_experiment("exp")
        run = fs.create_run(
            experiment_id=exp_id, user_id="user", start_time=0, tags=[], run_name="name"
        )
        cwd = Path.cwd().as_posix()
        drive = Path.cwd().drive
        if is_local_os_windows() and expected_artifact_uri_format.startswith("file:"):
            cwd = f"/{cwd}"
            drive = f"{drive}/"
        assert run.info.artifact_uri == expected_artifact_uri_format.format(
            e=exp_id, r=run.info.run_id, cwd=cwd, drive=drive
        )


@pytest.mark.skipif(not is_local_os_windows(), reason="This test only passes on Windows")
@pytest.mark.parametrize(
    ("input_uri", "expected_uri"),
    [
        ("path/to/local/folder", "file://{cwd}/path/to/local/folder/{e}/{r}/artifacts"),
        ("/path/to/local/folder", "file:///{drive}path/to/local/folder/{e}/{r}/artifacts"),
        ("#path/to/local/folder?", "file://{cwd}/{e}/{r}/artifacts#path/to/local/folder?"),
        (
            "file:///path/to/local/folder",
            "file:///{drive}path/to/local/folder/{e}/{r}/artifacts",
        ),
        (
            "file:///path/to/local/folder?param=value#fragment",
            "file:///{drive}path/to/local/folder/{e}/{r}/artifacts?param=value#fragment",
        ),
        ("file:path/to/local/folder", "file://{cwd}/path/to/local/folder/{e}/{r}/artifacts"),
        (
            "file:path/to/local/folder?param=value",
            "file://{cwd}/path/to/local/folder/{e}/{r}/artifacts?param=value",
        ),
    ],
)
def test_create_run_appends_to_artifact_local_path_file_uri_correctly_on_windows(
    input_uri, expected_uri
):
    _assert_create_run_appends_to_artifact_uri_path_correctly(input_uri, expected_uri)


@pytest.mark.skipif(is_local_os_windows(), reason="This test fails on Windows")
@pytest.mark.parametrize(
    ("input_uri", "expected_uri"),
    [
        ("path/to/local/folder", "{cwd}/path/to/local/folder/{e}/{r}/artifacts"),
        ("/path/to/local/folder", "/path/to/local/folder/{e}/{r}/artifacts"),
        ("#path/to/local/folder?", "{cwd}/#path/to/local/folder?/{e}/{r}/artifacts"),
        (
            "file:///path/to/local/folder",
            "file:///path/to/local/folder/{e}/{r}/artifacts",
        ),
        (
            "file:///path/to/local/folder?param=value#fragment",
            "file:///path/to/local/folder/{e}/{r}/artifacts?param=value#fragment",
        ),
        ("file:path/to/local/folder", "file://{cwd}/path/to/local/folder/{e}/{r}/artifacts"),
        (
            "file:path/to/local/folder?param=value",
            "file://{cwd}/path/to/local/folder/{e}/{r}/artifacts?param=value",
        ),
    ],
)
def test_create_run_appends_to_artifact_local_path_file_uri_correctly(input_uri, expected_uri):
    _assert_create_run_appends_to_artifact_uri_path_correctly(input_uri, expected_uri)


@pytest.mark.parametrize(
    ("input_uri", "expected_uri"),
    [
        ("s3://bucket/path/to/root", "s3://bucket/path/to/root/{e}/{r}/artifacts"),
        (
            "s3://bucket/path/to/root?creds=mycreds",
            "s3://bucket/path/to/root/{e}/{r}/artifacts?creds=mycreds",
        ),
        (
            "dbscheme+driver://root@host/dbname?creds=mycreds#myfragment",
            "dbscheme+driver://root@host/dbname/{e}/{r}/artifacts?creds=mycreds#myfragment",
        ),
        (
            "dbscheme+driver://root:password@hostname.com?creds=mycreds#myfragment",
            "dbscheme+driver://root:password@hostname.com/{e}/{r}/artifacts"
            "?creds=mycreds#myfragment",
        ),
        (
            "dbscheme+driver://root:password@hostname.com/mydb?creds=mycreds#myfragment",
            "dbscheme+driver://root:password@hostname.com/mydb/{e}/{r}/artifacts"
            "?creds=mycreds#myfragment",
        ),
    ],
)
def test_create_run_appends_to_artifact_uri_path_correctly(input_uri, expected_uri):
    _assert_create_run_appends_to_artifact_uri_path_correctly(input_uri, expected_uri)


def _assert_create_experiment_appends_to_artifact_uri_path_correctly(
    artifact_root_uri, expected_artifact_uri_format
):
    with TempDir() as tmp:
        fs = FileStore(tmp.path(), artifact_root_uri)
        exp_id = fs.create_experiment("exp")
        exp = fs.get_experiment(exp_id)
        cwd = Path.cwd().as_posix()
        drive = Path.cwd().drive
        if is_local_os_windows() and expected_artifact_uri_format.startswith("file:"):
            cwd = f"/{cwd}"
            drive = f"{drive}/"

        assert exp.artifact_location == expected_artifact_uri_format.format(
            e=exp_id, cwd=cwd, drive=drive
        )


@pytest.mark.skipif(not is_local_os_windows(), reason="This test only passes on Windows")
@pytest.mark.parametrize(
    ("input_uri", "expected_uri"),
    [
        ("path/to/local/folder", "file://{cwd}/path/to/local/folder/{e}"),
        ("/path/to/local/folder", "file:///{drive}path/to/local/folder/{e}"),
        ("#path/to/local/folder?", "file://{cwd}/{e}#path/to/local/folder?"),
        ("file:path/to/local/folder", "file://{cwd}/path/to/local/folder/{e}"),
        ("file:///path/to/local/folder", "file:///{drive}path/to/local/folder/{e}"),
        (
            "file:path/to/local/folder?param=value",
            "file://{cwd}/path/to/local/folder/{e}?param=value",
        ),
        ("file:///path/to/local/folder", "file:///{drive}path/to/local/folder/{e}"),
        (
            "file:///path/to/local/folder?param=value#fragment",
            "file:///{drive}path/to/local/folder/{e}?param=value#fragment",
        ),
    ],
)
def test_create_experiment_appends_to_artifact_local_path_file_uri_correctly_on_windows(
    input_uri, expected_uri
):
    _assert_create_experiment_appends_to_artifact_uri_path_correctly(input_uri, expected_uri)


@pytest.mark.skipif(is_local_os_windows(), reason="This test fails on Windows")
@pytest.mark.parametrize(
    ("input_uri", "expected_uri"),
    [
        ("path/to/local/folder", "{cwd}/path/to/local/folder/{e}"),
        ("/path/to/local/folder", "/path/to/local/folder/{e}"),
        ("#path/to/local/folder?", "{cwd}/#path/to/local/folder?/{e}"),
        ("file:path/to/local/folder", "file://{cwd}/path/to/local/folder/{e}"),
        ("file:///path/to/local/folder", "file:///path/to/local/folder/{e}"),
        (
            "file:path/to/local/folder?param=value",
            "file://{cwd}/path/to/local/folder/{e}?param=value",
        ),
        ("file:///path/to/local/folder", "file:///path/to/local/folder/{e}"),
        (
            "file:///path/to/local/folder?param=value#fragment",
            "file:///path/to/local/folder/{e}?param=value#fragment",
        ),
    ],
)
def test_create_experiment_appends_to_artifact_local_path_file_uri_correctly(
    input_uri, expected_uri
):
    _assert_create_experiment_appends_to_artifact_uri_path_correctly(input_uri, expected_uri)


@pytest.mark.parametrize(
    ("input_uri", "expected_uri"),
    [
        ("s3://bucket/path/to/root", "s3://bucket/path/to/root/{e}"),
        (
            "s3://bucket/path/to/root?creds=mycreds",
            "s3://bucket/path/to/root/{e}?creds=mycreds",
        ),
        (
            "dbscheme+driver://root@host/dbname?creds=mycreds#myfragment",
            "dbscheme+driver://root@host/dbname/{e}?creds=mycreds#myfragment",
        ),
        (
            "dbscheme+driver://root:password@hostname.com?creds=mycreds#myfragment",
            "dbscheme+driver://root:password@hostname.com/{e}?creds=mycreds#myfragment",
        ),
        (
            "dbscheme+driver://root:password@hostname.com/mydb?creds=mycreds#myfragment",
            "dbscheme+driver://root:password@hostname.com/mydb/{e}?creds=mycreds#myfragment",
        ),
    ],
)
def test_create_experiment_appends_to_artifact_uri_path_correctly(input_uri, expected_uri):
    _assert_create_experiment_appends_to_artifact_uri_path_correctly(input_uri, expected_uri)
