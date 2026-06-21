# SPDX-License-Identifier: Apache-2.0
# Importable shim — loads nvidia-xid-collector.py (hyphenated filename)
# and injects all of its globals into this module so that patch.object()
# calls against this module correctly intercept internal calls.
import importlib.util
import pathlib
import sys

_src = pathlib.Path(__file__).with_name("nvidia-xid-collector.py")
_spec = importlib.util.spec_from_file_location("_nvidia_xid_impl", _src)
_impl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_impl)

# Inject all public names from the impl into THIS module's namespace.
# This means the functions defined in the impl now live here, and their
# internal globals still point to the impl module — so we also need to
# make _run patchable here by wrapping the calls.
# Strategy: copy callable references AND make the module-level _run a
# mutable reference that the copied functions will use via a closure trick.

# The cleanest approach: exec the source directly into this module's globals.
_source_text = _src.read_text()
exec(compile(_source_text, str(_src), "exec"), vars(sys.modules[__name__]))  # noqa: S102
