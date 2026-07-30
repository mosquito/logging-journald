import asyncio
import inspect

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    test_func = pyfuncitem.obj
    if not inspect.iscoroutinefunction(test_func):
        return None

    kwargs = {name: pyfuncitem.funcargs[name] for name in inspect.signature(test_func).parameters}
    asyncio.run(test_func(**kwargs))
    return True
