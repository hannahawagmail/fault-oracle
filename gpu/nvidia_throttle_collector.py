# SPDX-License-Identifier: Apache-2.0
# Importable shim — loads nvidia-throttle-collector.py (hyphenated filename)
# and execs its source into this module's namespace so that patch.object()
# calls against this module correctly intercept internal calls.
import pathlib
import sys

_src = pathlib.Path(__file__).with_name("nvidia-throttle-collector.py")
_source_text = _src.read_text()
exec(compile(_source_text, str(_src), "exec"), vars(sys.modules[__name__]))  # noqa: S102
