# coding: utf-8
"""通用 mypyc 隔离构建器及命令行入口。

用途
====
将常规 Python 包中的可编译模块交给 mypyc 构建为本机扩展，同时保留不能编译
或必须保持动态行为的模块。构建过程全部发生在临时目录，既不修改源码文件，
也不把 ``.so``/``.pyd`` 写到源码旁边；最终产物统一部署到 ``native_root``。

设计边界
========
* 只支持含 ``__init__.py`` 的常规 Python 包，不处理 namespace package。
* ``excluded`` 指定不参与编译的模块或目录；``always_python`` 通常至少包含
  ``__init__.py`` 和 ``__main__.py``。
* mypyc 类型检查失败的模块会自动退出编译边界，并以原始 ``.py`` 文件回退。
* ``resource_paths`` 用于复制运行所需的非 Python 资源。
* 构建器核心不依赖 client_perf；文件后半部分仅提供 client_perf 的默认配置。

构建与加载流程
==============
1. 把包的 Python 源码复制到临时 staging 目录。
2. 探测可通过 mypyc 类型检查的最大模块集合。
3. 在另一个临时目录编译扩展。
4. 将扩展、纯 Python 回退模块和资源复制到独立 ``native_root``。
5. 调用 ``activate()``，让隔离产物在当前进程的导入路径中优先于源码包。

产物目录镜像原包结构，例如 ``native_root/my_package/*.so``；mypyc 的共享运行时
模块 ``__mypyc*`` 放在 ``native_root`` 顶层。源码目录始终保持纯 Python 状态。

Python API 示例
===============

    builder = NativeBuilder(
        package_name="my_package",
        package_root=Path("my_package"),
        native_root=Path(".runtime/native"),
        excluded={"my_package/routes"},
        resource_paths=("static",),
    )
    builder.build()
    builder.activate()  # 必须早于业务子模块导入

命令行示例
==========
``nativebuild`` 不带包参数时使用 client_perf 默认配置：

    nativebuild
    nativebuild --status
    nativebuild --clean

也可作为通用脚本使用：

    nativebuild --package-name my_package --package-root ./my_package \
        --native-root ./.runtime/native --exclude my_package/routes \
        --resource static

注意事项
========
* ``activate()`` 只影响当前进程；需要子进程继承时，调用方应传递对应环境变量。
* ``clean()`` 只清理该构建器的 ``native_root``。client_perf 的兼容入口还会清理
  旧版本遗留在源码包旁边的同名扩展。
* 原生扩展与 Python 版本、操作系统和 CPU 架构绑定，不应跨环境复制使用。
"""

from __future__ import annotations

import argparse
import importlib
import importlib.machinery
import os
import re
import shutil
import sys
import tempfile
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Iterator, Sequence

_ERROR_LINE = re.compile(r"^([^\s:]+\.py):\d+(?::\d+)?: error:", re.MULTILINE)


@contextmanager
def _working_directory(path: Path) -> Iterator[None]:
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


@dataclass
class NativeBuilder:
    """通用 mypyc 构建、隔离部署、激活和清理能力。"""

    package_name: str
    package_root: Path
    native_root: Path
    excluded: set[str] = field(default_factory=set)
    always_python: set[str] = field(default_factory=lambda: {"__init__.py", "__main__.py"})
    resource_paths: tuple[str, ...] = ()
    opt_level: str = "3"
    debug_level: str = "1"

    def __post_init__(self) -> None:
        self.package_root = Path(self.package_root).expanduser().resolve()
        self.native_root = Path(self.native_root).expanduser().resolve()
        if not self.package_name or any(not part.isidentifier() for part in self.package_name.split(".")):
            raise ValueError(f"无效 Python 包名: {self.package_name!r}")
        if not self.package_root.is_dir():
            raise ValueError(f"包目录不存在: {self.package_root}")
        if not (self.package_root / "__init__.py").is_file():
            raise ValueError(f"当前仅支持常规 Python 包，缺少 __init__.py: {self.package_root}")

    @property
    def package_path(self) -> Path:
        return Path(*self.package_name.split("."))

    @property
    def native_package_root(self) -> Path:
        return self.native_root / self.package_path

    def _module_path(self, relative: str) -> str:
        return (self.package_path / relative).as_posix()

    def candidates(self) -> list[str]:
        """返回相对包根目录的可编译模块。"""
        excluded = {item.replace("\\", "/").removeprefix("./") for item in self.excluded}
        result: list[str] = []
        for path in sorted(self.package_root.rglob("*.py")):
            if "__pycache__" in path.parts or path.name in self.always_python:
                continue
            relative = path.relative_to(self.package_root).as_posix()
            full_name = self._module_path(relative)
            if any(
                candidate == item.rstrip("/")
                or candidate.startswith(item.rstrip("/") + "/")
                for item in excluded
                for candidate in (relative, full_name)
            ):
                continue
            result.append(relative)
        return result

    def _stage_package(self, staging_root: Path) -> Path:
        staged = staging_root / self.package_path
        for source in self.package_root.rglob("*.py"):
            if "__pycache__" in source.parts:
                continue
            destination = staged / source.relative_to(self.package_root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        return staging_root

    def _probe_boundary(self, candidates: Sequence[str], module_base: Path) -> tuple[list[str], list[str]]:
        """找出可同时通过 mypyc 类型检查的最大模块集合。

        mypyc 会把一次调用中的模块作为整体检查；任一模块失败都会终止整次构建。
        这里根据错误位置逐轮剔除失败模块，保留其余可编译模块，并把被剔除项交给
        后续纯 Python 回退流程。无法从错误输出定位模块时直接失败，避免静默漏编。
        """
        from mypyc.build import mypycify

        accepted = [self._module_path(name) for name in candidates]
        dropped: list[str] = []
        with tempfile.TemporaryDirectory(prefix="mypyc-builder-probe-") as temp_name:
            for _ in range(1, len(accepted) + 1):
                args = ["--ignore-missing-imports", "--follow-imports=skip", *accepted]
                output = StringIO()
                try:
                    with redirect_stdout(output), redirect_stderr(output), _working_directory(module_base):
                        mypycify(
                            args,
                            opt_level=self.opt_level,
                            multi_file=True,
                            target_dir=str(Path(temp_name) / "generated"),
                        )
                except (Exception, SystemExit) as exc:
                    details = f"{exc}\n{output.getvalue()}"
                    failed = {name for name in _ERROR_LINE.findall(details) if name in accepted}
                    if not failed:
                        raise RuntimeError(
                            f"mypyc 类型检查失败且无法定位出错模块:\n{details.strip()}"
                        ) from exc
                    accepted = [name for name in accepted if name not in failed]
                    dropped.extend(sorted(failed))
                    if not accepted:
                        raise RuntimeError("没有任何模块能通过 mypyc 类型检查") from exc
                    continue
                break
        prefix = self.package_path.as_posix() + "/"
        return (
            [name.removeprefix(prefix) for name in accepted],
            [name.removeprefix(prefix) for name in dropped],
        )

    @staticmethod
    def ensure_build_dependencies() -> None:
        try:
            import mypyc.build  # noqa: F401
            import setuptools  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("原生编译需要 mypy 和 setuptools") from exc

    def build(self, quiet: bool = False) -> dict[str, list[str]]:
        """在临时目录完成编译并隔离部署，返回构建摘要。

        ``native_package_root`` 镜像原包结构存放扩展和纯 Python 回退模块；
        ``__mypyc`` 共享运行时放在 ``native_root`` 顶层。整个过程不污染源码树。
        """
        self.ensure_build_dependencies()
        from mypyc.build import mypycify
        from setuptools import Distribution
        from setuptools.command.build_ext import build_ext

        candidates = self.candidates()
        if not candidates:
            raise RuntimeError(f"包中没有可编译模块: {self.package_root}")

        with tempfile.TemporaryDirectory(prefix="mypyc-builder-stage-") as stage_name:
            module_base = self._stage_package(Path(stage_name))
            accepted, dropped = self._probe_boundary(candidates, module_base)
            if not quiet:
                print(f"[native] 编译边界: {len(accepted)}/{len(candidates)}")
                for name in dropped:
                    print(f"[native] 回退纯 Python: {name}")

            with tempfile.TemporaryDirectory(prefix="mypyc-builder-native-") as temp_name:
                temp_root = Path(temp_name)
                args = [
                    "--ignore-missing-imports",
                    "--follow-imports=skip",
                    *[self._module_path(name) for name in accepted],
                ]
                with _working_directory(module_base):
                    extensions = mypycify(
                        args,
                        opt_level=self.opt_level,
                        debug_level=self.debug_level,
                        multi_file=True,
                        target_dir=str(temp_root / "generated"),
                        strict_dunder_typing=False,
                    )
                    command = build_ext(Distribution({"ext_modules": extensions}))
                    command.build_lib = str(temp_root / "lib")
                    command.build_temp = str(temp_root / "objects")
                    command.ensure_finalized()
                    command.run()

                deployed: list[str] = []
                build_lib = (temp_root / "lib").resolve()
                for output in command.get_outputs():
                    source = Path(output).resolve()
                    if not source.is_file():
                        continue
                    try:
                        relative = source.relative_to(build_lib)
                    except ValueError:
                        continue
                    parts = relative.parts
                    if tuple(parts[: len(self.package_path.parts)]) == self.package_path.parts:
                        package_relative = Path(*parts[len(self.package_path.parts):])
                        destination = self.native_package_root / package_relative
                    elif parts and "__mypyc" in parts[0]:
                        package_relative = Path(*parts)
                        destination = self.native_root / package_relative
                    else:
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
                    deployed.append(package_relative.as_posix())

        compiled = set(accepted)
        fallbacks: list[str] = []
        for source in sorted(self.package_root.rglob("*.py")):
            if "__pycache__" in source.parts:
                continue
            relative = source.relative_to(self.package_root)
            if relative.as_posix() in compiled:
                continue
            destination = self.native_package_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            fallbacks.append(relative.as_posix())

        for relative_name in self.resource_paths:
            source = self.package_root / relative_name
            destination = self.native_package_root / relative_name
            if source.is_dir():
                shutil.copytree(source, destination, dirs_exist_ok=True)
            elif source.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)

        return {"deployed": deployed, "dropped": dropped, "fallbacks": fallbacks}

    def activate(self) -> Path:
        """让隔离扩展优先加载；必须在导入业务子模块之前调用。"""
        if not self.native_package_root.is_dir():
            raise RuntimeError(f"原生产物目录不存在: {self.native_package_root}")
        if str(self.native_root) not in sys.path:
            sys.path.insert(0, str(self.native_root))
        package = importlib.import_module(self.package_name)
        package_path = getattr(package, "__path__", None)
        if package_path is None:
            raise RuntimeError(f"目标不是 Python 包: {self.package_name}")
        if str(self.native_package_root) not in package_path:
            package_path.insert(0, str(self.native_package_root))
        importlib.invalidate_caches()
        return self.native_root

    def status(self) -> dict[str, list[str]]:
        native: list[str] = []
        pure: list[str] = []
        for py_file in sorted(self.package_root.rglob("*.py")):
            if "__pycache__" in py_file.parts or py_file.name in self.always_python:
                continue
            relative = py_file.relative_to(self.package_root)
            native_parent = self.native_package_root / relative.parent
            has_native = any(
                (native_parent / (py_file.stem + suffix)).is_file()
                for suffix in importlib.machinery.EXTENSION_SUFFIXES
            )
            (native if has_native else pure).append(relative.as_posix())
        return {"native": native, "pure": pure}

    def clean(self) -> list[str]:
        """仅清理本实例的隔离根目录，不扫描或删除项目其他位置。"""
        if not self.native_root.is_dir():
            return []
        removed = [
            path.relative_to(self.native_root).as_posix()
            for path in sorted(self.native_root.rglob("*"))
            if path.is_file()
        ]
        shutil.rmtree(self.native_root)
        return removed

ALWAYS_PYTHON = {"__init__.py", "__main__.py"}
# FastAPI 依赖 Python 函数签名生成请求参数与 OpenAPI；mypyc 扩展函数不保留
# 完整 inspect.signature 元数据，因此整个路由层必须保持纯 Python。
RUNTIME_EXCLUDED = {
    "client_perf/cli.py",          # 入口需支持运行时 monkeypatch/命令分发
    "client_perf/native_build.py", # 构建工具自身保持纯 Python
    "client_perf/routers",         # FastAPI 依赖可反射的函数签名
}


def _package_root() -> Path:
    return Path(__file__).resolve().parent


def _installed_root() -> Path:
    """兼容旧调用：当前导入的包目录。"""
    return _package_root()


def _source_project_root() -> Path | None:
    candidate = _package_root().parent
    if (candidate / "setup.py").is_file() and (candidate / "client_perf" / "cli.py").is_file():
        return candidate
    return None


def _native_root() -> Path:
    """确定 client_perf 产物目录：显式环境变量 > 源码项目 > 用户目录。"""
    override = os.environ.get("CLIENT_PERF_NATIVE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    project_root = _source_project_root()
    if project_root is not None:
        return project_root / ".runtime" / "native"
    return Path.home() / ".client_perf" / "native"


def create_builder(
    package_name: str,
    package_root: Path,
    native_root: Path,
    *,
    excluded: set[str] | None = None,
    always_python: set[str] | None = None,
    resource_paths: tuple[str, ...] = (),
) -> NativeBuilder:
    """创建适用于任意常规 Python 包的原生构建器。"""
    return NativeBuilder(
        package_name=package_name,
        package_root=package_root,
        native_root=native_root,
        excluded=set(excluded or ()),
        always_python=set(always_python or {"__init__.py", "__main__.py"}),
        resource_paths=resource_paths,
    )


def _builder() -> NativeBuilder:
    """client_perf 的默认构建配置。"""
    return create_builder(
        package_name="client_perf",
        package_root=_package_root(),
        native_root=_native_root(),
        excluded=RUNTIME_EXCLUDED,
        always_python=ALWAYS_PYTHON,
        resource_paths=("tool", "test_result/index.html"),
    )


def iter_candidate_modules(package_root: Path) -> list[str]:
    builder = NativeBuilder(
        package_name="client_perf",
        package_root=package_root,
        native_root=_native_root(),
        excluded=set(RUNTIME_EXCLUDED),
        always_python=set(ALWAYS_PYTHON),
    )
    return builder.candidates()


def build_and_deploy(quiet: bool = False) -> dict[str, list[str]]:
    result = _builder().build(quiet=quiet)
    print(f"[native] 完成: {len(result['deployed'])} 个原生扩展已部署到 {_native_root()}")
    return result


def status() -> dict[str, list[str]]:
    return _builder().status()


def _remove_legacy_inplace_extensions() -> list[str]:
    """移除旧版构建器写在 ``.py`` 同目录下的同名原生扩展。

    新实现不会产生这类文件；该兼容清理仅用于消除历史版本遗留物。
    """
    package_root = _installed_root()
    removed: list[str] = []
    for path in sorted(package_root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if not any(path.name.endswith(suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES):
            continue
        stem = path.name.split(".", 1)[0]
        if (path.parent / (stem + ".py")).is_file():
            path.unlink()
            removed.append(path.relative_to(package_root).as_posix())
    return removed


def clean() -> list[str]:
    removed = _remove_legacy_inplace_extensions()
    removed.extend(_builder().clean())
    return removed


def activate_native() -> Path:
    """激活 client_perf 原生产物，并通过环境变量把路径传给重载子进程。"""
    native_root = _builder().activate()
    os.environ["CLIENT_PERF_NATIVE_ROOT"] = str(native_root)
    return native_root


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="通用 mypyc 隔离构建器")
    parser.add_argument("--package-name", default="client_perf", help="目标 Python 包名")
    parser.add_argument("--package-root", type=Path, default=_package_root(), help="目标包目录")
    parser.add_argument("--native-root", type=Path, default=_native_root(), help="原生产物根目录")
    parser.add_argument("--exclude", action="append", default=[], help="排除的模块或目录，可重复指定")
    parser.add_argument("--resource", action="append", default=[], help="需要复制的包内资源，可重复指定")
    parser.add_argument("--status", action="store_true", help="查看原生/纯 Python 模块状态")
    parser.add_argument("--clean", action="store_true", help="清理原生产物")
    parser.add_argument("--quiet", action="store_true", help="减少构建输出")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """执行构建、状态查询或清理。

    参数完全保持默认值时使用 client_perf 的预设排除项与资源；只要调用方覆盖任一
    包参数，就进入通用模式，所有排除项和资源都以调用方显式传入的值为准。
    """
    args = _parse_args(argv)
    using_client_perf_defaults = (
        args.package_name == "client_perf"
        and args.package_root.resolve() == _package_root()
        and args.native_root.resolve() == _native_root()
        and not args.exclude
        and not args.resource
    )
    builder = _builder() if using_client_perf_defaults else create_builder(
        package_name=args.package_name,
        package_root=args.package_root,
        native_root=args.native_root,
        excluded=set(args.exclude),
        resource_paths=tuple(args.resource),
    )

    if args.status:
        result = builder.status()
        print("原生运行模块:")
        for name in result["native"]:
            print(f"  .so  {name}")
        print("纯 Python 模块:")
        for name in result["pure"]:
            print(f"  .py  {name}")
        return 0
    if args.clean:
        removed = builder.clean()
        if using_client_perf_defaults:
            removed = _remove_legacy_inplace_extensions() + removed
        if removed:
            print("已移除的原生扩展:")
            for name in removed:
                print(f"  - {name}")
        else:
            print("没有发现原生扩展")
        return 0

    result = builder.build(quiet=args.quiet)
    print(f"[native] 完成: {len(result['deployed'])} 个原生扩展已部署到 {builder.native_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
