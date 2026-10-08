# SPDX-License-Identifier: BSD-3-Clause
# make the test helpers importable under --import-mode importlib (repo tox.ini)
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
