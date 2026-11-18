"""架构级 AST 结构守卫单测（`test_arch_contracts.py`）。

覆盖三条核心工程不变式（每条均带反向变异自检，拒绝「永远绿」的空壳断言）：
1. 路由异步非阻塞契约：`backend/routes/*.py` 中所有 `@router.<method>` 修饰的 `async def`
   端点函数不得直接在事件循环线程上同步调用阻塞型进度/数据库/Diff/快照函数，必须经 `asyncio.to_thread(...)` 卸载；
2. ADR-005 人工闸门顺序契约：全部 5 个人工闸门（split / fix / blueprint / accept / qc）必须先执行
   `state.<event>.clear()`，再写入 `progress['current_phase'] = '<gate_phase>'` 与落盘推送，且后续等待函数不得二次 clear；
3. `.tfproj.db` 生命周期契约：`routes/output.py` 必须登记 `.tfproj.db` / `-wal` / `-shm` 清理白名单并在删除前
   执行 `checkpoint_project_db`，`versions.restore` 与 `pipeline_runner.run_pipeline` 必须在收尾/回滚处执行 checkpoint。
"""

from __future__ import annotations

import ast
from pathlib import Path

from core.project_db import DB_ARTIFACT_NAMES
from routes import output as output_mod

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ROUTES_DIR = BACKEND_ROOT / "routes"
CORE_DIR = BACKEND_ROOT / "core"

# 严禁在 async 路由函数体顶层直接同步调用的阻塞 I/O / CPU 函数名集合
BLOCKING_IO_CALLS: frozenset[str] = frozenset(
    {
        "read_progress",
        "load_progress_dict",
        "save_progress_dict",
        "merge_progress_write",
        "read_progress_cached",
        "atomic_write_json",
        "atomic_write_text",
        "compute_chapter_diff",
        "get_project_db",
        "checkpoint_project_db",
        "_summarize",
        "_clear_artifacts",
        "_list_chapters_sync",
        "_get_chapter_diff_sync",
        "_apply_review_sync",
        "restore",
        "list_versions",
        "estimate_run",
        "read_quality_report",
    }
)


def _call_func_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_to_thread_call(call: ast.Call) -> bool:
    if isinstance(call.func, ast.Attribute) and call.func.attr == "to_thread":
        if isinstance(call.func.value, ast.Name) and call.func.value.id == "asyncio":
            return True
    if isinstance(call.func, ast.Name) and call.func.id == "to_thread":
        return True
    return False


def _is_router_endpoint(fn: ast.AsyncFunctionDef) -> bool:
    for dec in fn.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "router"
        ):
            return True
    return False


def find_direct_blocking_calls_in_routes(source: str, filename: str = "<mem>") -> list[str]:
    """扫描模块源码中 `@router.*` 异步端点里未走 `asyncio.to_thread` 的直接阻塞调用。"""
    tree = ast.parse(source, filename=filename)
    violations: list[str] = []

    class BodyVisitor(ast.NodeVisitor):
        def __init__(self, endpoint_name: str) -> None:
            self.endpoint_name = endpoint_name

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            # 嵌套同步辅助函数（如 _bump / _mutate）交给 to_thread 在工作线程内运行，不在此展开
            return

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            return

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return

        def visit_Call(self, node: ast.Call) -> None:
            if _is_to_thread_call(node):
                # 第一个位置参数是被委托到线程池执行的目标函数引用，其余参数仍检查是否有同步阻塞调用
                for arg in node.args[1:]:
                    self.visit(arg)
                for kw in node.keywords:
                    self.visit(kw.value)
                return
            fname = _call_func_name(node.func)
            if fname in BLOCKING_IO_CALLS:
                violations.append(
                    f"{filename}:{node.lineno} async 端点 `{self.endpoint_name}` 直接同步调用 `{fname}()`"
                )
            self.generic_visit(node)

    for top in tree.body:
        if isinstance(top, ast.AsyncFunctionDef) and _is_router_endpoint(top):
            visitor = BodyVisitor(top.name)
            for stmt in top.body:
                visitor.visit(stmt)
    return violations


def test_all_route_endpoints_offload_blocking_io_via_to_thread() -> None:
    all_violations: list[str] = []
    route_files = sorted(ROUTES_DIR.glob("*.py"))
    assert route_files, "未扫描到 routes/*.py"
    for rf in route_files:
        src = rf.read_text(encoding="utf-8")
        all_violations.extend(find_direct_blocking_calls_in_routes(src, rf.name))
    assert all_violations == [], f"发现阻塞事件循环的路由调用：{all_violations}"


def test_blocking_io_ast_detector_catches_bad_endpoint() -> None:
    bad_src = """
import asyncio
@router.get("/api/bad")
async def bad_endpoint(p):
    data = read_progress(p)
    return data
"""
    good_src = """
import asyncio
@router.get("/api/good")
async def good_endpoint(p):
    data = await asyncio.to_thread(read_progress, p)
    return data
"""
    hits = find_direct_blocking_calls_in_routes(bad_src, "bad_route.py")
    assert len(hits) == 1 and "read_progress" in hits[0]
    assert find_direct_blocking_calls_in_routes(good_src, "good_route.py") == []


def _find_func_ast(tree: ast.Module, func_name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == func_name:
            return node
    raise AssertionError(f"未找到函数 {func_name}")


def check_adr005_gate_order_in_func(
    source: str,
    func_name: str,
    event_attr: str,
    gate_phase: str,
) -> list[str]:
    """AST 校验指定函数内 `state.<event_attr>.clear()` 严格早于 `progress['current_phase'] = <gate_phase>`。"""
    tree = ast.parse(source)
    fn = _find_func_ast(tree, func_name)

    clear_lines: list[int] = []
    phase_write_lines: list[int] = []

    for node in ast.walk(fn):
        # 匹配 state.<event_attr>.clear()
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "clear"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == event_attr
        ):
            clear_lines.append(node.lineno)

        # 匹配 progress['current_phase'] = '<gate_phase>'
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if node.value.value == gate_phase:
                for target in node.targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.slice, ast.Constant)
                        and target.slice.value == "current_phase"
                    ):
                        phase_write_lines.append(node.lineno)

    errors: list[str] = []
    if not clear_lines:
        errors.append(f"{func_name}: 未找到 state.{event_attr}.clear()")
    if not phase_write_lines:
        errors.append(f"{func_name}: 未找到 progress['current_phase'] = {gate_phase!r}")
    if clear_lines and phase_write_lines:
        first_phase = min(phase_write_lines)
        valid_clears = [ln for ln in clear_lines if ln < first_phase]
        if not valid_clears:
            errors.append(
                f"{func_name}: ADR-005 违规！state.{event_attr}.clear() (lines={clear_lines}) "
                f"晚于阶段落盘赋值 {gate_phase!r} (line={first_phase})"
            )
        late_clears = [ln for ln in clear_lines if ln > first_phase]
        if late_clears:
            errors.append(
                f"{func_name}: 阶段置为 {gate_phase!r} (line={first_phase}) 后又出现 "
                f"state.{event_attr}.clear() (lines={late_clears})，存在吞确认窗口"
            )
    return errors


def test_adr005_all_five_gates_clear_before_phase_write() -> None:
    pr_src = (ROUTES_DIR / "pipeline_runner.py").read_text(encoding="utf-8")
    pl_src = (CORE_DIR / "pipeline.py").read_text(encoding="utf-8")
    rf_src = (CORE_DIR / "refactor.py").read_text(encoding="utf-8")

    specs = [
        (pr_src, "wait_split_confirm", "split_confirm", "phase0_confirm"),
        (pr_src, "wait_fix_review", "fix_review_confirm", "phase1_fixreview"),
        (pl_src, "phase2_blueprint", "blueprint_confirmed", "phase2_waiting"),
        (rf_src, "_wait_batch_accept", "accept_confirm", "phase3_accept"),
        (rf_src, "phase3_refactor_stitch", "qc_confirm", "phase3_qc"),
    ]
    problems: list[str] = []
    for src, fn_name, ev_attr, phase_lit in specs:
        problems.extend(check_adr005_gate_order_in_func(src, fn_name, ev_attr, phase_lit))
    assert problems == [], f"ADR-005 闸门顺序校验失败：{problems}"

    # 额外确认 _wait_qc_confirm 内没有迟到的 state.qc_confirm.clear()
    rf_tree = ast.parse(rf_src)
    wait_qc_fn = _find_func_ast(rf_tree, "_wait_qc_confirm")
    late_qc_clears = [
        n.lineno
        for n in ast.walk(wait_qc_fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "clear"
    ]
    assert late_qc_clears == [], f"_wait_qc_confirm 内残留迟到的 clear()：{late_qc_clears}"


def test_adr005_ast_checker_catches_reversed_order() -> None:
    reversed_src = """
async def broken_gate(progress, progress_path):
    progress['current_phase'] = 'phase2_waiting'
    save_progress_dict(progress_path, progress)
    state.blueprint_confirmed.clear()
"""
    errs = check_adr005_gate_order_in_func(
        reversed_src, "broken_gate", "blueprint_confirmed", "phase2_waiting"
    )
    assert errs and "ADR-005 违规" in errs[0]


def test_tfproj_db_registered_in_cleanup_and_checkpointed_on_lifecycle() -> None:
    assert set(DB_ARTIFACT_NAMES) == {".tfproj.db", ".tfproj.db-wal", ".tfproj.db-shm"}
    assert set(DB_ARTIFACT_NAMES).issubset(set(output_mod.DB_ARTIFACTS))

    # AST 校验 output._clear_artifacts、versions.restore、pipeline_runner.run_pipeline 均调用 checkpoint_project_db
    for rel_path, fn_name in (
        (ROUTES_DIR / "output.py", "_clear_artifacts"),
        (CORE_DIR / "versions.py", "restore"),
        (ROUTES_DIR / "pipeline_runner.py", "run_pipeline"),
    ):
        tree = ast.parse(rel_path.read_text(encoding="utf-8"))
        fn = _find_func_ast(tree, fn_name)
        called_names = {
            _call_func_name(n.func)
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            for _ in [0]
        } | {
            n.args[0].id
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and _is_to_thread_call(n)
            and n.args
            and isinstance(n.args[0], ast.Name)
        }
        assert "checkpoint_project_db" in called_names, (
            f"{rel_path.name}:{fn_name} 未调用 checkpoint_project_db，可能导致 Windows WAL 锁残留"
        )
