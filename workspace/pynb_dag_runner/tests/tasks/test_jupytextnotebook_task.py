import datetime
from pathlib import Path
from typing import Any, Dict

#
import pytest

#
from pynb_dag_runner.tasks.tasks import make_jupytext_task
from pynb_dag_runner.core.dag_runner import run_tasks, TaskDependencies
from pynb_dag_runner.helpers import one
from pynb_dag_runner.notebooks_helpers import JupytextNotebook
from pynb_dag_runner.opentelemetry_helpers import Spans, SpanRecorder

# TODO: all the below tests should run multiple times in stress tests
# See, https://github.com/pynb-dag-runner/pynb-dag-runner/pull/5


def isotimestamp_normalized():
    """
    Return ISO timestamp modified (by replacing : with _) so that it can be used
    as part of a directory or file name.

    Eg "YYYY-MM-DDTHH-MM-SS.ffffff+00-00"

    This is useful to generate output directories that are guaranteed to not exist.
    """
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace(":", "-")


def test_jupytext_run_ok_notebook():
    def get_test_spans():
        with SpanRecorder() as rec:
            dependencies = TaskDependencies()

            nb_path: Path = (Path(__file__).parent) / "jupytext_test_notebooks"
            jupytext_task = make_jupytext_task(
                notebook=JupytextNotebook(nb_path / "notebook_ok.py"),
                task_id="123",
                tmp_dir=nb_path,
                timeout_s=5,
                n_max_retries=1,
                task_parameters={"task.variable_a": "task-value"},
            )

            run_tasks([jupytext_task], dependencies)

        return rec.spans

    def validate_spans(spans: Spans):
        py_span = one(
            spans.filter(["name"], "invoke-task").filter(
                ["attributes", "task_type"], "python"
            )
        )
        assert py_span["status"] == {"status_code": "OK"}

        jupytext_span = one(
            spans.filter(["name"], "invoke-task").filter(
                ["attributes", "task_type"], "jupytext"
            )
        )
        spans.contains_path(jupytext_span, py_span)

        assert jupytext_span["status"] == {"status_code": "OK"}
        for content in ["<html>", str(1 + 12 + 123), "variable_a=task-value"]:
            assert content in jupytext_span["attributes"]["notebook_html"]

    validate_spans(get_test_spans())


@pytest.mark.parametrize("N_retries", [2, 10])
def test_jupytext_exception_throwing_notebook(N_retries):
    def get_test_spans():
        with SpanRecorder() as rec:
            dependencies = TaskDependencies()

            nb_path: Path = (Path(__file__).parent) / "jupytext_test_notebooks"
            jupytext_task = make_jupytext_task(
                notebook=JupytextNotebook(nb_path / "notebook_exception.py"),
                task_id="123",
                tmp_dir=nb_path,
                timeout_s=5,
                n_max_retries=N_retries,
                task_parameters={},
            )

            run_tasks([jupytext_task], dependencies)

        return rec.spans

    # notebook will fail on first three runs. Depending on number of retries
    # determine which run:s are success/failed.
    def ok_indices():
        if N_retries == 2:
            return []
        else:
            return [3]

    def failed_indices():
        if N_retries == 2:
            return [0, 1]
        else:
            return [0, 1, 2]

    def validate_spans(spans: Spans):
        jupytext_span = one(
            spans.filter(["name"], "invoke-task").filter(
                ["attributes", "task_type"], "jupytext"
            )
        )
        if len(ok_indices()) > 0:
            assert jupytext_span["status"] == {"status_code": "OK"}
        else:
            assert jupytext_span["status"] == {
                "status_code": "ERROR",
                "description": "Jupytext notebook task failed",
            }

        run_spans = spans.filter(["name"], "task-run").sort_by_start_time()
        assert len(run_spans) == len(ok_indices()) + len(failed_indices())

        for idx in ok_indices():
            assert run_spans[idx]["status"] == {"status_code": "OK"}

        for idx in failed_indices():
            failed_run_span = run_spans[idx]

            exception = one(spans.exceptions_in(failed_run_span))["attributes"]
            assert exception["exception.type"] == "PapermillExecutionError"
            assert "Thrown from notebook!" in exception["exception.message"]

            assert run_spans[idx]["status"] == {
                "status_code": "ERROR",
                "description": "Run failed",
            }

        # for both successful and failed runs, there should be (partially evaluated)
        # notebook in html format
        for content in ["<html>", str(1 + 12 + 123)]:
            assert content in jupytext_span["attributes"]["notebook_html"]

    validate_spans(get_test_spans())


def test_jupytext_stuck_notebook():
    """
    Currently, timeout canceling is done on Ray level, but error handling and
    recovery is done only within the Python process (using try .. catch).
    Therefore, timeout canceled tasks can not currently do proper error handling.
    """

    def get_test_spans():
        with SpanRecorder() as rec:
            dependencies = TaskDependencies()

            nb_path: Path = (Path(__file__).parent) / "jupytext_test_notebooks"
            jupytext_task = make_jupytext_task(
                notebook=JupytextNotebook(nb_path / "notebook_stuck.py"),
                task_id="234",
                tmp_dir=nb_path,
                timeout_s=5,
                n_max_retries=1,
                task_parameters={},
            )

            run_tasks([jupytext_task], dependencies)

        return rec.spans

    def validate_spans(spans: Spans):
        py_span = one(
            spans.filter(["name"], "invoke-task").filter(
                ["attributes", "task_type"], "python"
            )
        )
        assert py_span["status"] == {
            "description": "Task failed",
            "status_code": "ERROR",
        }

        jupytext_span = one(
            spans.filter(["name"], "invoke-task").filter(
                ["attributes", "task_type"], "jupytext"
            )
        )
        assert jupytext_span["status"] == {
            "description": "Jupytext notebook task failed",
            "status_code": "ERROR",
        }

        timeout_guard_span = one(spans.filter(["name"], "timeout-guard"))
        assert timeout_guard_span["status"] == {
            "status_code": "ERROR",
            "description": "Timeout",
        }

        spans.contains_path(jupytext_span, timeout_guard_span, py_span)

        assert len(spans.exceptions_in(jupytext_span)) == 0

        assert "notebook_html" in jupytext_span["attributes"]

    validate_spans(get_test_spans())
