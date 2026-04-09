"""
plugins.py - Plugin system for claude-usage.

Allows users to extend functionality by placing Python modules in the plugins
directory. Plugins can hook into scan events, dashboard rendering, alerts,
and custom CLI commands.

Plugin structure:
    ~/.claude/usage_plugins/
        my_plugin/
            __init__.py       # required: must define PLUGIN_META dict
            hooks.py          # optional: hook functions
        simple_plugin.py      # single-file plugin also supported

Plugin __init__.py must define:
    PLUGIN_META = {
        "name": "My Plugin",
        "version": "1.0.0",
        "description": "What it does",
        "author": "Your Name",
        "hooks": ["after_scan", "on_alert", "on_dashboard_data"],
    }

Available hooks:
    after_scan(result: dict)              Called after each JSONL scan completes
    on_alert(anomaly: dict)               Called when anomaly is detected
    on_dashboard_data(data: dict) -> dict  Transform dashboard data before serving
    on_export(data: list, format: str)    Called when data is exported
    cli_commands() -> dict                Register additional CLI commands
"""

import importlib
import importlib.util
import sys
import traceback
from pathlib import Path

from config import PLUGINS_DIR


_loaded_plugins: dict[str, dict] = {}
_hooks: dict[str, list] = {}


def _load_single_file_plugin(filepath: Path) -> dict | None:
    """Load a single .py file as a plugin."""
    name = filepath.stem
    try:
        spec = importlib.util.spec_from_file_location(f"cu_plugin_{name}", filepath)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"cu_plugin_{name}"] = module
        spec.loader.exec_module(module)
        meta = getattr(module, "PLUGIN_META", None)
        if meta is None:
            meta = {"name": name, "version": "0.0.0", "description": "No metadata",
                    "hooks": []}
        return {"meta": meta, "module": module, "path": str(filepath)}
    except Exception as e:
        print(f"  Plugin load error ({name}): {e}")
        return None


def _load_package_plugin(dirpath: Path) -> dict | None:
    """Load a directory-based plugin package."""
    name = dirpath.name
    init_file = dirpath / "__init__.py"
    if not init_file.exists():
        return None

    try:
        spec = importlib.util.spec_from_file_location(f"cu_plugin_{name}", init_file,
                                                        submodule_search_locations=[str(dirpath)])
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"cu_plugin_{name}"] = module
        spec.loader.exec_module(module)
        meta = getattr(module, "PLUGIN_META", None)
        if meta is None:
            meta = {"name": name, "version": "0.0.0", "description": "No metadata",
                    "hooks": []}
        return {"meta": meta, "module": module, "path": str(dirpath)}
    except Exception as e:
        print(f"  Plugin load error ({name}): {e}")
        return None


def discover_plugins() -> list[dict]:
    """Discover all plugins in the plugins directory (without loading them)."""
    if not PLUGINS_DIR.exists():
        return []

    plugins = []
    for item in PLUGINS_DIR.iterdir():
        if item.is_file() and item.suffix == ".py" and item.name != "__init__.py":
            plugins.append({"name": item.stem, "type": "file", "path": str(item)})
        elif item.is_dir() and (item / "__init__.py").exists():
            plugins.append({"name": item.name, "type": "package", "path": str(item)})

    return plugins


def load_plugins(verbose: bool = False) -> dict[str, dict]:
    """Load all plugins from the plugins directory."""
    global _loaded_plugins, _hooks
    _loaded_plugins = {}
    _hooks = {}

    if not PLUGINS_DIR.exists():
        return _loaded_plugins

    for item in PLUGINS_DIR.iterdir():
        plugin = None

        if item.is_file() and item.suffix == ".py" and item.name != "__init__.py":
            plugin = _load_single_file_plugin(item)
        elif item.is_dir() and (item / "__init__.py").exists():
            plugin = _load_package_plugin(item)

        if plugin:
            name = plugin["meta"].get("name", item.name)
            _loaded_plugins[name] = plugin

            # Register hooks
            module = plugin["module"]
            for hook_name in plugin["meta"].get("hooks", []):
                fn = getattr(module, hook_name, None)
                # Also check hooks.py submodule for package plugins
                if fn is None and item.is_dir():
                    hooks_file = item / "hooks.py"
                    if hooks_file.exists():
                        try:
                            hooks_spec = importlib.util.spec_from_file_location(
                                f"cu_plugin_{item.name}_hooks", hooks_file
                            )
                            if hooks_spec and hooks_spec.loader:
                                hooks_module = importlib.util.module_from_spec(hooks_spec)
                                hooks_spec.loader.exec_module(hooks_module)
                                fn = getattr(hooks_module, hook_name, None)
                        except Exception:
                            pass

                if fn and callable(fn):
                    if hook_name not in _hooks:
                        _hooks[hook_name] = []
                    _hooks[hook_name].append({"plugin": name, "fn": fn})

            if verbose:
                hook_count = len(plugin["meta"].get("hooks", []))
                print(f"  Loaded plugin: {name} v{plugin['meta'].get('version', '?')} "
                      f"({hook_count} hooks)")

    return _loaded_plugins


def run_hook(hook_name: str, data=None) -> any:
    """
    Execute all registered handlers for a hook.

    For hooks that return data (like on_dashboard_data), the output of each
    handler is passed as input to the next (pipeline pattern).
    """
    # Lazy-load plugins on first hook call
    if not _loaded_plugins and PLUGINS_DIR.exists() and any(PLUGINS_DIR.iterdir()):
        load_plugins()

    handlers = _hooks.get(hook_name, [])
    result = data

    for handler in handlers:
        try:
            ret = handler["fn"](result)
            if ret is not None:
                result = ret
        except Exception as e:
            print(f"  Plugin hook error ({handler['plugin']}.{hook_name}): {e}")
            traceback.print_exc()

    return result


def get_cli_commands() -> dict:
    """Collect CLI commands registered by plugins."""
    if not _loaded_plugins and PLUGINS_DIR.exists():
        load_plugins()

    commands = {}
    for handler in _hooks.get("cli_commands", []):
        try:
            cmds = handler["fn"](None)
            if isinstance(cmds, dict):
                commands.update(cmds)
        except Exception:
            pass

    return commands


def list_loaded() -> list[dict]:
    """Return info about all loaded plugins."""
    return [
        {
            "name": name,
            "version": p["meta"].get("version", "?"),
            "description": p["meta"].get("description", ""),
            "author": p["meta"].get("author", ""),
            "hooks": p["meta"].get("hooks", []),
            "path": p["path"],
        }
        for name, p in _loaded_plugins.items()
    ]


def create_plugin_scaffold(name: str, plugin_dir: Path = PLUGINS_DIR) -> Path:
    """Create a new plugin scaffold directory."""
    plugin_dir.mkdir(parents=True, exist_ok=True)
    plugin_path = plugin_dir / name

    if plugin_path.exists():
        raise FileExistsError(f"Plugin '{name}' already exists at {plugin_path}")

    plugin_path.mkdir()

    init_content = f'''"""
{name} - Custom plugin for claude-usage.
"""

PLUGIN_META = {{
    "name": "{name}",
    "version": "1.0.0",
    "description": "Description of what this plugin does",
    "author": "Your Name",
    "hooks": ["after_scan"],
}}


def after_scan(result):
    """Called after each JSONL scan completes."""
    turns = result.get("turns", 0)
    if turns > 0:
        print(f"  [{name}] Scan found {{turns}} new turns")
'''

    (plugin_path / "__init__.py").write_text(init_content, encoding="utf-8")

    return plugin_path
