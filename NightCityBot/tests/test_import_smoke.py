import importlib
import os
import pkgutil
import pytest


def _discover_modules(package_path: str, package_name: str):
    modules = []
    pkg_dir = os.path.join(os.path.dirname(__file__), "..", package_path.replace("NightCityBot/", ""))
    abs_dir = os.path.abspath(pkg_dir)
    if not os.path.isdir(abs_dir):
        return modules
    for info in pkgutil.iter_modules([abs_dir]):
        if info.name.startswith("__"):
            continue
        modules.append(f"{package_name}.{info.name}")
    return modules


ALL_MODULES = (
    _discover_modules("NightCityBot/cogs", "NightCityBot.cogs")
    + _discover_modules("NightCityBot/services", "NightCityBot.services")
    + _discover_modules("NightCityBot/utils", "NightCityBot.utils")
)


@pytest.mark.parametrize("module_name", ALL_MODULES)
def test_module_imports_cleanly(module_name):
    importlib.import_module(module_name)
