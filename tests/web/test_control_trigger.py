"""Web /api/control/jobs/*/trigger 手动触发路径回归测试。

历史 bug: batch.post_close 手动触发时 ws_services 是 workflow.services 的 copy，
但 batch_post_close._do 需要 services["workflow"]（daemon 路径在 bootstrap.py:303
额外注入了这个）。web 路径漏掉这行注入，手动 trigger 立即 KeyError('workflow')。
"""
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def container_with_trigger(assets):
    """扩展 conftest 的 container：加 workflow + job_runner 让 trigger endpoint 能跑通。"""
    container = assets["container"]

    # 模拟 daemon 侧的 workflow.run_once 返回值（agent -> 输出 dict）
    fake_workflow = MagicMock()
    fake_workflow.run_once.return_value = {
        "portfolio-agent": {"portfolio_size": 50, "status": "ok"},
        "analyst-agent": {"rendered": "some markdown"},
    }
    fake_workflow.services = {}  # 空 dict, 让手动路径 dict(...) 后必须显式注入 workflow

    fake_runner = MagicMock()

    def _run(job_id, partition, fn, *, timeout_s):
        """真调 fn(): 让 batch_post_close._do 真执行, 才能捕获 KeyError。"""
        result = MagicMock()
        result.reason_code = None
        try:
            result.payload = fn()
            result.status = "ok"
        except KeyError as exc:
            result.status = "failed"
            result.reason_code = "UNKNOWN"
            result.payload = {"error": repr(exc)}
        except Exception as exc:  # noqa: BLE001
            result.status = "failed"
            result.reason_code = "UNKNOWN"
            result.payload = {"error": str(exc)}
        return result

    fake_runner.run.side_effect = _run
    container.workflow = fake_workflow
    container.job_runner = fake_runner
    return {"container": container, "workflow": fake_workflow, "runner": fake_runner}


def test_batch_post_close_manual_trigger_injects_workflow_into_services(
    client, container_with_trigger,
) -> None:
    """回归 bug: web 手动 trigger batch.post_close 必须在 ws_services 里注入 workflow 对象,
    否则 _do() 里 services['workflow'] 会 KeyError。"""
    r = client.post("/api/control/jobs/batch.post_close/trigger")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok", (
        f"手动 trigger 应成功执行, 但返回 {body}。历史 bug: ws_services 忘记注入 workflow。"
    )
    # 确认 workflow.run_once 真被调用了 (说明 _do 里 services['workflow'] 拿到了)
    container_with_trigger["workflow"].run_once.assert_called_once()
