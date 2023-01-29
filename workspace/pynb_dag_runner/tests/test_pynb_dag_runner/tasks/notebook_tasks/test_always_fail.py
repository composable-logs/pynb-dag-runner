import pytest

# -
from pynb_dag_runner.helpers import one
from pynb_dag_runner.opentelemetry_helpers import Spans, SpanRecorder
from pynb_dag_runner.opentelemetry_task_span_parser import parse_spans
from pynb_dag_runner.notebooks_helpers import JupytextNotebookContent
from pynb_dag_runner.tasks.tasks import make_jupytext_task
from pynb_dag_runner.wrappers import run_dag

# -
from .nb_test_helpers import get_test_jupytext_nb

# -
TEST_NOTEBOOK: JupytextNotebookContent = get_test_jupytext_nb("notebook_always_fail.py")
TASK_PARAMETERS = {
    "task.injected_parameter": 123123,
    "workflow.a": 1000,
}
TASK_TIMEOUT_S = 100.0


@pytest.fixture(scope="module")
def spans() -> Spans:
    with SpanRecorder() as rec:
        run_dag(
            make_jupytext_task(
                notebook=TEST_NOTEBOOK,
                parameters=TASK_PARAMETERS,
                timeout_s=TASK_TIMEOUT_S,
            )()
        )

    return rec.spans


def test__jupytext__always_fail__parse_spans(spans: Spans):
    pipeline_summary = parse_spans(spans)

    # assert there is one task
    task_summary = one(pipeline_summary.task_runs)

    # assert that exception is logged
    assert not task_summary.is_success()
    assert len(task_summary.exceptions) == 1
    assert "This notebook always fails" in str(task_summary.exceptions)

    # asset attributes are logged
    # (str needed to avoid mypy validation error)
    assert "notebook_always_fail.py" in str(task_summary.attributes["task.notebook"])

    assert task_summary.attributes == {
        **TASK_PARAMETERS,
        "task.num_cpus": 1,
        "task.task_type": "jupytext",
        "task.task_id": "notebook_always_fail.py",
        "task.notebook": "notebook_always_fail.py",
        "task.timeout_s": TASK_TIMEOUT_S,
    }
