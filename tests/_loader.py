"""插件测试的公共加载器：按宿主的方式把 ``plugin.py`` 当作包导入。

宿主（``src/plugin_runtime/runner/plugin_loader.py``）用的是：

.. code-block:: python

    module_name = f"_maibot_plugin_{re.sub(r'[^0-9A-Za-z_]', '_', plugin_id)}"
    spec = importlib.util.spec_from_file_location(
        module_name, plugin_path, submodule_search_locations=[plugin_dir]
    )

这里必须复刻这个规则：``spec_from_file_location`` 一旦带了
``submodule_search_locations``，模块自己的 ``__package__`` 就等于模块名，
于是 ``plugin.py`` 里的 ``from .audio_codec import ...`` 会解析成
``<module_name>.audio_codec``。如果测试用带点的模块名（例如
``pkg.plugin``），相对导入会落到 ``pkg.plugin.audio_codec``，和测试里
``import pkg.audio_codec`` 拿到的**不是同一个模块对象** —— 打桩就会失效，
测试会真的发网络请求。
"""

from __future__ import annotations

import importlib
import importlib.util
import re
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, Optional

PLUGIN_ID = "lgv-h.mimo-asr-bridge"
PLUGIN_DIR = Path(__file__).resolve().parents[1]

# 测试里统一使用这套配置：启用插件 + 一个假 api_key
BASE_CONFIG: Dict[str, Any] = {
    "plugin": {"enabled": True, "config_version": "1.0.0"},
    "asr": {"api_key": "sk-test"},
}


def build_host_module_name(plugin_id: str) -> str:
    """复刻宿主 ``PluginLoader._build_safe_module_name`` 的合成模块名规则。"""

    normalized = re.sub(r"[^0-9A-Za-z_]", "_", str(plugin_id or "").strip())
    if normalized and normalized[0].isdigit():
        normalized = f"_{normalized}"
    return f"_maibot_plugin_{normalized or 'plugin'}"


MODULE_NAME = build_host_module_name(PLUGIN_ID)


def require_sdk() -> None:
    """确认宿主的 SDK 可以导入。

    这些测试跑的是真实的 ``maibot_sdk`` 契约（配置校验、装饰器元数据、Hook 载荷），
    不是打桩版本；SDK 不在环境里时跳过，而不是假装通过。

    Raises:
        unittest.SkipTest: 当前环境没有 maibot_sdk。
    """

    try:
        import maibot_sdk  # noqa: F401
    except ImportError as exc:
        raise unittest.SkipTest(f"未安装 maibot_sdk，跳过插件测试: {exc}") from exc


def load_plugin_module() -> Any:
    """加载插件目录下的 ``plugin.py``（同一进程内只加载一次，和宿主一致）。"""

    require_sdk()
    cached_module = sys.modules.get(MODULE_NAME)
    if cached_module is not None:
        return cached_module

    spec = importlib.util.spec_from_file_location(
        MODULE_NAME,
        str(PLUGIN_DIR / "plugin.py"),
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    # 必须先登记父包，plugin.py 里的相对导入才找得到自己
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def load_submodule(short_name: str) -> Any:
    """加载插件目录下的子模块，例如 ``audio_codec`` / ``mimo_asr_client``。

    带一层断言：拿到的必须是 ``plugin.py`` 实际导入的那个模块对象，
    否则测试打桩的就是另一个副本（打桩失效、真的会联网）。

    Raises:
        AssertionError: 子模块对象和插件实际使用的不是同一个。
    """

    plugin_module = load_plugin_module()
    submodule = importlib.import_module(f"{MODULE_NAME}.{short_name}")
    loaded_by_plugin = getattr(plugin_module, short_name, None)
    if loaded_by_plugin is not submodule:
        raise AssertionError(
            f"子模块 {short_name} 的加载路径和 plugin.py 内部导入的不一致，"
            "测试打桩会失效；请检查合成模块名是否复刻了宿主规则"
        )
    return submodule


def build_configured_plugin(overrides: Optional[Dict[str, Dict[str, Any]]] = None) -> Any:
    """构造一个注入了上下文、应用了配置的插件实例（尚未 ``on_load``）。

    Args:
        overrides: 按段覆盖的配置，例如 ``{"asr": {"stream": True}}``。

    Returns:
        Any: 插件实例。
    """

    from maibot_sdk.context import PluginContext

    plugin = load_plugin_module().create_plugin()
    plugin._set_context(PluginContext(plugin_id=PLUGIN_ID))

    config: Dict[str, Any] = {section: dict(values) for section, values in BASE_CONFIG.items()}
    for section, values in (overrides or {}).items():
        config.setdefault(section, {}).update(values)
    plugin.set_plugin_config(config)
    return plugin
