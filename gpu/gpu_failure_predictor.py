# SPDX-License-Identifier: Apache-2.0
# Importable shim — loads gpu-failure-predictor.py (hyphenated filename) and
# injects all of its globals into this module so that patch.object() calls
# against this module correctly intercept internal calls.
import pathlib
import sys

_src = pathlib.Path(__file__).with_name("gpu-failure-predictor.py")
_source_text = _src.read_text()
exec(compile(_source_text, str(_src), "exec"), vars(sys.modules[__name__]))  # noqa: S102
